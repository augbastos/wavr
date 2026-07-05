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
    # no device_meta store is wired in, as here. type_confidence/make are the
    # recog additions ("make" appears because the vendor is known; sources
    # appear because recog recorded evidence).
    assert set(apple) == {"mac", "ip", "vendor", "device_type", "type_confidence",
                           "known", "name", "first_seen", "last_seen", "make",
                           "sources"}
    assert apple["known"] is True and apple["vendor"] == "Apple"
    assert apple["type_confidence"] in ("low", "medium", "high")
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
                         "first_seen": None, "last_seen": None,
                         "device_type": None}
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


# ---- passive mDNS/SSDP collector wiring (defensive-inventory collectors) --------------

class _FakeCollector:
    """Injectable stand-in for MDNSCollector/SSDPCollector: no sockets, no
    real network, keyed by IP exactly like the real collectors' output."""

    def __init__(self, by_ip: dict | None = None, boom: bool = False):
        self._by_ip = by_ip or {}
        self._boom = boom
        self.calls = 0

    async def collect(self, duration: float = 3.0) -> dict:
        self.calls += 1
        if self._boom:
            raise RuntimeError("collector exploded")
        return dict(self._by_ip)


def test_collectors_off_by_default_never_invoked():
    fake = _FakeCollector({"192.168.0.23": {"device_type": "camera"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   mdns=fake, ssdp=fake)  # enabled flags default False
    asyncio.run(svc.scan_once())
    assert fake.calls == 0
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type != "camera"   # signal never applied


def test_mdns_signal_folds_into_recognized_device():
    fake = _FakeCollector({"192.168.0.23": {"device_type": "camera", "make": "Wyze"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   mdns_enabled=True, mdns=fake)
    asyncio.run(svc.scan_once())
    assert fake.calls == 1
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type == "camera"
    assert dev.make == "Wyze"
    # M1-style cap: a lone self-description signal is "medium", never "high".
    assert dev.type_confidence == "medium"
    assert dev.sources[0]["signal"] == "bonjour"   # highest-weight winner


def test_ssdp_signal_folds_into_recognized_device():
    fake = _FakeCollector({"192.168.0.42": {"device_type": "router"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   ssdp_enabled=True, ssdp=fake)
    asyncio.run(svc.scan_once())
    dev = next(d for d in svc.latest_inventory() if d.mac == "de:ad:be:ef:00:01")
    assert dev.device_type == "router"
    assert dev.type_confidence == "medium"


def test_collector_signal_for_unmapped_ip_is_dropped_not_invented():
    # An IP no ARP entry resolved this cycle has nothing to attach to.
    fake = _FakeCollector({"10.0.0.99": {"device_type": "camera"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   mdns_enabled=True, mdns=fake)
    devices = asyncio.run(svc.scan_once())   # must not raise / must not add a phantom device
    assert {d.mac for d in devices} == {
        "a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01",
    }


def test_collector_exception_is_tolerated_scan_still_completes():
    fake = _FakeCollector(boom=True)
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   mdns_enabled=True, ssdp_enabled=True, mdns=fake, ssdp=fake)
    devices = asyncio.run(svc.scan_once())   # must not raise despite both collectors exploding
    assert devices


def test_both_collectors_run_concurrently_and_combine_on_one_device():
    # mDNS (bonjour) + SSDP (upnp) are BOTH the `self_report` evidence family
    # (audit fix #2) -- a single rogue LAN host could answer both mDNS and
    # SSDP itself, so two agreeing self-descriptions must NOT forge "high" on
    # their own (that was the exact defect the family-gated consensus bump
    # closes). Both collectors still run concurrently and both signals still
    # fold onto the same device -- only the confidence claim is corrected.
    mdns = _FakeCollector({"192.168.0.23": {"device_type": "camera"}})
    ssdp = _FakeCollector({"192.168.0.23": {"device_type": "camera", "os": "Linux"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   mdns_enabled=True, ssdp_enabled=True, mdns=mdns, ssdp=ssdp)
    asyncio.run(svc.scan_once())
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type == "camera"
    assert dev.type_confidence == "medium"
    assert dev.os == "Linux"
    assert {"bonjour", "upnp"} <= {s["signal"] for s in dev.sources}


# ---- NetBIOS/SNMP active probes + DHCP fingerprint wiring (collectors-lote2) --

import logging
import struct

from wavr.sources.netbios import _encode_nbname
from wavr.sources.snmp import _encode_oid


def _nb_name_entry(name: str, suffix: int, is_group: bool = False) -> bytes:
    padded = name.ljust(15)[:15].encode("ascii")
    flags = 0x8000 if is_group else 0x0000
    return padded + bytes([suffix]) + struct.pack(">H", flags)


def _nbstat_response(entries: list[bytes], mac: bytes = b"\xaa\xbb\xcc\x00\x11\x22") -> bytes:
    header = struct.pack(">HHHHHH", 0x1337, 0x8400, 0, 1, 0, 0)
    encoded_name = _encode_nbname(b"*" + b"\x00" * 15)
    rr_name = bytes([len(encoded_name)]) + encoded_name + b"\x00"
    rdata = bytes([len(entries)]) + b"".join(entries) + mac
    rr = rr_name + struct.pack(">HHIH", 0x21, 1, 0, len(rdata)) + rdata
    return header + rr


_WINDOWS_PC_RESPONSE = _nbstat_response([_nb_name_entry("DESKTOP-A1B2C3", 0x00)])


def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = bytearray()
    while n:
        out.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(out)]) + bytes(out)


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(content)) + content


def _snmp_int(value: int) -> bytes:
    return _tlv(0x02, bytes([value]) if value else b"\x00")


def _snmp_octet(value: str) -> bytes:
    return _tlv(0x04, value.encode())


def _snmp_varbind(oid: str, value_tlv: bytes) -> bytes:
    return _tlv(0x30, _encode_oid(oid) + value_tlv)


def _snmp_get_response(community: str, varbinds: list[tuple[str, bytes]]) -> bytes:
    vb_bytes = b"".join(_snmp_varbind(oid, v) for oid, v in varbinds)
    varbind_list = _tlv(0x30, vb_bytes)
    pdu_body = _snmp_int(1) + _snmp_int(0) + _snmp_int(0) + varbind_list
    pdu = _tlv(0xA2, pdu_body)
    message_body = _snmp_int(0) + _snmp_octet(community) + pdu
    return _tlv(0x30, message_body)


_ROUTER_SNMP_RESPONSE = _snmp_get_response("public", [
    ("1.3.6.1.2.1.1.1.0", _snmp_octet("RouterOS 6.47.1")),
    ("1.3.6.1.2.1.1.2.0", _encode_oid("1.3.6.1.4.1.9.1.1")),
    ("1.3.6.1.2.1.1.5.0", _snmp_octet("core-router")),
    ("1.3.6.1.2.1.1.7.0", _snmp_int(78)),
])


async def test_netbios_signal_folds_into_recognized_device():
    async def prober(ip, request):
        return _WINDOWS_PC_RESPONSE

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   netbios_enabled=True, netbios_prober=prober)
    await svc.scan_once()
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type == "desktop"
    # M1-style cap: a lone self-description signal is "medium", never "high".
    assert dev.type_confidence == "medium"
    assert dev.sources[0]["signal"] == "netbios"


async def test_netbios_scope_known_only_restricts_targets():
    probed = []

    async def prober(ip, request):
        probed.append(ip)
        return _WINDOWS_PC_RESPONSE

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   netbios_enabled=True, netbios_scope_known_only=True,
                                   netbios_prober=prober)
    await svc.scan_once()
    assert probed == ["192.168.0.1"]   # only the allowlisted Apple host


async def test_netbios_off_by_default_prober_never_called():
    calls = []

    async def prober(ip, request):
        calls.append(ip)
        return _WINDOWS_PC_RESPONSE

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   netbios_prober=prober)   # netbios_enabled defaults False
    await svc.scan_once()
    assert calls == []


async def test_snmp_signal_folds_into_recognized_device():
    async def prober(ip, request):
        return _ROUTER_SNMP_RESPONSE

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   snmp_enabled=True, snmp_prober=prober)
    await svc.scan_once()
    dev = next(d for d in svc.latest_inventory() if d.mac == "de:ad:be:ef:00:01")
    assert dev.device_type == "router"
    assert dev.os == "RouterOS"
    assert dev.type_confidence == "medium"
    assert dev.sources[0]["signal"] == "snmp"


async def test_snmp_scope_known_only_restricts_targets():
    probed = []

    async def prober(ip, request):
        probed.append(ip)
        return _ROUTER_SNMP_RESPONSE

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   snmp_enabled=True, snmp_scope_known_only=True,
                                   snmp_prober=prober)
    await svc.scan_once()
    assert probed == ["192.168.0.1"]


async def test_snmp_community_is_never_logged_even_on_failure(monkeypatch, caplog):
    # Regression guard for the "no-log-community" mitigation (audit fix #6
    # rewrite): the ORIGINAL version of this test raised inside
    # `SNMPCollector._one`, which has its own internal try/except (plus
    # `collect()`'s own `contextlib.suppress`) -- so the exception never
    # escaped to `netinventory_service`'s except branch, the assertion passed
    # vacuously, and it would have kept passing even if a future refactor
    # added a leaking log call there. Monkeypatching the whole
    # `SNMPCollector` class to raise straight out of `collect()` forces that
    # branch to actually run, so this now proves BOTH halves: the warning
    # WAS logged (the guarded branch executed) AND the community is still
    # absent from it.
    class _RaisingSNMPCollector:
        def __init__(self, *args, **kwargs):
            pass

        async def collect(self):
            raise RuntimeError("snmp probe blew up")

    monkeypatch.setattr("wavr.sources.snmp.SNMPCollector", _RaisingSNMPCollector)

    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   snmp_enabled=True, snmp_community="s3cr3t-community")
    with caplog.at_level(logging.WARNING):
        devices = await svc.scan_once()   # must not raise despite the collector exploding
    assert devices   # scan still completes
    assert "snmp collector failed" in caplog.text   # proves the except branch actually ran
    assert "s3cr3t-community" not in caplog.text


