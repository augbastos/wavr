import pytest

from wavr.device_meta import DeviceMeta, normalize_mac, sanitize_name


def _store(tmp_path):
    return DeviceMeta(str(tmp_path / "t.db"))


# ---- seen() -- first_seen set once, last_seen bumped ------------------------

def test_seen_sets_first_and_last_seen(tmp_path):
    s = _store(tmp_path)
    s.seen("A4:83:E7:11:22:33")
    entry = s.get("a4:83:e7:11:22:33")
    assert entry["first_seen"] is not None
    assert entry["first_seen"] == entry["last_seen"]
    assert entry["name"] is None


def test_seen_twice_keeps_first_seen_but_bumps_last_seen(tmp_path):
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    first = s.get("a4:83:e7:11:22:33")["first_seen"]
    s.seen("a4:83:e7:11:22:33")
    second = s.get("a4:83:e7:11:22:33")
    assert second["first_seen"] == first        # never changes after the first sighting
    assert second["last_seen"] is not None       # still populated (may equal first on a fast box)


def test_seen_normalizes_dash_separator_and_case(tmp_path):
    s = _store(tmp_path)
    s.seen("A4-83-E7-11-22-33")
    assert s.get("a4:83:e7:11:22:33") is not None


def test_seen_preserves_existing_name(tmp_path):
    s = _store(tmp_path)
    s.set_name("a4:83:e7:11:22:33", "Living Room TV")
    s.seen("a4:83:e7:11:22:33")
    assert s.get("a4:83:e7:11:22:33")["name"] == "Living Room TV"


def test_get_missing_mac_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.get("00:11:22:33:44:55") is None


# ---- set_name() ---------------------------------------------------------------

def test_set_name_creates_entry_without_seen(tmp_path):
    s = _store(tmp_path)
    entry = s.set_name("a4:83:e7:11:22:33", "Fridge")
    assert entry == {"mac": "a4:83:e7:11:22:33", "name": "Fridge",
                      "first_seen": None, "last_seen": None,
                      "device_type": None}


def test_set_name_updates_without_touching_seen_timestamps(tmp_path):
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    before = s.get("a4:83:e7:11:22:33")
    s.set_name("a4:83:e7:11:22:33", "Fridge")
    after = s.get("a4:83:e7:11:22:33")
    assert after["name"] == "Fridge"
    assert after["first_seen"] == before["first_seen"]
    assert after["last_seen"] == before["last_seen"]


def test_set_name_trims_and_strips_control_chars(tmp_path):
    s = _store(tmp_path)
    entry = s.set_name("a4:83:e7:11:22:33", "  Fridge\x00\x01 \n ")
    assert entry["name"] == "Fridge"


