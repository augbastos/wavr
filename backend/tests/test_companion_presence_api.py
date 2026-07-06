"""Route tests for POST/DELETE /api/presence/register-companion.

Uses the same forged-LAN-peer technique as test_multidevice_integration.py
(`TestClient(app, client=(host, port))` + monkeypatched `_local_ipv4`) to
exercise BOTH the loopback-root path and an authenticated (non-central) LAN
'user' companion, since that is the whole point of this feature: a plain
paired phone -- not just a 'central' -- may self-register its own presence.
The ARP resolution is injected (`companion_resolve_mac`) so nothing here
touches a real network/subprocess.
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


def _app(tmp_path, resolver_map=None, identity=None):
    store = identity or IdentityStore(str(tmp_path / "id.db"))
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=store,
        companion_resolve_mac=_fake_resolver(resolver_map or {}))


# --------------------------------------------------------------------------- #
# Loopback (default, non-multidevice build).
# --------------------------------------------------------------------------- #
def test_loopback_register_happy_path(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"}, identity=store)
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "alice"})
        assert r.status_code == 200
        body = r.json()
        assert body == {"mac_registered": True, "label": "alice",
                        "mac_prefix": "aa:bb:cc"}
    # Persisted into the SAME registry NetworkSource's live provider reads.
    assert store.as_net_map() == {"aa:bb:cc:dd:ee:ff": "alice"}
    row = store.get("aa:bb:cc:dd:ee:ff")
    assert row["origin"] == "companion" and row["source"] == "network"


def test_loopback_requires_csrf_header(tmp_path):
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app) as c:   # no X-Wavr-Local
        r = c.post("/api/presence/register-companion", json={"label": "alice"})
        assert r.status_code == 403


def test_no_arp_resolution_is_200_not_error(tmp_path):
    # The IP simply isn't in the (fake) ARP table -- an honest, expected outcome,
    # not a server error, so the mobile client can show a clear message.
    app = _app(tmp_path, {})   # empty resolver map -> always None
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "alice"})
        assert r.status_code == 200
        assert r.json() == {"mac_registered": False, "reason": "no-arp-resolution"}


def test_full_mac_never_leaked_only_prefix(tmp_path):
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app, headers=CSRF) as c:
        body = c.post("/api/presence/register-companion",
                      json={"label": "alice"}).json()
        assert body["mac_prefix"] == "aa:bb:cc"
        assert "dd:ee:ff" not in str(body) and "aa:bb:cc:dd:ee:ff" not in str(body)


def test_empty_label_rejected(tmp_path):
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "   "})
        assert r.status_code == 400


def test_unregister_happy_path(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    store.add("aa:bb:cc:dd:ee:ff", "alice", "network", "companion")
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"}, identity=store)
    with TestClient(app, headers=CSRF) as c:
        r = c.delete("/api/presence/register-companion")
        assert r.status_code == 200
        assert r.json() == {"mac_unregistered": True, "mac_prefix": "aa:bb:cc"}
    assert store.as_net_map() == {}


def test_unregister_no_arp_resolution(tmp_path):
    app = _app(tmp_path, {})
    with TestClient(app, headers=CSRF) as c:
        r = c.delete("/api/presence/register-companion")
        assert r.status_code == 200
        assert r.json() == {"mac_unregistered": False, "reason": "no-arp-resolution"}


def test_unregister_requires_csrf_header(tmp_path):
    app = _app(tmp_path, {"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app) as c:   # no X-Wavr-Local
        assert c.delete("/api/presence/register-companion").status_code == 403


# --------------------------------------------------------------------------- #
# Multi-device: an authenticated 'user'-role LAN peer (NOT 'central') can
# self-register -- the whole point of require_authenticated over require_local.
# --------------------------------------------------------------------------- #
def _md_app(tmp_path, monkeypatch, resolver_map):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    # DeviceStore isn't create_app-injectable (unlike storage/camera_store/
    # identity_store) -- it always opens cfg.db_path, so WAVR_DB must point at
    # an isolated temp file per test (same convention as
    # test_multidevice_integration.py) or devices would leak into the real
    # project wavr.db across test runs.
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    store = IdentityStore(str(tmp_path / "id.db"))
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=store, companion_resolve_mac=_fake_resolver(resolver_map))
    return app, store


def _pair(app, role):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_lan_user_role_can_self_register(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch, {"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    assert r.json()["mac_registered"] is True
    assert store.as_net_map() == {"11:22:33:44:55:66": "housemate"}


def test_lan_user_role_no_csrf_needed_unlike_root(tmp_path, monkeypatch):
    # An authenticated LAN peer proved possession of a bearer token already --
    # it must NOT also need the loopback-root X-Wavr-Local header.
    app, _store = _md_app(tmp_path, monkeypatch, {"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)   # no X-Wavr-Local
    assert r.status_code == 200


def test_unauthenticated_lan_peer_is_forbidden(tmp_path, monkeypatch):
    # No token at all -- the global loopback_or_authed middleware already denies
    # this before require_authenticated is ever reached.
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    r = peer.post("/api/presence/register-companion", json={"label": "x"})
    assert r.status_code == 403


def test_revoked_lan_device_cannot_self_register(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch, {"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    r = peer.post("/api/presence/register-companion", json={"label": "x"}, headers=auth)
    assert r.status_code == 403
