"""Runtime known-device store: persistence + the known_provider hook into
NetworkInventoryService/RulesEngine, plus the POST /api/inventory/known
route. Mirrors test_device_meta.py's store-level coverage and
test_type_pin_api.py's router/app-level coverage."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_inventory import build_inventory_router
from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta
from wavr.known_store import KnownStore
from wavr.netinventory_service import NetworkInventoryService
from wavr.rules import RulesEngine

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
  192.168.0.42          DE-AD-BE-EF-00-01     dynamic
"""

KNOWN = {"a4:83:e7:11:22:33"}   # static env allowlist -- only the Apple host


async def _fake_scan() -> str:
    return WINDOWS_ARP


# ---- KnownStore: persistence -------------------------------------------------

def _store(tmp_path):
    return KnownStore(str(tmp_path / "t.db"))


def test_set_known_true_then_is_known(tmp_path):
    s = _store(tmp_path)
    entry = s.set_known("24:0A:C4:AA:BB:CC", True)
    assert entry == {"mac": "24:0a:c4:aa:bb:cc", "known": True}
    assert s.is_known("24:0a:c4:aa:bb:cc") is True
    assert s.known_macs() == {"24:0a:c4:aa:bb:cc"}


def test_unmarked_mac_is_not_known(tmp_path):
    s = _store(tmp_path)
    assert s.is_known("24:0a:c4:aa:bb:cc") is False
    assert s.known_macs() == set()


def test_set_known_false_after_true_removes_from_known_macs(tmp_path):
    s = _store(tmp_path)
    s.set_known("24:0a:c4:aa:bb:cc", True)
    s.set_known("24:0a:c4:aa:bb:cc", False)
    assert s.is_known("24:0a:c4:aa:bb:cc") is False
    assert s.known_macs() == set()


def test_set_known_rejects_invalid_mac(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.set_known("not-a-mac", True)


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "t.db")
    KnownStore(p).set_known("24:0a:c4:aa:bb:cc", True)
    assert KnownStore(p).is_known("24:0a:c4:aa:bb:cc") is True


def test_in_memory_store_for_tests():
    s = KnownStore(":memory:")
    s.set_known("24:0a:c4:aa:bb:cc", True)
    assert s.is_known("24:0a:c4:aa:bb:cc") is True


# ---- known_provider hook: NetworkInventoryService ----------------------------

def _service(known_provider=None) -> NetworkInventoryService:
    return NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   known_provider=known_provider)


async def test_known_provider_suppresses_rogue_alert_on_next_scan():
    known = set()
    svc = _service(known_provider=lambda: known)
    await svc.scan_once()
    macs = {a.mac for a in svc.recent_alerts()}
    assert "24:0a:c4:aa:bb:cc" in macs               # unknown -> alerts

    known.add("24:0a:c4:aa:bb:cc")                    # mark known at runtime
    svc2 = _service(known_provider=lambda: known)      # fresh service, fresh scan
    await svc2.scan_once()
    assert all(a.mac != "24:0a:c4:aa:bb:cc" for a in svc2.recent_alerts())
    by_mac = {d.mac: d for d in svc2.latest_inventory()}
    assert by_mac["24:0a:c4:aa:bb:cc"].known is True


async def test_static_allowlist_still_honored_with_known_provider_wired():
    svc = _service(known_provider=lambda: set())      # provider present but empty
    await svc.scan_once()
    assert all(a.mac != "a4:83:e7:11:22:33" for a in svc.recent_alerts())  # still allowlisted


async def test_broken_known_provider_never_breaks_scanning():
    def _boom():
        raise RuntimeError("store unavailable")
    svc = _service(known_provider=_boom)
    devices = await svc.scan_once()                   # must not raise
    assert devices


async def test_apply_known_change_true_clears_existing_alert_and_updates_cache():
    svc = _service()
    await svc.scan_once()
    assert any(a.mac == "24:0a:c4:aa:bb:cc" for a in svc.recent_alerts())

    svc.apply_known_change("24:0a:c4:aa:bb:cc", True)
    assert all(a.mac != "24:0a:c4:aa:bb:cc" for a in svc.recent_alerts())
    by_mac = {d.mac: d for d in svc.latest_inventory()}
    assert by_mac["24:0a:c4:aa:bb:cc"].known is True