def test_set_name_rejects_empty_after_trim(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.set_name("a4:83:e7:11:22:33", "   \x00\x01  ")


def test_set_name_rejects_over_max_len(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.set_name("a4:83:e7:11:22:33", "x" * 65)


def test_set_name_allows_exactly_max_len(tmp_path):
    s = _store(tmp_path)
    entry = s.set_name("a4:83:e7:11:22:33", "x" * 64)
    assert entry["name"] == "x" * 64


def test_set_name_rejects_invalid_mac(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.set_name("not-a-mac", "Fridge")


# ---- all() ----------------------------------------------------------------------

def test_all_returns_dict_keyed_by_mac(tmp_path):
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    s.set_name("24:0a:c4:aa:bb:cc", "Router")
    everything = s.all()
    assert set(everything) == {"a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc"}
    assert everything["24:0a:c4:aa:bb:cc"]["name"] == "Router"
    assert everything["a4:83:e7:11:22:33"]["first_seen"] is not None


def test_all_empty_store_returns_empty_dict(tmp_path):
    assert _store(tmp_path).all() == {}


# ---- get_many() -- batch read (N+1 fix for GET /api/inventory) ------------------

def test_get_many_returns_only_matching_macs(tmp_path):
    s = _store(tmp_path)
    s.set_name("a4:83:e7:11:22:33", "Fridge")
    s.seen("24:0a:c4:aa:bb:cc")
    found = s.get_many(["a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "00:11:22:33:44:55"])
    assert set(found) == {"a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc"}  # unseen MAC absent
    assert found["a4:83:e7:11:22:33"]["name"] == "Fridge"
    assert found["24:0a:c4:aa:bb:cc"]["first_seen"] is not None


def test_get_many_matches_get_for_each_mac(tmp_path):
    # One-SELECT batch path must return the same {name, first_seen, last_seen,
    # device_type} values as N calls to get() -- get_many()'s per-mac entry
    # omits the redundant "mac" key (same convention as all()'s entries,
    # since the outer dict is already keyed by mac) -- this is a pure perf
    # change, never an observable one for _device_view's callers, which never
    # read meta["mac"].
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    s.set_name("24:0a:c4:aa:bb:cc", "Router")
    macs = ["a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc"]
    batch = s.get_many(macs)
    for mac in macs:
        individual = dict(s.get(mac))
        del individual["mac"]
        assert batch[mac] == individual


def test_get_many_empty_input_returns_empty_dict_without_querying(tmp_path):
    assert _store(tmp_path).get_many([]) == {}


def test_get_many_skips_malformed_macs_without_raising(tmp_path):
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    found = s.get_many(["a4:83:e7:11:22:33", "not-a-mac"])
    assert set(found) == {"a4:83:e7:11:22:33"}


def test_get_many_dedupes_repeated_macs(tmp_path):
    s = _store(tmp_path)
    s.seen("a4:83:e7:11:22:33")
    found = s.get_many(["a4:83:e7:11:22:33", "A4:83:E7:11:22:33", "a4-83-e7-11-22-33"])
    assert set(found) == {"a4:83:e7:11:22:33"}


# ---- seen_many() -- batch write, one commit for the whole scan cycle ------------

def test_seen_many_sets_first_and_last_seen_for_every_mac(tmp_path):
    s = _store(tmp_path)
    s.seen_many(["a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc"])
    for mac in ("a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc"):
        entry = s.get(mac)
        assert entry["first_seen"] is not None
        assert entry["first_seen"] == entry["last_seen"]


def test_seen_many_preserves_first_seen_and_existing_name_on_rescan(tmp_path):
    s = _store(tmp_path)
    s.set_name("a4:83:e7:11:22:33", "Living Room TV")
    s.seen_many(["a4:83:e7:11:22:33"])
    first = s.get("a4:83:e7:11:22:33")["first_seen"]
    s.seen_many(["a4:83:e7:11:22:33"])
    after = s.get("a4:83:e7:11:22:33")
    assert after["first_seen"] == first          # never changes after the first sighting
    assert after["name"] == "Living Room TV"     # untouched


def test_seen_many_skips_malformed_macs_without_raising(tmp_path):
    s = _store(tmp_path)
    s.seen_many(["a4:83:e7:11:22:33", "not-a-mac"])
    assert s.get("a4:83:e7:11:22:33") is not None


def test_seen_many_empty_input_is_a_noop(tmp_path):
    s = _store(tmp_path)
    s.seen_many([])
    assert s.all() == {}


def test_seen_many_equivalent_to_calling_seen_per_mac(tmp_path):
    macs = ["a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"]
    batched = _store(tmp_path)
    batched.seen_many(macs)
    individually = _store(tmp_path)
    for mac in macs:
        individually.seen(mac)
    assert set(batched.all()) == set(individually.all()) == set(macs)


# ---- PRAGMA tuning (WAL + synchronous=NORMAL) -- SD-card wear/latency on the G9 -

def test_file_backed_store_uses_wal_and_synchronous_normal(tmp_path):
    s = _store(tmp_path)
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = s._conn.execute("PRAGMA synchronous").fetchone()[0]
    assert mode.lower() == "wal"
    assert sync == 1   # NORMAL (0=OFF, 1=NORMAL, 2=FULL)


def test_in_memory_store_pragma_tuning_never_raises():
    # :memory: doesn't support WAL -- construction must not raise (suppressed),
    # and the store must still work normally.
    s = DeviceMeta(":memory:")
    s.seen("a4:83:e7:11:22:33")
    assert s.get("a4:83:e7:11:22:33") is not None


# ---- persistence across instances (mirrors test_camera_store.py) ----------------

def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "t.db")
    DeviceMeta(p).seen("a4:83:e7:11:22:33")
    assert DeviceMeta(p).get("a4:83:e7:11:22:33") is not None


def test_in_memory_store_for_tests():
    s = DeviceMeta(":memory:")
    s.seen("a4:83:e7:11:22:33")
    assert s.get("a4:83:e7:11:22:33") is not None


# ---- module-level helpers -----------------------------------------------------

def test_normalize_mac_accepts_dash_and_colon():
    assert normalize_mac("A4-83-E7-11-22-33") == "a4:83:e7:11:22:33"
    assert normalize_mac("a4:83:e7:11:22:33") == "a4:83:e7:11:22:33"


def test_normalize_mac_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_mac("hello")


def test_sanitize_name_strips_control_chars_and_trims():
    assert sanitize_name("  Kitchen\x07 Cam  ") == "Kitchen Cam"


def test_sanitize_name_rejects_empty():
    with pytest.raises(ValueError):
        sanitize_name("   ")
