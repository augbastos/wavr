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