async def test_apply_known_change_false_rearms_edge_trigger():
    known = {"24:0a:c4:aa:bb:cc"}
    svc = _service(known_provider=lambda: known)
    await svc.scan_once()                              # known -> no alert
    assert all(a.mac != "24:0a:c4:aa:bb:cc" for a in svc.recent_alerts())

    known.discard("24:0a:c4:aa:bb:cc")                 # store un-marks it
    svc.apply_known_change("24:0a:c4:aa:bb:cc", False)  # re-arm the dedup set
    await svc.scan_once()                               # unknown again -> re-alerts
    assert any(a.mac == "24:0a:c4:aa:bb:cc" for a in svc.recent_alerts())


# ---- known_provider hook: RulesEngine -----------------------------------------

def test_rules_known_provider_suppresses_alert():
    msgs = []
    known = {"de:ad:be:ef:00:01"}
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)),
                      known_macs={"a4:83:e7:11:22:33"},
                      known_provider=lambda: known)
    eng.handle_devices([
        {"mac": "de:ad:be:ef:00:01", "ip": "192.168.0.42",
         "vendor": "unknown", "device_type": "unknown", "known": False},
    ])
    assert [m for m in msgs if m[0] == "wavr/security/rogue"] == []


def test_rules_broken_known_provider_falls_back_to_static_allowlist():
    msgs = []

    def _boom():
        raise RuntimeError("store unavailable")

    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)),
                      known_macs={"a4:83:e7:11:22:33"}, known_provider=_boom)
    eng.handle_devices([
        {"mac": "a4:83:e7:11:22:33", "known": False},   # still statically allowlisted
    ])
    assert [m for m in msgs if m[0] == "wavr/security/rogue"] == []
    eng.handle_devices([
        {"mac": "de:ad:be:ef:00:01", "ip": "192.168.0.42",
         "vendor": "unknown", "device_type": "unknown", "known": False},
    ])
    assert len(msgs) == 1   # the truly-unknown MAC still alerts -- provider failure isn't fatal


# ---- POST /api/inventory/known (router level) ---------------------------------

def _router_client(known_store=None, device_meta=None, name_deps=None) -> TestClient:
    svc = _service(known_provider=known_store.known_macs if known_store else None)
    asyncio.run(svc.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(
        svc, device_meta=device_meta, known_store=known_store, name_deps=name_deps))
    return TestClient(app)


def test_post_known_persists_and_returns_entry():
    ks = KnownStore(":memory:")
    with _router_client(known_store=ks) as c:
        r = c.post("/api/inventory/known",
                   json={"mac": "24-0A-C4-AA-BB-CC", "known": True})
    assert r.status_code == 200
    assert r.json() == {"mac": "24:0a:c4:aa:bb:cc", "known": True}
    assert ks.is_known("24:0a:c4:aa:bb:cc") is True


def test_post_known_rejects_invalid_mac():
    ks = KnownStore(":memory:")
    with _router_client(known_store=ks) as c:
        r = c.post("/api/inventory/known", json={"mac": "nope", "known": True})
    assert r.status_code == 400


def test_post_known_absent_without_known_store():
    with _router_client() as c:
        r = c.post("/api/inventory/known",
                   json={"mac": "24:0a:c4:aa:bb:cc", "known": True})
    assert r.status_code == 404


def test_post_known_true_drops_from_current_alerts():
    ks = KnownStore(":memory:")
    svc = _service()
    asyncio.run(svc.scan_once())
    assert any(a.mac == "24:0a:c4:aa:bb:cc" for a in svc.recent_alerts())
    app = FastAPI()
    app.include_router(build_inventory_router(svc, known_store=ks))
    with TestClient(app) as c:
        c.post("/api/inventory/known", json={"mac": "24:0a:c4:aa:bb:cc", "known": True})
        alerts = c.get("/api/alerts").json()["alerts"]
    assert all(a["mac"] != "24:0a:c4:aa:bb:cc" for a in alerts)


