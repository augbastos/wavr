"""Route tests for GET/POST /api/consent (device-scope participation
tri-color, 2026-07-11 mobile companion reconciliation) + the register-companion
enforcement it backs. Uses the same forged-LAN-peer technique as
test_companion_presence_api.py/test_multidevice_integration.py
(`TestClient(app, client=(host, port))` + monkeypatched `_local_ipv4`).
"""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.identity_store import IdentityStore
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}


def _fake_resolver(mapping: dict):
    async def resolve(ip):
        return mapping.get(ip)
    return resolve


def _md_app(tmp_path, monkeypatch, resolver_map=None, identity=None):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    # DeviceStore always opens cfg.db_path -- isolate it per test (same
    # convention as test_companion_presence_api.py's _md_app) or devices would
    # leak into the real project wavr.db across test runs.
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    store = identity or IdentityStore(str(tmp_path / "id.db"))
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=store, companion_resolve_mac=_fake_resolver(resolver_map or {}))
    return app, store


def _pair(app, role):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Root: has no Device row -- its lever is /api/system/toggle, so /api/consent
# 409s rather than fabricating a value. Tested so the loopback dashboard (which
# never calls this route today) can't accidentally crash if it ever did.
# --------------------------------------------------------------------------- #
def test_root_get_consent_409(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.get("/api/consent")
        assert r.status_code == 409
        assert "system/toggle" in r.json()["detail"]


def test_root_post_consent_409(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/consent", json={"level": "red"})
        assert r.status_code == 409


# --------------------------------------------------------------------------- #
# A paired LAN companion: self-resolved, no body device_id anywhere.
# --------------------------------------------------------------------------- #
def test_default_consent_is_green(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.get("/api/consent", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "green"
    assert "device_id" in body


def test_set_and_get_consent_roundtrip(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "yellow" and body["device_id"]
    r2 = peer.get("/api/consent", headers=auth)
    assert r2.json()["level"] == "yellow"


def test_invalid_consent_level_422(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "blue"}, headers=auth)
    assert r.status_code == 422


def test_lan_peer_needs_no_csrf_header(tmp_path, monkeypatch):
    # An authenticated LAN peer proved possession of a bearer token already --
    # unlike root, it must NOT also need X-Wavr-Local.
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "red"}, headers=auth)
    assert r.status_code == 200


def test_unauthenticated_lan_peer_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                     storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    r = peer.get("/api/consent")
    assert r.status_code == 403


def test_revoked_device_cannot_read_consent(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    r = peer.get("/api/consent", headers=auth)
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Enforcement at register_companion: red drops, yellow anonymizes, green full.
# --------------------------------------------------------------------------- #
def test_red_consent_drops_registration_server_side(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    assert r.json() == {"mac_registered": False, "reason": "consent-withdrawn"}
    # Never even reaches identity_store -- a patched client that keeps sending
    # after withdrawal still can't get a row written.
    assert store.as_net_map() == {}


def test_yellow_consent_registers_without_name_label(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["mac_registered"] is True
    assert body["label"] is None
    assert body["mac_prefix"] == "11:22:33"
    # No identity row -- anonymized, never carries the real name.
    assert store.as_net_map() == {}
    assert store.get("11:22:33:44:55:66") is None


def test_green_consent_registers_full_named_presence(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    # green is the default -- no explicit POST /api/consent needed.
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    assert r.json()["mac_registered"] is True
    assert r.json()["label"] == "housemate"
    assert store.as_net_map() == {"11:22:33:44:55:66": "housemate"}


def test_root_register_companion_unaffected_by_consent_column(tmp_path, monkeypatch):
    # Web-mode/loopback byte-identical regression guard: root has no Device
    # row, so it must always behave as "green" -- unchanged from before this
    # feature existed.
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "alice"})
        assert r.status_code == 200
        assert r.json() == {"mac_registered": True, "label": "alice",
                            "mac_prefix": "aa:bb:cc"}
    assert store.as_net_map() == {"aa:bb:cc:dd:ee:ff": "alice"}


# --------------------------------------------------------------------------- #
# GET /api/devices/me -- read-back of the caller's own role.
# --------------------------------------------------------------------------- #
def test_devices_me_root(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.get("/api/devices/me")
        assert r.status_code == 200
        assert r.json()["role"] == "root"


def test_devices_me_user_peer(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.get("/api/devices/me", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "user"
    assert body["device_id"]
