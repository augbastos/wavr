"""Tests for the HA -> Wavr device-registry import (A4.1) + the recog `ha`
signal (A4.0). Everything runs OFFLINE: the WS transport is injected, so no LAN /
HA / cloud is ever touched.

Proves the load-bearing invariants:
  * the recog `ha` signal caps at MEDIUM alone (self_report family) and only
    reaches high via a 2nd INDEPENDENT (non-self_report) family; a user pin wins;
  * the import maps HA devices to Wavr taxonomy + make/model/os, correlates by
    MAC, matches the catalog, and feeds recog through the store;
  * SSRF is impossible -- fetch_registry only ever contacts a URL derived from
    the configured ha_url, never anything from the HA payload;
  * the HA long-lived token never appears in a response, error, or log;
  * malformed HA payloads never crash (never-raise parse);
  * dry_run writes nothing; HA-not-configured -> 400 with no write; disabled -> 403.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.ha_client import WavrHAError
from wavr.ha_import import (fetch_registry, import_devices, map_device,
                            _ws_url)
from wavr.ha_import_store import HAImportStore
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta
from wavr.netinventory_service import NetworkInventoryService
from wavr.recog import recognize
from wavr.sources.simulated import SimulatedSource


# --- canned HA device + entity registry (what the WS API returns) ------------------

HA_DEVICES = [
    {"id": "d1", "manufacturer": "Aqara",
     "model": "Door and Window Sensor P2", "name": "Front Door",
     "sw_version": "1.2.3", "area_id": "hall",
     "connections": [["mac", "AA:BB:CC:DD:EE:01"]]},
    {"id": "d2", "manufacturer": "Reolink", "model": "RLC-810A",
     "name": "Driveway Cam", "sw_version": "3.0", "area_id": "outside",
     "connections": [["mac", "aa:bb:cc:dd:ee:02"]]},
    # No LAN MAC (zigbee-only) -> can't feed per-MAC recog -> skipped.
    {"id": "d3", "manufacturer": "NoMac Corp", "model": "Zigbee Bulb",
     "name": "Lamp", "connections": [["zigbee", "0x00124b00"]]},
]
HA_ENTITIES = [
    {"entity_id": "binary_sensor.front_door", "device_id": "d1"},
    {"entity_id": "camera.driveway", "device_id": "d2"},
    {"entity_id": "light.lamp", "device_id": "d3"},
]
REGISTRY = {"devices": HA_DEVICES, "entities": HA_ENTITIES}


def _ws_fn(registry, spy=None):
    async def fn(ws_url, token, timeout):
        if spy is not None:
            spy.append({"ws_url": ws_url, "token": token})
        return registry
    return fn


# =========================== A4.0 -- recog `ha` signal ==============================

def test_ha_signal_alone_caps_at_medium():
    ident = recognize({"ha": {"device_type": "camera", "make": "Reolink",
                              "model": "RLC-810A", "os": "3.0"}})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"          # never "high" on its own
    assert ident.make == "Reolink"
    assert ident.model == "RLC-810A"
    assert ident.os == "3.0"


def test_ha_two_self_reports_stay_medium():
    # ha + mdns(bonjour) are BOTH the self_report family -> the family-gated
    # consensus bump must NOT fire (two self-descriptions can't forge high).
    ident = recognize({"ha": {"device_type": "camera"},
                       "bonjour": {"device_type": "camera"}})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"


def test_ha_plus_independent_family_reaches_high():
    # ha (self_report) + an OBSERVED family signal (a Wavr-run port hint) agreeing
    # spans 2 families -> the consensus bump reaches high.
    ident = recognize({"ha": {"device_type": "camera"}, "open_ports": [554]})
    assert ident.device_type == "camera"
    assert ident.confidence == "high"


def test_user_pin_still_wins_over_ha():
    ident = recognize({"user_pin": "nas", "ha": {"device_type": "camera"}})
    assert ident.device_type == "nas"
    assert ident.confidence == "high"


def test_ha_make_model_os_ranked_below_bonjour_above_snmp():
    ident = recognize({
        "bonjour": {"device_type": "tv", "make": "Sony"},
        "ha": {"device_type": "camera", "make": "Reolink", "model": "RLC-810A"},
        "snmp": {"make": "SnmpCorp"},
    })
    assert ident.device_type == "tv"            # bonjour outranks ha
    assert ident.make == "Sony"                 # bonjour make wins
    # ha still fills a field bonjour lacks (model), ranked above snmp
    assert ident.model == "RLC-810A"


def test_ha_fills_make_model_os_when_no_higher_signal():
    ident = recognize({"ha": {"make": "Reolink", "model": "RLC-810A", "os": "3.0"}})
    assert ident.make == "Reolink"
    assert ident.model == "RLC-810A"
    assert ident.os == "3.0"


# =========================== A4.1 -- mapping + import ================================

def test_map_device_taxonomy_make_model_os_mac():
    view = map_device(HA_DEVICES[0], [HA_ENTITIES[0]], catalog=[])
    assert view["mac"] == "aa:bb:cc:dd:ee:01"
    assert view["make"] == "Aqara"
    assert view["model"] == "Door and Window Sensor P2"
    assert view["os"] == "1.2.3"
    assert view["device_type"] == "iot_sensor"
    assert view["area"] == "hall"
    assert view["entity_count"] == 1


def test_map_device_camera_domain_and_pattern():
    view = map_device(HA_DEVICES[1], [HA_ENTITIES[1]], catalog=[])
    assert view["mac"] == "aa:bb:cc:dd:ee:02"
    assert view["device_type"] == "camera"


def test_catalog_match_hits_via_home_assistant_entry():
    catalog = [
        {"id": "aqara-p2", "brand": "Aqara",
         "name": "Door and Window Sensor P2", "status": "via-home-assistant"},
        {"id": "noise", "brand": "Aqara", "name": "Vacuum X1",
         "status": "addable-now"},
    ]
    view = map_device(HA_DEVICES[0], [HA_ENTITIES[0]], catalog=catalog)
    assert view.get("catalog_match") == {"id": "aqara-p2",
                                         "name": "Door and Window Sensor P2"}


def test_import_devices_counts_and_persist():
    store = HAImportStore(":memory:")
    summary = import_devices(REGISTRY, catalog=[], store=store, dry_run=False)
    assert summary["imported"] == 2
    assert summary["matched_to_lan"] == 2
    assert summary["unmatched"] == 1
    assert {d["mac"] for d in summary["devices"]} == {
        "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    assert summary["skipped"] == [
        {"reason": "no MAC in HA registry", "name": "Zigbee Bulb"}]
    # persisted -> feeds recog as the `ha` signal
    sigs = store.signals()
    assert sigs["aa:bb:cc:dd:ee:01"]["device_type"] == "iot_sensor"
    assert sigs["aa:bb:cc:dd:ee:01"]["make"] == "Aqara"
    ident = recognize({"mac": "aa:bb:cc:dd:ee:01", "vendor": "unknown",
                       "ha": sigs["aa:bb:cc:dd:ee:01"]})
    assert ident.device_type == "iot_sensor"
    assert ident.confidence == "medium"


def test_unresolved_type_stored_as_null_not_unknown():
    # A device whose type can't be resolved must NOT persist "unknown" (recog
    # weights `ha` at 0.82 -> an "unknown" opinion would mask hostname/port
    # verdicts). make/model still enrich.
    reg = {"devices": [{"id": "d4", "manufacturer": "Generic", "model": "Widget",
                        "name": "Thing", "connections": [["mac", "aa:bb:cc:dd:ee:03"]]}],
           "entities": [{"entity_id": "button.thing", "device_id": "d4"}]}
    store = HAImportStore(":memory:")
    import_devices(reg, catalog=[], store=store)
    sig = store.signals()["aa:bb:cc:dd:ee:03"]
    assert "device_type" not in sig               # not persisted as "unknown"
    assert sig["make"] == "Generic" and sig["model"] == "Widget"
    # a hostname verdict still wins because `ha` carries no type opinion here
    ident = recognize({"mac": "aa:bb:cc:dd:ee:03", "vendor": "unknown",
                       "hostname": "reolink-nvr", "ha": sig})
    assert ident.device_type == "camera"
    assert ident.make == "Generic"                # ha still fills make


def test_import_dry_run_writes_nothing():
    store = HAImportStore(":memory:")
    summary = import_devices(REGISTRY, catalog=[], store=store, dry_run=True)
    assert summary["imported"] == 2
    assert summary["dry_run"] is True
    assert store.count() == 0                    # nothing persisted
    assert store.signals() == {}


def test_import_never_raises_on_malformed():
    # None/int rows, a mac-less dict, and a non-list entities blob must all
    # degrade, never crash (never-raise parse).
    bad = {"devices": [None, 42, {"id": "x"}], "entities": "garbage"}
    summary = import_devices(bad, catalog=[], store=None)
    assert summary["imported"] == 0
    assert summary["unmatched"] == 1             # the mac-less dict
    assert any(s["reason"] == "malformed registry entry" for s in summary["skipped"])


def test_import_bad_registry_shape_is_empty():
    assert import_devices("not a dict", catalog=[], store=None)["imported"] == 0
    assert import_devices({}, catalog=[], store=None)["imported"] == 0


# =========================== SSRF + token safety ====================================

async def test_fetch_registry_only_contacts_configured_host():
    spy = []
    # a hostile registry embedding an external "url" must NEVER be contacted --
    # fetch_registry only calls the transport with the derived, configured URL.
    hostile = {"devices": [{"id": "d", "manufacturer": "X",
                            "connections": [["mac", "aa:bb:cc:dd:ee:99"]]}],
               "entities": []}
    reg = await fetch_registry("http://homeassistant.local:8123", "TOK",
                               ws_fn=_ws_fn(hostile, spy=spy))
    assert len(spy) == 1                          # exactly one call, to the HA host
    assert spy[0]["ws_url"] == "ws://homeassistant.local:8123/api/websocket"
    assert reg["devices"][0]["connections"][0][1] == "aa:bb:cc:dd:ee:99"


async def test_fetch_registry_https_becomes_wss():
    spy = []
    await fetch_registry("https://ha.local:8123/", "TOK", ws_fn=_ws_fn(REGISTRY, spy=spy))
    assert spy[0]["ws_url"] == "wss://ha.local:8123/api/websocket"


async def test_fetch_registry_refuses_non_http_scheme():
    spy = []
    with pytest.raises(WavrHAError):
        await fetch_registry("ftp://evil.example", "TOK", ws_fn=_ws_fn(REGISTRY, spy=spy))
    assert spy == []                              # transport never called


def test_ws_url_derivation():
    assert _ws_url("http://ha:8123") == "ws://ha:8123/api/websocket"
    assert _ws_url("https://ha:8123/") == "wss://ha:8123/api/websocket"
    with pytest.raises(WavrHAError):
        _ws_url("file:///etc/passwd")


async def test_token_never_in_error_message():
    async def boom(ws_url, token, timeout):
        raise OSError("connection refused")
    with pytest.raises(WavrHAError) as ei:
        await fetch_registry("http://ha.local:8123", "SUPER-SECRET-TOKEN", ws_fn=boom)
    assert "SUPER-SECRET-TOKEN" not in str(ei.value)
    assert "SUPER-SECRET-TOKEN" not in repr(ei.value)


async def test_fetch_registry_malformed_shape_yields_empty():
    async def weird(ws_url, token, timeout):
        return "not a dict"
    assert await fetch_registry("http://ha", "t", ws_fn=weird) == {
        "devices": [], "entities": []}


# =========================== recog service integration ==============================

async def test_scan_folds_imported_ha_identity():
    store = HAImportStore(":memory:")
    import_devices(REGISTRY, catalog=[], store=store)

    arp = ("  192.168.0.1   A4-83-E7-00-00-01  dynamic\n"
           "  192.168.0.5   AA-BB-CC-DD-EE-01  dynamic\n")

    async def fake_scan():
        return arp

    svc = NetworkInventoryService(known_macs=set(), scan=fake_scan, interval=0,
                                  ha_store=store)
    devices = await svc.scan_once()
    by_mac = {d.mac: d for d in devices}
    d = by_mac["aa:bb:cc:dd:ee:01"]
    assert d.device_type == "iot_sensor"          # HA-imported identity applied
    assert d.type_confidence == "medium"
    assert d.make == "Aqara"


async def test_scan_tolerates_broken_ha_store():
    class BoomStore:
        def signals(self):
            raise RuntimeError("db gone")

    async def fake_scan():
        return "  192.168.0.5   AA-BB-CC-DD-EE-01  dynamic\n"

    svc = NetworkInventoryService(known_macs=set(), scan=fake_scan, interval=0,
                                  ha_store=BoomStore())
    devices = await svc.scan_once()               # must not raise
    assert devices[0].mac == "aa:bb:cc:dd:ee:01"


# =========================== endpoint ==============================================

def _client(monkeypatch, *, ha_url="http://ha.local:8123", ha_token="SECRET-TOK-123",
            ha_import="1"):
    monkeypatch.setenv("WAVR_HA_URL", ha_url)
    monkeypatch.setenv("WAVR_HA_TOKEN", ha_token)
    monkeypatch.setenv("WAVR_HA_IMPORT", ha_import)
    store = HAImportStore(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=0.01), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"),
        device_meta=DeviceMeta(":memory:"),
        health_resolvers={},
        ha_import_store=store)
    return TestClient(app), store


def test_endpoint_imports_and_hides_token(monkeypatch):
    async def fake_fetch(base_url, token, ws_fn=None, timeout=5.0):
        assert base_url == "http://ha.local:8123"
        assert token == "SECRET-TOK-123"          # real token reaches the transport
        return REGISTRY
    monkeypatch.setattr("wavr.app.fetch_registry", fake_fetch)
    client, store = _client(monkeypatch)
    with client:
        r = client.post("/api/ha/import", json={"dry_run": False},
                        headers={"X-Wavr-Local": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 2 and body["unmatched"] == 1
    assert "SECRET-TOK-123" not in r.text          # token never echoed
    assert store.count() == 2                       # persisted


def test_endpoint_dry_run_no_write(monkeypatch):
    async def fake_fetch(base_url, token, ws_fn=None, timeout=5.0):
        return REGISTRY
    monkeypatch.setattr("wavr.app.fetch_registry", fake_fetch)
    client, store = _client(monkeypatch)
    with client:
        r = client.post("/api/ha/import", json={"dry_run": True},
                        headers={"X-Wavr-Local": "1"})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert store.count() == 0


def test_endpoint_400_when_ha_not_configured(monkeypatch):
    client, store = _client(monkeypatch, ha_url="", ha_token="")
    with client:
        r = client.post("/api/ha/import", json={}, headers={"X-Wavr-Local": "1"})
    assert r.status_code == 400
    assert store.count() == 0


def test_endpoint_403_when_disabled(monkeypatch):
    client, store = _client(monkeypatch, ha_import="0")
    with client:
        r = client.post("/api/ha/import", json={}, headers={"X-Wavr-Local": "1"})
    assert r.status_code == 403


def test_endpoint_requires_csrf_header(monkeypatch):
    async def fake_fetch(base_url, token, ws_fn=None, timeout=5.0):
        return REGISTRY
    monkeypatch.setattr("wavr.app.fetch_registry", fake_fetch)
    client, store = _client(monkeypatch)
    with client:
        r = client.post("/api/ha/import", json={})   # no X-Wavr-Local header
    assert r.status_code == 403


def test_endpoint_502_on_ha_unreachable_no_token_leak(monkeypatch, caplog):
    async def boom(base_url, token, ws_fn=None, timeout=5.0):
        raise WavrHAError("Home Assistant registry unreachable at ws://ha.local:8123/api/websocket")
    monkeypatch.setattr("wavr.app.fetch_registry", boom)
    client, store = _client(monkeypatch)
    with client, caplog.at_level(logging.WARNING):
        r = client.post("/api/ha/import", json={}, headers={"X-Wavr-Local": "1"})
    assert r.status_code == 502
    assert "SECRET-TOK-123" not in r.text
    assert "SECRET-TOK-123" not in caplog.text
