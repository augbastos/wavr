import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_inventory import build_inventory_router
from wavr.config import load_config
from wavr.device_meta import DeviceMeta
from wavr.netinventory import Device
from wavr.netinventory_service import NetworkInventoryService, RogueAlert

# Raw Windows `arp -a` text: one allowlisted Apple host + two unlisted hosts
# (an Espressif and a truly-unknown OUI), plus broadcast/multicast noise rows
# that the inventory must drop and must never flag as rogue.
WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
  192.168.0.42          DE-AD-BE-EF-00-01     dynamic
  192.168.0.255         FF-FF-FF-FF-FF-FF     static
  224.0.0.22            01-00-5E-00-00-16     static
"""

KNOWN = {"a4:83:e7:11:22:33"}   # only the Apple host is allowlisted


async def _fake_scan() -> str:
    return WINDOWS_ARP


def _service() -> NetworkInventoryService:
    return NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0)


# ---- inventory shape ---------------------------------------------------------

async def test_latest_inventory_shape_after_scan():
    svc = _service()
    assert svc.latest_inventory() == []          # empty before the first scan
    inv = await svc.scan_once()
    assert inv is svc.latest_inventory() or inv == svc.latest_inventory()
    held = svc.latest_inventory()
    assert all(isinstance(d, Device) for d in held)
    assert {d.mac for d in held} == {
        "a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01",
    }
    by_mac = {d.mac: d for d in held}
    assert by_mac["a4:83:e7:11:22:33"].known is True
    assert by_mac["a4:83:e7:11:22:33"].vendor == "Apple"
    assert by_mac["24:0a:c4:aa:bb:cc"].known is False


async def test_port_awareness_off_by_default_no_risks():
    svc = _service()
    await svc.scan_once()
    assert all(d.risks == () for d in svc.latest_inventory())


# ---- edge-triggered rogue alerting ------------------------------------------

async def test_unknown_mac_alerts_exactly_once_across_rescans():
    svc = _service()
    await svc.scan_once()
    await svc.scan_once()                          # a rescan must NOT re-alert
    await svc.scan_once()
    macs = [a.mac for a in svc.recent_alerts()]
    # both unlisted hosts alert once each; neither is duplicated
    assert sorted(macs) == ["24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"]
    assert macs.count("de:ad:be:ef:00:01") == 1


async def test_known_mac_never_alerts():
    svc = _service()
    await svc.scan_once()
    assert all(a.mac != "a4:83:e7:11:22:33" for a in svc.recent_alerts())


async def test_alert_carries_timestamp_mac_and_vendor():
    svc = _service()
    await svc.scan_once()
    alert = next(a for a in svc.recent_alerts() if a.mac == "24:0a:c4:aa:bb:cc")
    assert isinstance(alert, RogueAlert)
    assert alert.ts and alert.mac == "24:0a:c4:aa:bb:cc"
    assert alert.vendor == "Espressif"
    d = alert.to_dict()
    assert set(d) >= {"ts", "mac", "vendor"}
    assert d["ip"] == "192.168.0.23"


async def test_broadcast_and_multicast_never_alert():
    svc = _service()
    await svc.scan_once()
    macs = {a.mac for a in svc.recent_alerts()}
    assert "ff:ff:ff:ff:ff:ff" not in macs
    assert "01:00:5e:00:00:16" not in macs


# ---- start()/stop() cancel-safety -------------------------------------------

async def test_start_scans_then_stop_is_cancel_safe():
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0.01)
    await svc.start()
    for _ in range(50):                            # let the loop run >=1 scan
        if svc.latest_inventory():
            break
        await asyncio.sleep(0.01)
    assert svc.latest_inventory()                  # background task populated it
    await svc.stop()                               # must not raise
    await svc.stop()                               # idempotent second stop


# ---- read-only router (FastAPI TestClient on a tiny app) --------------------

def _router_client(device_meta=None, name_deps=None) -> TestClient:
    svc = _service()
    asyncio.run(svc.scan_once())                   # seed one scan synchronously
    app = FastAPI()
    app.include_router(build_inventory_router(svc, device_meta=device_meta, name_deps=name_deps))
    return TestClient(app)


def test_inventory_endpoint_returns_device_json():
    with _router_client() as c:
        r = c.get("/api/inventory")
    assert r.status_code == 200
    devices = r.json()["devices"]
    by_mac = {d["mac"]: d for d in devices}
    assert set(by_mac) == {
        "a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01",
    }
    apple = by_mac["a4:83:e7:11:22:33"]
    # name/first_seen/last_seen are always present (Feature A/C) -- None when
    # no device_meta store is wired in, as here.
    assert set(apple) == {"mac", "ip", "vendor", "device_type", "known",
                           "name", "first_seen", "last_seen"}
    assert apple["known"] is True and apple["vendor"] == "Apple"
    assert apple["name"] is None and apple["first_seen"] is None and apple["last_seen"] is None
    assert "risks" not in apple                    # port-scan off -> no risk notes


# ---- Feature A: device_meta merge + seen() wiring ----------------------------

def test_scan_once_calls_device_meta_seen_for_each_observed_mac():
    dm = DeviceMeta(":memory:")
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0, device_meta=dm)
    asyncio.run(svc.scan_once())
    for mac in ("a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"):
        assert dm.get(mac) is not None
        assert dm.get(mac)["first_seen"] is not None


def test_scan_once_tolerates_a_broken_device_meta_store():
    class BoomMeta:
        def seen(self, mac):
            raise RuntimeError("disk full")
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0, device_meta=BoomMeta())
    devices = asyncio.run(svc.scan_once())          # must not raise
    assert devices                                   # scan still completed


def test_inventory_endpoint_merges_name_and_seen_fields():
    dm = DeviceMeta(":memory:")
    dm.set_name("a4:83:e7:11:22:33", "MacBook do Augusto")
    svc = _service()
    asyncio.run(svc.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(svc, device_meta=dm))
    with TestClient(app) as c:
        r = c.get("/api/inventory")
    by_mac = {d["mac"]: d for d in r.json()["devices"]}
    apple = by_mac["a4:83:e7:11:22:33"]
    assert apple["name"] == "MacBook do Augusto"
    assert apple["first_seen"] is None and apple["last_seen"] is None   # named but never scanned via this dm
    unnamed = by_mac["24:0a:c4:aa:bb:cc"]
    assert unnamed["name"] is None


# ---- Feature A: PUT /api/inventory/name (router-level, unguarded) -----------

def test_put_name_endpoint_persists_and_returns_entry():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        r = c.put("/api/inventory/name", json={"mac": "A4-83-E7-11-22-33", "name": "Sala TV"})
    assert r.status_code == 200
    assert r.json() == {"mac": "a4:83:e7:11:22:33", "name": "Sala TV",
                         "first_seen": None, "last_seen": None}
    assert dm.get("a4:83:e7:11:22:33")["name"] == "Sala TV"


def test_put_name_endpoint_rejects_invalid_mac():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        r = c.put("/api/inventory/name", json={"mac": "not-a-mac", "name": "x"})
    assert r.status_code == 400


def test_put_name_endpoint_rejects_empty_name():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "   "})
    assert r.status_code == 400


def test_put_name_endpoint_absent_without_device_meta():
    # No device_meta wired in -> the write route isn't even registered.
    with _router_client() as c:
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "x"})
    assert r.status_code == 404


def test_alerts_endpoint_returns_rogue_json():
    with _router_client() as c:
        r = c.get("/api/alerts")
    assert r.status_code == 200
    alerts = r.json()["alerts"]
    macs = {a["mac"] for a in alerts}
    assert macs == {"24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"}
    assert all({"ts", "mac", "vendor"} <= set(a) for a in alerts)


# ---- config wiring -----------------------------------------------------------

def test_config_has_net_scan_interval_default(monkeypatch):
    monkeypatch.delenv("WAVR_NET_SCAN_INTERVAL", raising=False)
    assert load_config().net_scan_interval == 30.0


# ---- on_rogue callback (opt-in ntfy hook) -------------------------------------

async def test_on_rogue_fires_once_per_new_rogue_mac():
    calls = []
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   on_rogue=lambda a: calls.append(a))
    await svc.scan_once()
    await svc.scan_once()   # rescan must NOT re-fire (edge-triggered, same rule as alerts)
    assert sorted(a.mac for a in calls) == ["24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"]
    assert all(isinstance(a, RogueAlert) for a in calls)


async def test_on_rogue_never_fires_for_known_mac():
    calls = []
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   on_rogue=lambda a: calls.append(a))
    await svc.scan_once()
    assert all(a.mac != "a4:83:e7:11:22:33" for a in calls)


async def test_on_rogue_absent_by_default_no_crash():
    svc = _service()   # no on_rogue passed
    await svc.scan_once()   # must not raise


async def test_on_rogue_exception_is_swallowed_not_propagated():
    def boom(alert):
        raise RuntimeError("callback exploded")

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0, on_rogue=boom)
    devices = await svc.scan_once()   # must not raise despite the callback exploding
    assert devices   # scan still completed and returned the inventory
