"""App-level gating for PUT /api/inventory/name (Feature A): mirrors
test_camera_api.py's X-Wavr-Local / CSRF coverage for the camera routes."""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta


def _client(seed_name=None):
    dm = DeviceMeta(":memory:")
    if seed_name:
        dm.set_name(*seed_name)
    app = create_app(
        sources=[],                       # no default sources -> isolate this route's behavior
        camera_store=CameraStore(":memory:"),
        device_meta=dm,
    )
    return TestClient(app, headers={"X-Wavr-Local": "1"}), dm


def test_put_name_requires_local_header():
    dm = DeviceMeta(":memory:")
    app = create_app(sources=[], camera_store=CameraStore(":memory:"), device_meta=dm)
    with TestClient(app) as c:                    # no X-Wavr-Local
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "TV"})
    assert r.status_code == 403


def test_put_name_persists_with_header():
    c, dm = _client()
    with c:
        r = c.put("/api/inventory/name", json={"mac": "A4-83-E7-11-22-33", "name": "Sala TV"})
    assert r.status_code == 200
    body = r.json()
    assert body["mac"] == "a4:83:e7:11:22:33"
    assert body["name"] == "Sala TV"
    assert dm.get("a4:83:e7:11:22:33")["name"] == "Sala TV"


def test_put_name_rejects_invalid_mac():
    c, _ = _client()
    with c:
        r = c.put("/api/inventory/name", json={"mac": "not-a-mac", "name": "x"})
    assert r.status_code == 400


def test_put_name_rejects_empty_name():
    c, _ = _client()
    with c:
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "   \x00 "})
    assert r.status_code == 400


def test_put_name_rejects_over_max_len():
    c, _ = _client()
    with c:
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "x" * 65})
    assert r.status_code == 400


def test_put_name_overwrites_existing_name():
    c, dm = _client(seed_name=("a4:83:e7:11:22:33", "Old Name"))
    with c:
        r = c.put("/api/inventory/name", json={"mac": "a4:83:e7:11:22:33", "name": "New Name"})
    assert r.status_code == 200
    assert r.json()["name"] == "New Name"
    assert dm.get("a4:83:e7:11:22:33")["name"] == "New Name"


def test_get_inventory_reachable_and_shaped_even_with_no_devices():
    c, _ = _client()
    with c:
        r = c.get("/api/inventory")
    assert r.status_code == 200
    assert r.json() == {"devices": []}   # no scan has run yet -- shape still correct
