"""User device-type pin: DeviceMeta column (migration-safe), the
PUT /api/inventory/type route (CSRF-gated like /name), pin flow into the scan
loop, and the enriched /api/inventory identity fields."""
import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_inventory import _device_view, build_inventory_router
from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta
from wavr.netinventory import Device
from wavr.netinventory_service import NetworkInventoryService

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
"""


async def _fake_scan() -> str:
    return WINDOWS_ARP


def _service(**kw) -> NetworkInventoryService:
    return NetworkInventoryService(scan=_fake_scan, interval=0, **kw)


def _router_client(device_meta=None) -> TestClient:
    svc = _service(device_meta=device_meta)
    asyncio.run(svc.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(svc, device_meta=device_meta))
    return TestClient(app)


# ---- DeviceMeta: pin store + migration ----------------------------------------

def test_set_type_persists_and_normalizes(tmp_path):
    s = DeviceMeta(str(tmp_path / "t.db"))
    entry = s.set_type("A4-83-E7-11-22-33", " Camera ")
    assert entry["mac"] == "a4:83:e7:11:22:33"
    assert entry["device_type"] == "camera"
    assert s.type_pins() == {"a4:83:e7:11:22:33": "camera"}


def test_set_type_rejects_values_outside_taxonomy(tmp_path):
    s = DeviceMeta(str(tmp_path / "t.db"))
    with pytest.raises(ValueError):
        s.set_type("a4:83:e7:11:22:33", "spaceship")


def test_set_type_none_or_empty_clears_the_pin(tmp_path):
    s = DeviceMeta(str(tmp_path / "t.db"))
    s.set_type("a4:83:e7:11:22:33", "camera")
    assert s.set_type("a4:83:e7:11:22:33", None)["device_type"] is None
    s.set_type("a4:83:e7:11:22:33", "tv")
    assert s.set_type("a4:83:e7:11:22:33", "  ")["device_type"] is None
    assert s.type_pins() == {}


def test_pin_before_first_sighting_still_records_first_seen(tmp_path):
    # pinning creates the row with NULL timestamps; the first real sighting
    # must still set first_seen (seen() backfills a NULL first_seen).
    s = DeviceMeta(str(tmp_path / "t.db"))
    s.set_type("a4:83:e7:11:22:33", "camera")
    assert s.get("a4:83:e7:11:22:33")["first_seen"] is None
    s.seen("a4:83:e7:11:22:33")
    entry = s.get("a4:83:e7:11:22:33")
    assert entry["first_seen"] is not None
    assert entry["device_type"] == "camera"      # pin survives the sighting


def test_set_type_never_touches_name_or_seen(tmp_path):
    s = DeviceMeta(str(tmp_path / "t.db"))
    s.seen("a4:83:e7:11:22:33")
    s.set_name("a4:83:e7:11:22:33", "Hall cam")
    before = s.get("a4:83:e7:11:22:33")
    s.set_type("a4:83:e7:11:22:33", "camera")
    after = s.get("a4:83:e7:11:22:33")
    assert after["name"] == "Hall cam"
    assert after["first_seen"] == before["first_seen"]
    assert after["last_seen"] == before["last_seen"]


def test_migration_adds_device_type_column_to_legacy_db(tmp_path):
    # a DB created by the pre-pin schema (no device_type column)
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE device_meta (
        mac TEXT PRIMARY KEY, name TEXT, first_seen TEXT, last_seen TEXT)""")
    conn.execute("INSERT INTO device_meta VALUES ('a4:83:e7:11:22:33', 'TV', 't0', 't1')")
    conn.commit()
    conn.close()

    s = DeviceMeta(path)                      # must migrate, not crash
    entry = s.get("a4:83:e7:11:22:33")
    assert entry["name"] == "TV" and entry["device_type"] is None
    assert s.set_type("a4:83:e7:11:22:33", "tv")["device_type"] == "tv"


# ---- pin flows into the scan loop (highest recog precedence) -------------------

def test_pin_wins_recognition_on_next_scan():
    dm = DeviceMeta(":memory:")
    dm.set_type("24:0a:c4:aa:bb:cc", "smart_plug")   # an ESP-based plug, says the owner
    svc = _service(device_meta=dm)
    devices = asyncio.run(svc.scan_once())
    esp = next(d for d in devices if d.mac == "24:0a:c4:aa:bb:cc")
    assert esp.device_type == "smart_plug"
    assert esp.type_confidence == "high"
    assert esp.sources[0]["signal"] == "user_pin"


