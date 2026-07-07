import pytest

from wavr.identity_store import IdentityStore


def _store(tmp_path):
    return IdentityStore(str(tmp_path / "t.db"))


def test_add_normalizes_and_lists(tmp_path):
    s = _store(tmp_path)
    row = s.add("AA-BB-CC-DD-EE-FF", "alice", "ble", "bonded")
    assert row["address"] == "aa:bb:cc:dd:ee:ff"   # normalized to lowercase colon
    assert row["person"] == "alice"
    assert row["source"] == "ble" and row["origin"] == "bonded"
    assert [r["address"] for r in s.list()] == ["aa:bb:cc:dd:ee:ff"]


def test_as_ble_and_net_maps_partition_by_source(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded")
    s.add("11:22:33:44:55:66", "phone", "network", "manual")
    assert s.as_ble_map() == {"aa:bb:cc:dd:ee:ff": "alice"}
    assert s.as_net_map() == {"11:22:33:44:55:66": "phone"}


def test_delete_is_the_optout(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded")
    assert s.delete("AA:BB:CC:DD:EE:FF") is True     # separator/case agnostic
    assert s.as_ble_map() == {}                       # no longer a signal
    assert s.delete("aa:bb:cc:dd:ee:ff") is False     # already gone


def test_reregister_updates_but_preserves_created_ts(tmp_path):
    ticks = iter(["2026-01-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00"])
    s = IdentityStore(str(tmp_path / "t.db"), now_fn=lambda: next(ticks))
    s.add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded")
    s.add("aa:bb:cc:dd:ee:ff", "renamed", "network", "manual")
    row = s.get("aa:bb:cc:dd:ee:ff")
    assert row["person"] == "renamed" and row["source"] == "network"
    assert row["created_ts"] == "2026-01-01T00:00:00+00:00"   # first consent kept


def test_add_rejects_junk_address(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.add("not-a-mac", "alice")
    assert s.list() == []                             # nothing persisted


def test_add_rejects_empty_person(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.add("aa:bb:cc:dd:ee:ff", "   ")


def test_add_rejects_bad_source_and_origin(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.add("aa:bb:cc:dd:ee:ff", "alice", source="camera")
    with pytest.raises(ValueError):
        s.add("aa:bb:cc:dd:ee:ff", "alice", origin="sniffed")


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "t.db")
    IdentityStore(p).add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded")
    assert IdentityStore(p).as_ble_map() == {"aa:bb:cc:dd:ee:ff": "alice"}
