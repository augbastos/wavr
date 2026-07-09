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
