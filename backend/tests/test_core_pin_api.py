"""Route tests for POST /api/core/pin, POST /api/core/pin/verify,
GET /api/core/pin/status. Mirrors test_identity_api.py's fixture style."""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.pin_store import PinStore
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}


def _client(tmp_path, pin_store=None):
    store = pin_store or PinStore(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        pin_store=store)
    return TestClient(app, headers=CSRF), store


# --------------------------------------------------------------------------- #
# GET status
# --------------------------------------------------------------------------- #
def test_status_false_when_unset(tmp_path):
    c, _store = _client(tmp_path)
    assert c.get("/api/core/pin/status").json() == {"pin_set": False}


def test_status_true_after_set(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    assert c.get("/api/core/pin/status").json() == {"pin_set": True}


def test_status_needs_no_csrf_header(tmp_path):
    store = PinStore(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        pin_store=store)
    with TestClient(app) as c:   # no X-Wavr-Local
        assert c.get("/api/core/pin/status").status_code == 200


# --------------------------------------------------------------------------- #
# POST /api/core/pin (set) -- require_local: admin/loopback-root+CSRF only.
# --------------------------------------------------------------------------- #
def test_set_pin_happy_path(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/core/pin", json={"pin": "4321"})
    assert r.status_code == 200
    assert r.json() == {"set": True}
    assert store.verify("4321") is True


def test_set_pin_requires_csrf_header(tmp_path):
    store = PinStore(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        pin_store=store)
    with TestClient(app) as c:   # no X-Wavr-Local
        r = c.post("/api/core/pin", json={"pin": "1234"})
        assert r.status_code == 403
        assert store.is_set() is False


def test_set_pin_rejects_non_digits(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/core/pin", json={"pin": "abcd"})
    assert r.status_code == 400
    assert store.is_set() is False


def test_set_pin_rejects_too_short(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/core/pin", json={"pin": "12"})
    assert r.status_code == 400


def test_set_pin_rejects_too_long(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/core/pin", json={"pin": "1" * 20})
    assert r.status_code == 400


def test_set_pin_never_echoes_the_pin(tmp_path):
    c, _store = _client(tmp_path)
    r = c.post("/api/core/pin", json={"pin": "1234"})
    assert "1234" not in r.text


# --------------------------------------------------------------------------- #
# POST /api/core/pin/verify -- constant-time, rate-limited.
# --------------------------------------------------------------------------- #
def test_verify_correct_pin(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    r = c.post("/api/core/pin/verify", json={"pin": "1234"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_verify_wrong_pin(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    r = c.post("/api/core/pin/verify", json={"pin": "0000"})
    assert r.json() == {"ok": False}


def test_verify_no_pin_set_is_false_not_error(tmp_path):
    c, _store = _client(tmp_path)
    r = c.post("/api/core/pin/verify", json={"pin": "1234"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_verify_rate_limited_after_repeated_failures(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    for _ in range(5):   # MAX_FAILED_ATTEMPTS
        r = c.post("/api/core/pin/verify", json={"pin": "0000"})
        assert r.json() == {"ok": False}
    # Locked out now -- even the CORRECT pin is refused until the window elapses.
    r = c.post("/api/core/pin/verify", json={"pin": "1234"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_verify_success_does_not_trip_the_limiter_for_next_caller(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    assert c.post("/api/core/pin/verify", json={"pin": "1234"}).json()["ok"] is True
    assert c.post("/api/core/pin/verify", json={"pin": "1234"}).json()["ok"] is True


def test_verify_malformed_pin_type_is_false(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    r = c.post("/api/core/pin/verify", json={"pin": ""})
    assert r.json() == {"ok": False}


def test_verify_oversized_pin_is_false_never_hashed(tmp_path):
    c, _store = _client(tmp_path)
    c.post("/api/core/pin", json={"pin": "1234"})
    r = c.post("/api/core/pin/verify", json={"pin": "1" * 200})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


# --------------------------------------------------------------------------- #
# Multi-device: a paired LAN peer (any role) may verify without the loopback
# CSRF header (it already proved a bearer token); an unauthenticated peer is
# denied entirely by the global middleware before this route is even reached.
# --------------------------------------------------------------------------- #
def _md_app(tmp_path, monkeypatch, pin_store):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        pin_store=pin_store)


def _pair(app, role="user"):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "panel"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_lan_user_role_can_verify_without_csrf(tmp_path, monkeypatch):
    store = PinStore(":memory:")
    store.set_pin("1234")
    app = _md_app(tmp_path, monkeypatch, store)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/core/pin/verify", json={"pin": "1234"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_lan_user_role_cannot_set_pin(tmp_path, monkeypatch):
    # Set is require_local -> 'user' role is not central/root, so it is 403 even
    # though the same role IS allowed to verify (require_authenticated).
    store = PinStore(":memory:")
    app = _md_app(tmp_path, monkeypatch, store)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/core/pin", json={"pin": "1234"}, headers=auth)
    assert r.status_code == 403
    assert store.is_set() is False


def test_lan_central_role_can_set_pin(tmp_path, monkeypatch):
    store = PinStore(":memory:")
    app = _md_app(tmp_path, monkeypatch, store)
    peer, auth = _pair(app, "central")
    r = peer.post("/api/core/pin", json={"pin": "1234"}, headers=auth)
    assert r.status_code == 200
    assert store.verify("1234") is True


def test_unauthenticated_lan_peer_cannot_verify(tmp_path, monkeypatch):
    store = PinStore(":memory:")
    store.set_pin("1234")
    app = _md_app(tmp_path, monkeypatch, store)
    peer = TestClient(app, client=("192.168.1.50", 12345))
    r = peer.post("/api/core/pin/verify", json={"pin": "1234"})
    assert r.status_code == 403