def test_known_field_exposed_on_inventory_view():
    ks = KnownStore(":memory:")
    with _router_client(known_store=ks) as c:
        r = c.get("/api/inventory")
    by_mac = {d["mac"]: d for d in r.json()["devices"]}
    assert by_mac["24:0a:c4:aa:bb:cc"]["known"] is False
    with _router_client(known_store=ks) as c:
        c.post("/api/inventory/known", json={"mac": "24:0a:c4:aa:bb:cc", "known": True})
        r = c.get("/api/inventory")
    by_mac = {d["mac"]: d for d in r.json()["devices"]}
    assert by_mac["24:0a:c4:aa:bb:cc"]["known"] is True


# ---- app-level CSRF gating (mirrors PUT /api/inventory/type) ------------------

def test_app_post_known_requires_local_header():
    dm = DeviceMeta(":memory:")
    ks = KnownStore(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                     device_meta=dm, known_store=ks)
    with TestClient(app) as c:                    # no X-Wavr-Local header
        r = c.post("/api/inventory/known",
                   json={"mac": "24:0a:c4:aa:bb:cc", "known": True})
    assert r.status_code == 403
    assert ks.is_known("24:0a:c4:aa:bb:cc") is False   # rejected -> never persisted


def test_app_post_known_works_with_local_header():
    dm = DeviceMeta(":memory:")
    ks = KnownStore(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                     device_meta=dm, known_store=ks)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/inventory/known",
                   json={"mac": "24:0a:c4:aa:bb:cc", "known": True})
    assert r.status_code == 200
    assert ks.is_known("24:0a:c4:aa:bb:cc") is True


# ---- POST /api/inventory/known/bulk ("Trust all N devices") -------------------

def test_bulk_marks_every_currently_unknown_device():
    ks = KnownStore(":memory:")
    svc = _service()   # WINDOWS_ARP: 1 allowlisted (gateway not counted -- see below), 2 unknown
    asyncio.run(svc.scan_once())
    unknown_before = [d.mac for d in svc.latest_inventory() if not d.known]
    assert unknown_before   # sanity: fixture actually has unknown devices
    app = FastAPI()
    app.include_router(build_inventory_router(svc, known_store=ks))
    with TestClient(app) as c:
        r = c.post("/api/inventory/known/bulk")
    assert r.status_code == 200
    assert r.json() == {"marked": len(unknown_before)}
    for mac in unknown_before:
        assert ks.is_known(mac) is True
    assert all(d.known for d in svc.latest_inventory())   # cache patched immediately


def test_bulk_is_idempotent_second_call_marks_zero():
    ks = KnownStore(":memory:")
    svc = _service()
    asyncio.run(svc.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(svc, known_store=ks))
    with TestClient(app) as c:
        c.post("/api/inventory/known/bulk")
        r = c.post("/api/inventory/known/bulk")
    assert r.json() == {"marked": 0}


def test_bulk_drops_all_current_rogue_alerts():
    ks = KnownStore(":memory:")
    svc = _service()
    asyncio.run(svc.scan_once())
    assert svc.recent_alerts()   # sanity: unknown devices alerted
    app = FastAPI()
    app.include_router(build_inventory_router(svc, known_store=ks))
    with TestClient(app) as c:
        c.post("/api/inventory/known/bulk")
        alerts = c.get("/api/alerts").json()["alerts"]
    assert alerts == []
    assert svc.recent_alerts() == []


def test_bulk_absent_without_known_store():
    svc = _service()
    asyncio.run(svc.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(svc))   # no known_store
    with TestClient(app) as c:
        r = c.post("/api/inventory/known/bulk")
    assert r.status_code == 404


def test_app_bulk_requires_local_header():
    dm = DeviceMeta(":memory:")
    ks = KnownStore(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                     device_meta=dm, known_store=ks)
    with TestClient(app) as c:                    # no X-Wavr-Local header
        r = c.post("/api/inventory/known/bulk")
    assert r.status_code == 403


def test_app_bulk_works_with_local_header():
    dm = DeviceMeta(":memory:")
    ks = KnownStore(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                     device_meta=dm, known_store=ks)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/inventory/known/bulk")
    assert r.status_code == 200
    assert "marked" in r.json()
