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


def test_details_defaults_to_false(tmp_path):
    s = _store(tmp_path)
    row = s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual")
    assert row["details"] is False
    assert s.detailed_net_addresses() == set()


def test_add_with_details_true_persists_and_lists(tmp_path):
    s = _store(tmp_path)
    row = s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", details=True)
    assert row["details"] is True
    assert s.get("aa:bb:cc:dd:ee:ff")["details"] is True
    assert [r["details"] for r in s.list()] == [True]
    assert s.detailed_net_addresses() == {"aa:bb:cc:dd:ee:ff"}


def test_reregister_without_details_preserves_existing_optin(tmp_path):
    # consent #2 must never be silently revoked by an unrelated re-register (e.g.
    # renaming the person label) that doesn't mention `details` at all.
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", details=True)
    s.add("aa:bb:cc:dd:ee:ff", "renamed", "network", "manual")   # details omitted
    assert s.get("aa:bb:cc:dd:ee:ff")["details"] is True
    assert s.get("aa:bb:cc:dd:ee:ff")["person"] == "renamed"


def test_reregister_with_explicit_details_false_clears_it(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", details=True)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", details=False)
    assert s.get("aa:bb:cc:dd:ee:ff")["details"] is False


def test_set_details_toggles_existing_row(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual")
    assert s.set_details("AA:BB:CC:DD:EE:FF", True) is True   # case/normalize agnostic
    assert s.get("aa:bb:cc:dd:ee:ff")["details"] is True
    assert s.set_details("aa:bb:cc:dd:ee:ff", False) is True
    assert s.get("aa:bb:cc:dd:ee:ff")["details"] is False


def test_set_details_on_unknown_address_returns_false(tmp_path):
    s = _store(tmp_path)
    assert s.set_details("aa:bb:cc:dd:ee:ff", True) is False


def test_detailed_net_addresses_filters_by_source_and_flag(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded", details=True)   # wrong source
    s.add("11:22:33:44:55:66", "phone", "network", "manual", details=False)  # not opted-in
    s.add("22:22:33:44:55:66", "laptop", "network", "manual", details=True)
    assert s.detailed_net_addresses() == {"22:22:33:44:55:66"}


def test_delete_removes_details_optin_too(tmp_path):
    s = _store(tmp_path)
    s.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", details=True)
    assert s.delete("aa:bb:cc:dd:ee:ff") is True
    assert s.detailed_net_addresses() == set()


def test_migration_adds_details_column_to_pre_existing_schema(tmp_path):
    import sqlite3

    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE identity_devices (
            address TEXT PRIMARY KEY, person TEXT NOT NULL, source TEXT NOT NULL,
            origin TEXT NOT NULL, created_ts TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO identity_devices VALUES (?, ?, ?, ?, ?)",
        ("aa:bb:cc:dd:ee:ff", "alice", "network", "manual", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    s = IdentityStore(p)   # must migrate in place, not crash, not drop the row
    row = s.get("aa:bb:cc:dd:ee:ff")
    assert row["person"] == "alice"
    assert row["details"] is False
    assert s.detailed_net_addresses() == set()
