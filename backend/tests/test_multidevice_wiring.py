"""Integration test for the multi-device wiring in create_app (ADR-0006).

The device/auth MODULES are unit-tested in test_multidevice.py. This covers the
create_app wiring: with WAVR_MULTIDEVICE on, the routers exist, the loopback peer is
still 'root' (unchanged CSRF-gated control), and default-off is verified elsewhere by
the whole existing suite staying green.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app


@pytest.fixture
def md_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    with TestClient(create_app()) as c:
        yield c


def test_loopback_is_root_gets_work(md_client):
    assert md_client.get("/api/state").status_code == 200
    assert md_client.get("/api/house").status_code == 200


def test_loopback_state_change_still_needs_csrf(md_client):
    # loopback == root, but the CSRF header guard is preserved (drive-by defense)
    assert md_client.post("/api/system/toggle", json={"on": True}).status_code == 403
    ok = md_client.post("/api/system/toggle", json={"on": False},
                        headers={"X-Wavr-Local": "1"})
    assert ok.status_code == 200


def test_device_routers_are_wired(md_client):
    # /api/pair exists (validation error on empty body, not a 404)
    assert md_client.post("/api/pair", json={}).status_code in (400, 422)
    # loopback root can list devices (empty to start; shape is {"devices": [...]})
    r = md_client.get("/api/devices")
    assert r.status_code == 200 and r.json() == {"devices": []}


def test_multidevice_off_has_no_device_routes(tmp_path, monkeypatch):
    # default OFF: the pairing route is not mounted at all
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "off.db"))
    with TestClient(create_app()) as c:
        assert c.post("/api/pair", json={}).status_code == 404
