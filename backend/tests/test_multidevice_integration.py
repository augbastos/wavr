"""End-to-end integration of the multi-device auth wiring in create_app (ADR-0006).

Uses TestClient's `client=(host, port)` to forge a non-loopback LAN peer (same technique
as test_app.py), and monkeypatches `_local_ipv4` so a fixed "192.168.1.x" is in-subnet.
Exercises the REAL middleware + route guards, verifying the security-audit fixes:
C1 (device-route role gate), M2 (WS/HTTP subnet), M3 (pair reachability), revocation.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    # In-memory storage/cameras so only the device store touches the temp file (no lock);
    # sources registered but not started (no lifespan) — auth doesn't need them.
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def _pair(app, role="user"):
    """Central (loopback root) mints a code; a forged in-subnet LAN peer redeems it."""
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_lan_peer_pairs_and_can_view(app):
    peer, auth = _pair(app, "user")
    assert peer.get("/api/state", headers=auth).status_code == 200


def test_lan_peer_without_token_is_forbidden(app):
    peer = TestClient(app, client=("192.168.1.50", 12345))
    assert peer.get("/api/state").status_code == 403


def test_user_role_cannot_change_state(app):
    peer, auth = _pair(app, "user")
    assert peer.post("/api/system/toggle", json={"on": True}, headers=auth).status_code == 403


def test_user_role_cannot_manage_devices(app):   # audit C1
    peer, auth = _pair(app, "user")
    assert peer.get("/api/devices", headers=auth).status_code == 403
    assert peer.delete("/api/devices/anything", headers=auth).status_code == 403


def test_central_role_can_change_state(app):
    peer, auth = _pair(app, "central")
    assert peer.post("/api/system/toggle", json={"on": False}, headers=auth).status_code == 200


def test_out_of_subnet_peer_is_forbidden(app):   # audit M2 / subnet
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    assert outsider.get("/api/state").status_code == 403
    # /api/pair onboarding also requires in-subnet
    assert outsider.post("/api/pair", json={"code": "1", "device_name": "x"}).status_code == 403


def test_revoked_token_is_denied(app):
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    assert peer.get("/api/state", headers=auth).status_code == 403
