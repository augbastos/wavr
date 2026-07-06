"""Adversarial tests for the phone-telemetry security spine (mobile unification,
blueprint steps 1-3). These are the GATE for the sensor role: they prove a stolen
sensor token can inject only its OWN telemetry and read NOTHING, and that a phone can
never attribute telemetry to another device.

Same forged-LAN-peer technique as test_multidevice_integration.py: TestClient with a
`client=(host, port)` tuple, `_local_ipv4` monkeypatched so a fixed 192.168.1.x is
in-subnet. Exercises the REAL middleware + routes.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wavr.app import create_app
from wavr.devices import DeviceStore, VALID_ROLES
from wavr.telemetry import PerDeviceRateLimiter
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}        # loopback root's CSRF header

# Read/GET routes a 'sensor' must be 403 on -> confinement. Kept broad on purpose
# (includes /api/health, which is require_local-gated -> a plain user is 403 there too).
SENSOR_BLOCKED_ROUTES = [
    "/api/history", "/api/state", "/api/house", "/api/inventory",
    "/api/status", "/api/cameras", "/api/health", "/api/presence/report",
]
# The subset of the above that returns 200 for a plain 'user' today -> a 'sensor'
# getting 403 while a user gets 200 proves confinement, not a mere role gate.
# (/api/health is excluded: it is require_local-gated for everyone below central.)
USER_READ_ROUTES = [r for r in SENSOR_BLOCKED_ROUTES if r != "/api/health"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def _pair(app, role="user"):
    """Central (loopback root) mints a code; a forged in-subnet LAN peer redeems it.
    Returns (client, auth_headers, device_id)."""
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    body = peer.post("/api/pair", json={"code": code, "device_name": f"{role}-dev"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}, body["device_id"]


def _sample_payload(device_field="phone"):
    return {
        "device": device_field,
        "sensors": {"accel": [0.0, 0.1, 9.8], "gyro": [0.0, 0.0, 0.0], "pressure": [1013.2]},
        "battery_pct": 72, "charging": True, "rssi": -47, "ssid": "home", "bssid": "aa:bb:cc:dd:ee:ff",
    }


# --- STEP 1: roles -----------------------------------------------------------------

def test_sensor_is_a_valid_role():
    assert "sensor" in VALID_ROLES


def test_devicestore_add_and_set_role_accept_sensor(tmp_path):
    store = DeviceStore(str(tmp_path / "s.db"))
    device_id, token = store.add("phone", "sensor")     # constructor path validates
    assert store.get(device_id).role == "sensor"
    assert store.verify(token).role == "sensor"
    store.add("u", "user")
    assert store.set_role(device_id, "user") is True    # set_role validates too
    with pytest.raises(ValueError):                      # bogus still rejected
        store.set_role(device_id, "bogus")
    store.close()


def test_pair_code_mints_sensor_and_rejects_bogus(app):
    central = TestClient(app)
    ok = central.post("/api/pair-code", json={"role": "sensor"}, headers=CSRF)
    assert ok.status_code == 200 and ok.json()["code"]
    bad = central.post("/api/pair-code", json={"role": "bogus"}, headers=CSRF)
    assert bad.status_code == 400


# --- STEP 2: /api/telemetry identity + rate limit + validation ---------------------

def test_user_can_post_own_telemetry(app):
    peer, auth, device_id = _pair(app, "user")
    r = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "device_id": device_id}


def test_sensor_can_post_telemetry(app):
    peer, auth, device_id = _pair(app, "sensor")
    r = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert r.status_code == 200
    assert r.json()["device_id"] == device_id


def test_cross_device_impersonation_is_keyed_to_caller(app):
    # Device A's token POSTs a body claiming to BE device B. The stored reading and the
    # response must be keyed to A -- the body's `device` field can never override the
    # token identity.
    a_peer, a_auth, a_id = _pair(app, "sensor")
    _b_peer, _b_auth, b_id = _pair(app, "sensor")
    assert a_id != b_id
    r = a_peer.post("/api/telemetry", json=_sample_payload(device_field=b_id), headers=a_auth)
    assert r.status_code == 200
    assert r.json()["device_id"] == a_id            # response keyed to A
    assert r.json()["device_id"] != b_id
    # The enqueued reading is keyed to A, carries A's sensors, and never B's id.
    reading = app.state.telemetry_hub._q.get_nowait()
    assert reading.device_id == a_id
    assert reading.device_id != b_id
    assert reading.sensors.get("accel") == [0.0, 0.1, 9.8]
    assert b_id not in reading.to_dict().values()


def test_telemetry_rate_limit_trips_429(app):
    peer, auth, _ = _pair(app, "sensor")
    # Deterministic tight bucket: 3-token burst, no refill -> the 4th POST is 429.
    app.state.telemetry_limiter = PerDeviceRateLimiter(
        capacity=3, refill_per_sec=0, clock=lambda: 0.0)
    codes = [peer.post("/api/telemetry", json=_sample_payload(), headers=auth).status_code
             for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_rate_limit_is_per_device(app):
    # A's flood must not throttle B (per-device buckets, keyed to the token identity).
    a_peer, a_auth, _ = _pair(app, "sensor")
    b_peer, b_auth, _ = _pair(app, "sensor")
    app.state.telemetry_limiter = PerDeviceRateLimiter(
        capacity=2, refill_per_sec=0, clock=lambda: 0.0)
    for _ in range(3):
        a_peer.post("/api/telemetry", json=_sample_payload(), headers=a_auth)
    # A is now exhausted; B still has its own full bucket.
    assert a_peer.post("/api/telemetry", json=_sample_payload(), headers=a_auth).status_code == 429
    assert b_peer.post("/api/telemetry", json=_sample_payload(), headers=b_auth).status_code == 200


@pytest.mark.parametrize("bad", [
    {"battery_pct": "not-a-number"},
    {"battery_pct": 200},                       # out of range
    {"sensors": "not-an-object"},
    {"sensors": {"accel": "not-a-list"}},
    {"sensors": {"accel": [1, 2, 3, 4, 5, 6, 7, 8, 9]}},  # oversized array
    {"sensors": {"unknown_sensor": [1]}},       # extra key rejected
    {"rssi": 999999},                           # out of range
    {"unexpected_top_level": 1},                # extra top-level key rejected
    [1, 2, 3],                                  # not even an object
])
def test_malformed_payload_is_4xx_never_500(app, bad):
    peer, auth, _ = _pair(app, "sensor")
    r = peer.post("/api/telemetry", json=bad, headers=auth)
    assert r.status_code in (400, 422), (bad, r.status_code)
    assert r.status_code != 500


def test_telemetry_without_token_is_rejected(app):
    # A tokenless in-subnet peer cannot even load /api/telemetry (middleware 403 -- no
    # token -> role None). Loopback root (no device) gets 401 from the handler itself.
    peer = TestClient(app, client=("192.168.1.50", 12345))
    assert peer.post("/api/telemetry", json=_sample_payload()).status_code == 403
    central = TestClient(app)
    assert central.post("/api/telemetry", json=_sample_payload(), headers=CSRF).status_code == 401


# --- STEP 3: confinement -----------------------------------------------------------

def test_sensor_token_is_403_on_every_read_route(app):
    peer, auth, _ = _pair(app, "sensor")
    for route in SENSOR_BLOCKED_ROUTES:
        assert peer.get(route, headers=auth).status_code == 403, route
    # /api/devices (central-gated) and the ws-ticket mint are likewise blocked.
    assert peer.get("/api/devices", headers=auth).status_code == 403
    assert peer.post("/api/ws-ticket", headers=auth).status_code == 403


def test_sensor_token_cannot_open_ws_live(app):
    peer, auth, _ = _pair(app, "sensor")
    # Cannot mint a ticket (confined), and opening /ws/live with any ticket is closed.
    assert peer.post("/api/ws-ticket", headers=auth).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with peer.websocket_connect("/ws/live?ticket=anything"):
            pass


def test_ws_handshake_rejects_sensor_even_with_a_valid_ticket(app):
    # Defence-in-depth: mint a real ticket as a 'user', then demote the device to
    # 'sensor'. The WS handshake's role re-check must now close the connection, proving
    # /ws/live is sensor-proof independent of the http choke point.
    peer, auth, device_id = _pair(app, "user")
    ticket = peer.post("/api/ws-ticket", headers=auth).json()["ticket"]
    central = TestClient(app)
    assert central.post(f"/api/devices/{device_id}/role",
                        json={"role": "sensor"}, headers=CSRF).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with peer.websocket_connect(f"/ws/live?ticket={ticket}"):
            pass


def test_user_and_central_are_unaffected_by_confinement(app):
    # The confinement must not regress non-sensor roles: user reads today's read routes,
    # central changes state, and neither is boxed to /api/telemetry.
    user_peer, user_auth, _ = _pair(app, "user")
    for route in USER_READ_ROUTES:
        assert user_peer.get(route, headers=user_auth).status_code == 200, route
    central_peer, central_auth, _ = _pair(app, "central")
    assert central_peer.get("/api/state", headers=central_auth).status_code == 200
    assert central_peer.post("/api/system/toggle", json={"on": True},
                             headers=central_auth).status_code == 200
    # A user may open the live stream (unchanged); a user may also post its own telemetry.
    t = user_peer.post("/api/ws-ticket", headers=user_auth).json()["ticket"]
    with user_peer.websocket_connect(f"/ws/live?ticket={t}"):
        pass
    assert user_peer.post("/api/telemetry", json=_sample_payload(),
                          headers=user_auth).status_code == 200
