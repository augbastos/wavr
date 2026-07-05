"""Post-pairing role change (promote/demote) — DeviceStore.set_role + the
POST /api/devices/{id}/role route (ADR-0006).

Lets an operator flip an already-paired device between the two grantable roles
('user' <-> 'central') without revoke+re-pair, so a fleet of many 'user' devices
and a few 'central' admins can be managed in place. The change touches ONLY the
role column — never tokens, never revocation — and inherits the exact same gates
as DELETE: router-level central/root role gate + the X-Wavr-Local CSRF guard.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.devices import DeviceStore
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header


# --------------------------------------------------------------------------- #
# DeviceStore.set_role: round-trip, unknown id, invalid role.
# --------------------------------------------------------------------------- #
def test_set_role_round_trip(tmp_path):
    store = DeviceStore(str(tmp_path / "devices.db"))
    device_id, _token = store.add("phone", "user")
    assert store.set_role(device_id, "central") is True
    assert store.get(device_id).role == "central"        # promoted
    assert store.set_role(device_id, "user") is True
    assert store.get(device_id).role == "user"           # demoted back


def test_set_role_unknown_id_returns_false(tmp_path):
    store = DeviceStore(str(tmp_path / "devices.db"))
    assert store.set_role("does-not-exist", "central") is False


def test_set_role_invalid_role_raises(tmp_path):
    store = DeviceStore(str(tmp_path / "devices.db"))
    device_id, _token = store.add("phone", "user")
    with pytest.raises(ValueError):
        store.set_role(device_id, "superadmin")
    # role unchanged after the rejected call
    assert store.get(device_id).role == "user"


def test_set_role_never_touches_token_or_revoked(tmp_path):
    # A role change must not grant/void credentials: token still verifies, not revoked.
    store = DeviceStore(str(tmp_path / "devices.db"))
    device_id, token = store.add("phone", "user")
    store.set_role(device_id, "central")
    dev = store.verify(token)
    assert dev is not None and dev.device_id == device_id and dev.revoked is False
    assert dev.role == "central"


# --------------------------------------------------------------------------- #
# POST /api/devices/{id}/role via the real app (middleware + route guards).
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    # A bare house.json default resolves to cwd (recent F1 change) — pin it to tmp.
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
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


def _device_id(central, name="phone"):
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    return next(d for d in devs if d["name"] == name)["device_id"]


def test_loopback_root_can_change_role(app):
    _peer, _auth = _pair(app, "user")
    central = TestClient(app)
    device_id = _device_id(central)
    resp = central.post(f"/api/devices/{device_id}/role", json={"role": "central"}, headers=CSRF)
    assert resp.status_code == 200
    assert resp.json() == {"device_id": device_id, "role": "central"}
    # re-GET confirms the change actually persisted
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    assert next(d for d in devs if d["device_id"] == device_id)["role"] == "central"


def test_lan_central_can_change_role(app):
    _peer, _auth = _pair(app, "user")
    central_peer, central_auth = _pair(app, "central")
    central = TestClient(app)
    target = _device_id(central)
    # a LAN central is Bearer-authed, not subject to the loopback CSRF gate
    resp = central_peer.post(f"/api/devices/{target}/role", json={"role": "central"},
                             headers=central_auth)
    assert resp.status_code == 200


def test_user_token_cannot_self_promote(app):   # audit C1 — role gate is load-bearing
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    device_id = _device_id(central)
    resp = peer.post(f"/api/devices/{device_id}/role", json={"role": "central"}, headers=auth)
    assert resp.status_code == 403
    # and the role really did NOT change
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    assert next(d for d in devs if d["device_id"] == device_id)["role"] == "user"


def test_loopback_role_change_requires_csrf_header(app):
    _peer, _auth = _pair(app, "user")
    central = TestClient(app)
    device_id = _device_id(central)
    # loopback root WITHOUT the X-Wavr-Local header is refused (browser drive-by guard)
    assert central.post(f"/api/devices/{device_id}/role", json={"role": "central"}).status_code == 403


def test_invalid_role_is_422(app):
    _peer, _auth = _pair(app, "user")
    central = TestClient(app)
    device_id = _device_id(central)
    resp = central.post(f"/api/devices/{device_id}/role", json={"role": "root"}, headers=CSRF)
    assert resp.status_code == 422


def test_unknown_device_is_404(app):
    _pair(app, "central")   # ensure a central token path exists / router mounted
    central = TestClient(app)
    resp = central.post("/api/devices/deadbeef/role", json={"role": "central"}, headers=CSRF)
    assert resp.status_code == 404