def test_snmp_non_default_community_with_wide_scope_warns_at_construction(caplog):
    # Audit fix #4: a non-default community is a credential for the
    # operator's own gear -- widening the probe scope to every ARP-discovered
    # host (scope=all) means it's sent in SNMPv1 cleartext to hosts the
    # operator may not own. Must warn (never logging the community itself).
    with caplog.at_level(logging.WARNING):
        NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                 snmp_enabled=True, snmp_community="monitoring-secret",
                                 snmp_scope_known_only=False)
    assert "widened" in caplog.text.lower()
    assert "monitoring-secret" not in caplog.text


def test_snmp_default_community_with_wide_scope_does_not_warn(caplog):
    # The default "public" community is not a secret -- no warning needed.
    with caplog.at_level(logging.WARNING):
        NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                 snmp_enabled=True, snmp_scope_known_only=False)
    assert caplog.text == ""


def test_snmp_non_default_community_with_known_only_scope_does_not_warn(caplog):
    # Scope already narrowed to the known-MAC allowlist -- no cross-subnet
    # exposure, so no warning needed even with a non-default community.
    with caplog.at_level(logging.WARNING):
        NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                 snmp_enabled=True, snmp_community="monitoring-secret",
                                 snmp_scope_known_only=True)
    assert caplog.text == ""


