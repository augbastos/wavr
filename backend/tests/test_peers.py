import pytest
from wavr.peers import PeerStore


def _store(tmp_path):
    return PeerStore(str(tmp_path / "peers.db"))


def test_add_returns_peer_id_and_is_listed(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add(name="Core-G9", base_url="https://192.168.1.57:8000",
                         cert_fingerprint="AB:CD:EF", local_device_id="dev123",
                         token="secret-token-abc")
    assert peer_id
    peers = store.list()
    assert len(peers) == 1
    assert peers[0].peer_id == peer_id
    assert peers[0].name == "Core-G9"
    assert peers[0].base_url == "https://192.168.1.57:8000"
    assert peers[0].cert_fingerprint == "AB:CD:EF"
    assert peers[0].room_map == {}
    assert peers[0].revoked is False


def test_token_for_returns_the_stored_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    assert store.token_for(peer_id) == "tok-xyz"


def test_token_for_unknown_peer_is_none(tmp_path):
    store = _store(tmp_path)
    assert store.token_for("nope") is None


def test_list_never_includes_token(tmp_path):
    store = _store(tmp_path)
    store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    peer = store.list()[0]
    assert not hasattr(peer, "token")


def test_get_by_id(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.get(peer_id).name == "Core-G9"
    assert store.get("nope") is None


def test_set_room_map_persists(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.set_room_map(peer_id, {"sala": "living_room"}) is True
    assert store.get(peer_id).room_map == {"sala": "living_room"}


def test_set_room_map_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.set_room_map("nope", {"a": "b"}) is False


def test_revoke_marks_revoked_and_clears_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.get(peer_id).revoked is True
    assert store.token_for(peer_id) is None  # revoked = unusable, not just flagged


def test_revoke_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.revoke("nope") is False


def test_revoke_is_idempotent(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.revoke(peer_id) is True  # second revoke still True, not an error


from datetime import datetime, timedelta, timezone
from wavr.peers import PeerExchangeManager


class _Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def test_stash_and_pop_roundtrip():
    mgr = PeerExchangeManager()
    exchange_id = mgr.stash("Desktop", "https://192.168.1.10:8000", "12345678", "AA:BB")
    pending = mgr.pop(exchange_id)
    assert pending.requester_name == "Desktop"
    assert pending.requester_base_url == "https://192.168.1.10:8000"
    assert pending.requester_code == "12345678"
    assert pending.requester_fingerprint == "AA:BB"


def test_pop_is_single_use():
    mgr = PeerExchangeManager()
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    assert mgr.pop(exchange_id) is not None
    assert mgr.pop(exchange_id) is None  # consumed


def test_pop_unknown_id_returns_none():
    mgr = PeerExchangeManager()
    assert mgr.pop("nope") is None


def test_pop_expired_returns_none():
    clock = _Clock()
    mgr = PeerExchangeManager(now_fn=clock, ttl=120)
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    clock.advance(121)
    assert mgr.pop(exchange_id) is None


def test_pop_just_before_ttl_still_works():
    clock = _Clock()
    mgr = PeerExchangeManager(now_fn=clock, ttl=120)
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    clock.advance(119)
    assert mgr.pop(exchange_id) is not None


# --------------------------------------------------------------------------
# api_peers.py router-level tests (Task 6). TWO router factories -- the public
# (unauthenticated, in-subnet) exchange/redeem entry points and the local-admin
# discovered/confirm/finish/list/unpair surface -- mirroring api_devices.py's
# split so app.py (Task 7) can attach different auth gates per group. The
# confirm/finish two-instance handshake belongs in Task 7 (needs two wired
# _app() instances); here every endpoint is exercised directly with no gates.
# --------------------------------------------------------------------------
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_peers import build_peers_public_router, build_peers_admin_router
from wavr.devices import DeviceStore
from wavr.pairing import PairingManager


def _app(tmp_path, self_base_url="https://desktop.local:8000", self_name="Desktop"):
    devices = DeviceStore(str(tmp_path / "devices.db"))
    peers = PeerStore(str(tmp_path / "peers.db"))
    pairing = PairingManager(devices)
    exchange = PeerExchangeManager()
    cfg = types.SimpleNamespace(tls_cert="")  # resolved_cert_path("") -> default; absent -> ""
    app = FastAPI()
    app.include_router(build_peers_public_router(peers, exchange, pairing, cfg, self_name))
    app.include_router(build_peers_admin_router(peers, exchange, pairing, devices,
                                                cfg, self_name, self_base_url))
    return app, devices, peers, pairing, exchange


def test_discovered_lists_mdns_results(tmp_path, monkeypatch):
    app, *_ = _app(tmp_path)
    from wavr import mdns_peers
    monkeypatch.setattr(mdns_peers, "browse_wavr_peers",
                        lambda **k: [mdns_peers.DiscoveredPeer("Core", "1.2.3.4", 8000, "core")])
    client = TestClient(app)
    r = client.get("/api/peers/discovered")
    assert r.status_code == 200
    assert r.json() == [{"name": "Core", "host": "1.2.3.4", "port": 8000, "role": "core"}]


def test_exchange_stashes_and_returns_own_code(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path, self_name="Core")
    client = TestClient(app)
    r = client.post("/api/peers/exchange", json={
        "requester_name": "Desktop", "requester_base_url": "https://desktop:8000",
        "requester_code": "11112222", "requester_fingerprint": "AA:AA",
    })
    assert r.status_code == 200
    body = r.json()
    assert "code" in body and len(body["code"]) == 8
    assert "fingerprint" in body


def test_redeem_creates_central_device(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    code = pairing.mint_code("central")
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": code, "requester_name": "Desktop"})
    assert r.status_code == 200
    body = r.json()
    assert "device_id" in body and "token" in body
    dev = devices.get(body["device_id"])
    assert dev.role == "central"


def test_redeem_rejects_bad_code(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": "00000000", "requester_name": "X"})
    assert r.status_code == 403


def test_list_peers_empty_initially(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/peers")
    assert r.status_code == 200
    assert r.json() == []


def test_unpair_revokes_peer_and_device(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    device_id, token = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "their-token")
    client = TestClient(app)
    r = client.delete(f"/api/peers/{peer_id}")
    assert r.status_code == 200
    assert peers.get(peer_id).revoked is True
    assert devices.get(device_id).revoked is True


# --------------------------------------------------------------------------
# Task 7: the full two-instance handshake (PROTOCOL, via the bare _app() harness)
# + the create_app auth-gate wiring (real middleware + route deps).
# --------------------------------------------------------------------------
from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

_CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header


def test_full_bidirectional_pairing_two_instances(tmp_path):
    """The real end-to-end protocol: two separate _app() instances (standing in
    for Desktop and Core), wired to call each other via a SHARED routed fake
    transport (keyed by base_url so peer_client's outbound calls land on the
    OTHER app's TestClient instead of the network) -- proves the whole 2-leg
    handshake produces a working PeerStore row + a role=central DeviceStore row
    on BOTH sides, with zero real network."""
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    d_app, d_devices, d_peers, d_pairing, d_exchange = _app(
        tmp_path / "d", self_base_url="https://desktop:8000", self_name="Desktop")
    c_app, c_devices, c_peers, c_pairing, c_exchange = _app(
        tmp_path / "c", self_base_url="https://core:8000", self_name="Core")
    d_client, c_client = TestClient(d_app), TestClient(c_app)

    def routed_transport(method, url, headers, body, pinned_fingerprint, timeout):
        import json as _json
        client = c_client if url.startswith("https://core:8000") else d_client
        path = url.split(":8000", 1)[1]
        resp = (client.post(path, json=_json.loads(body), headers=headers) if method == "POST"
                else client.get(path, headers=headers))
        return resp.content

    import wavr.peer_client as peer_client
    orig_default = peer_client._default_transport
    peer_client._default_transport = routed_transport
    try:
        # D mints its own code, D calls C's /exchange (as if D initiated pairing).
        d_code = d_pairing.mint_code("central")
        exch = c_client.post("/api/peers/exchange", json={
            "requester_name": "Desktop", "requester_base_url": "https://desktop:8000",
            "requester_code": d_code, "requester_fingerprint": "DESKTOP-FP",
        }).json()
        # Admin confirms C's fingerprint on D's screen; D calls its own /confirm.
        result = d_client.post("/api/peers/confirm", json={
            "exchange_id": exch["exchange_id"], "peer_code": exch["code"],
            "peer_fingerprint": exch["fingerprint"], "peer_base_url": "https://core:8000",
            "peer_name": "Core",
        }).json()
        assert result["reverse_leg_ok"] is True
        assert len(d_peers.list()) == 1 and d_peers.list()[0].name == "Core"
        assert len(c_peers.list()) == 1 and c_peers.list()[0].name == "Desktop"
        assert d_devices.list()[0].role == "central"  # D's row for C
        assert c_devices.list()[0].role == "central"  # C's row for D
    finally:
        peer_client._default_transport = orig_default


def _peers_app(tmp_path, monkeypatch, peers="1", multidevice="1"):
    """A REAL create_app with peers (and multidevice) toggled, a forged fixed
    LAN IP so an in-subnet peer can be simulated, and in-memory storage/cameras
    so only the device+peer store touch the temp db file."""
    if multidevice is None:
        monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    else:
        monkeypatch.setenv("WAVR_MULTIDEVICE", multidevice)
    monkeypatch.setenv("WAVR_PEERS_ENABLED", peers)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "peers-app.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def test_peers_enabled_requires_multidevice(tmp_path, monkeypatch):
    # Prerequisite validation: peers ON without multidevice must fail fast.
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError, match="requires WAVR_MULTIDEVICE"):
        create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                   storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def test_admin_peer_route_gated_but_public_exchange_open(tmp_path, monkeypatch):
    app = _peers_app(tmp_path, monkeypatch)
    peer = TestClient(app, client=("192.168.1.50", 12345))  # forged in-subnet LAN peer
    # Admin surface is gated: an unauthenticated LAN peer is refused (middleware, no token)
    assert peer.get("/api/peers").status_code == 403
    assert peer.post("/api/peers/confirm", json={
        "exchange_id": "x", "peer_code": "y", "peer_fingerprint": "z",
        "peer_base_url": "https://core:8000", "peer_name": "Core"}).status_code == 403
    assert peer.post("/api/peers/finish", json={"exchange_id": "x"}).status_code == 403
    # ...but the deliberately-unauthenticated public entry point IS reachable without a
    # token, exactly like /api/pair (widened token-exemption in the middleware).
    r = peer.post("/api/peers/exchange", json={
        "requester_name": "Desktop", "requester_base_url": "https://desktop:8000",
        "requester_code": "11112222", "requester_fingerprint": "AA:AA"})
    assert r.status_code == 200
    assert "code" in r.json() and len(r.json()["code"]) == 8


def test_admin_peer_route_needs_csrf_on_loopback_root(tmp_path, monkeypatch):
    # Proves require_local is actually attached to the admin router: loopback root
    # WITHOUT the CSRF header is refused; WITH it, the list works.
    app = _peers_app(tmp_path, monkeypatch)
    root = TestClient(app)
    assert root.get("/api/peers").status_code == 403           # missing X-Wavr-Local
    ok = root.get("/api/peers", headers=_CSRF)
    assert ok.status_code == 200 and ok.json() == []


def test_out_of_subnet_peer_cannot_reach_public_exchange(tmp_path, monkeypatch):
    # The public exemption is in-subnet-bounded, same as /api/pair: an out-of-/24
    # peer is still forbidden even on /api/peers/exchange.
    app = _peers_app(tmp_path, monkeypatch)
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    assert outsider.post("/api/peers/exchange", json={
        "requester_name": "X", "requester_base_url": "https://x:8000",
        "requester_code": "1", "requester_fingerprint": "Z"}).status_code == 403


def test_peer_routes_absent_when_flag_off(tmp_path, monkeypatch):
    # Default-off wiring: multidevice ON but peers OFF -> routers not mounted -> 404,
    # not an accidental 200. (Byte-identical-when-off invariant for the mount.)
    app = _peers_app(tmp_path, monkeypatch, peers="0")
    root = TestClient(app)
    assert root.get("/api/peers", headers=_CSRF).status_code == 404


def test_lifespan_boots_with_peers_enabled_without_zeroconf(tmp_path, monkeypatch):
    # zeroconf is NOT installed; the lifespan mDNS self-advertise must fail soft
    # (lazy import + try/except) so the app still boots. Entering the TestClient as a
    # context manager runs the real lifespan startup + shutdown.
    app = _peers_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200