def test_broken_pin_store_never_breaks_scanning():
    class LegacyMeta:                # pre-pin store: has seen(), no type_pins()
        def seen(self, mac):
            pass
    svc = _service(device_meta=LegacyMeta())
    assert asyncio.run(svc.scan_once())       # must not raise


# ---- PUT /api/inventory/type (router level) -------------------------------------

def test_put_type_persists_and_returns_entry():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        r = c.put("/api/inventory/type",
                  json={"mac": "A4-83-E7-11-22-33", "device_type": "camera"})
    assert r.status_code == 200
    assert r.json()["device_type"] == "camera"
    assert dm.get("a4:83:e7:11:22:33")["device_type"] == "camera"


def test_put_type_rejects_invalid_mac_and_type():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        assert c.put("/api/inventory/type",
                     json={"mac": "nope", "device_type": "camera"}).status_code == 400
        assert c.put("/api/inventory/type",
                     json={"mac": "a4:83:e7:11:22:33",
                           "device_type": "spaceship"}).status_code == 400


def test_put_type_clears_with_null():
    dm = DeviceMeta(":memory:")
    dm.set_type("a4:83:e7:11:22:33", "camera")
    with _router_client(device_meta=dm) as c:
        r = c.put("/api/inventory/type",
                  json={"mac": "a4:83:e7:11:22:33", "device_type": None})
    assert r.status_code == 200
    assert r.json()["device_type"] is None


def test_put_type_absent_without_device_meta():
    with _router_client() as c:
        r = c.put("/api/inventory/type",
                  json={"mac": "a4:83:e7:11:22:33", "device_type": "camera"})
    assert r.status_code == 404


# ---- pin reflected instantly on GET /api/inventory -------------------------------

def test_pin_overrides_inventory_view_between_scans():
    dm = DeviceMeta(":memory:")
    svc = _service(device_meta=dm)
    asyncio.run(svc.scan_once())              # scan BEFORE the pin exists
    dm.set_type("a4:83:e7:11:22:33", "laptop")
    app = FastAPI()
    app.include_router(build_inventory_router(svc, device_meta=dm))
    with TestClient(app) as c:
        r = c.get("/api/inventory")
    apple = next(d for d in r.json()["devices"] if d["mac"] == "a4:83:e7:11:22:33")
    assert apple["device_type"] == "laptop"       # pin wins without a rescan
    assert apple["type_confidence"] == "high"


def test_inventory_view_backward_compatible_plus_additive_fields():
    dm = DeviceMeta(":memory:")
    with _router_client(device_meta=dm) as c:
        r = c.get("/api/inventory")
    apple = next(d for d in r.json()["devices"] if d["mac"] == "a4:83:e7:11:22:33")
    # every pre-existing field is still there, unchanged in name
    assert {"mac", "ip", "vendor", "device_type", "known",
            "name", "first_seen", "last_seen"} <= set(apple)
    # additive identity fields
    assert apple["type_confidence"] in ("low", "medium", "high")
    assert apple["make"] == "Apple"
    assert isinstance(apple["sources"], list) and apple["sources"]
    assert "risks" not in apple                   # port scan off -> absent, as before
    assert "open_ports" not in apple
    assert "hostname" not in apple                 # no self-announced/PTR name -> absent, as before


def test_inventory_view_exposes_hostname_when_populated():
    # Confirmed-gap fix (2f57435): Device.hostname is now filled from a
    # self-announced mDNS/SSDP/SNMP/NetBIOS name even without the opt-in PTR
    # resolver, but the /api/inventory view never surfaced it -- additive.
    d = Device(mac="a4:83:e7:11:22:33", ip="192.168.0.1", vendor="Apple",
               device_type="speaker", known=True, hostname="Living-Room-HomePod")
    view = _device_view(d)
    assert view["hostname"] == "Living-Room-HomePod"
    # every pre-existing field still present, unchanged
    assert {"mac", "ip", "vendor", "device_type", "type_confidence", "known"} <= set(view)


# ---- app-level CSRF gating (mirrors PUT /api/inventory/name) ---------------------

def test_app_put_type_requires_local_header():
    dm = DeviceMeta(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"), device_meta=dm)
    with TestClient(app) as c:                    # no X-Wavr-Local header
        r = c.put("/api/inventory/type",
                  json={"mac": "a4:83:e7:11:22:33", "device_type": "camera"})
    assert r.status_code == 403


def test_app_put_type_works_with_local_header():
    dm = DeviceMeta(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"), device_meta=dm)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.put("/api/inventory/type",
                  json={"mac": "a4:83:e7:11:22:33", "device_type": "camera"})
    assert r.status_code == 200
    assert dm.get("a4:83:e7:11:22:33")["device_type"] == "camera"