async def test_dhcp_fp_signal_folds_into_recognized_device_by_mac():
    # DHCP-fp is already MAC-keyed (parsed from chaddr) -- no ip_to_mac lookup.
    # Uses "de:ad:be:ef:00:01" (truly-unknown OUI in WINDOWS_ARP, only otherwise
    # backed by the low-confidence randomized-MAC heuristic) rather than the
    # Espressif-OUI MAC used by the mdns/ssdp tests above -- audit fix #3
    # down-weights `dhcp` below `oui`, so on an OUI-recognized MAC the OUI
    # guess would now correctly win (see
    # test_dhcp_fp_loses_to_a_recognized_oui_vendor below).
    fake = _FakeCollector({"de:ad:be:ef:00:01": {"device_type": "router", "os": "Linux"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   dhcp_fp_enabled=True, dhcp_fp=fake)
    await svc.scan_once()
    assert fake.calls == 1
    dev = next(d for d in svc.latest_inventory() if d.mac == "de:ad:be:ef:00:01")
    assert dev.device_type == "router"
    assert dev.os == "Linux"
    assert dev.sources[0]["signal"] == "dhcp"
    # Audit fix #3: chaddr-keyed dhcp evidence is flagged unverified.
    assert dev.sources[0]["unverified"] is True


async def test_dhcp_fp_loses_to_a_recognized_oui_vendor():
    # Audit fix #3: dhcp-fp is keyed by the unauthenticated packet `chaddr` --
    # an off-path attacker can broadcast one spoofed-chaddr DISCOVER naming a
    # currently-present device's MAC to inject a device_type. Down-weighting
    # it below `oui` means a recognized vendor OUI now wins the disagreement
    # instead of trusting the chaddr-derived claim.
    fake = _FakeCollector({"24:0a:c4:aa:bb:cc": {"device_type": "router"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0,
                                   dhcp_fp_enabled=True, dhcp_fp=fake)
    await svc.scan_once()
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type != "router"   # Espressif OUI (esp_dev) wins, not the spoofable dhcp claim


async def test_dhcp_fp_off_by_default_never_invoked():
    fake = _FakeCollector({"24:0a:c4:aa:bb:cc": {"device_type": "router"}})
    svc = NetworkInventoryService(known_macs=KNOWN, scan=_fake_scan, interval=0, dhcp_fp=fake)
    await svc.scan_once()
    assert fake.calls == 0
    dev = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert dev.device_type != "router"
