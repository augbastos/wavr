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
                      "first_seen": None, "last_seen": None}


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
