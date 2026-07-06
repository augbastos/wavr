"""Unit tests for wavr.pin_store.PinStore -- persistence + hashing only (no
FastAPI/routes). Mirrors test_multidevice.py's DeviceStore tests in style."""
from wavr.pin_store import PinStore


def test_unset_pin_is_not_set_and_verifies_false():
    store = PinStore(":memory:")
    assert store.is_set() is False
    assert store.verify("1234") is False


def test_set_then_verify_correct_pin():
    store = PinStore(":memory:")
    store.set_pin("1234")
    assert store.is_set() is True
    assert store.verify("1234") is True


def test_verify_wrong_pin_is_false():
    store = PinStore(":memory:")
    store.set_pin("1234")
    assert store.verify("9999") is False
    assert store.verify("123") is False       # wrong length
    assert store.verify("") is False


def test_pin_stored_hashed_not_plaintext():
    store = PinStore(":memory:")
    store.set_pin("1234")
    row = store._conn.execute("SELECT salt_hex, hash_hex FROM core_pin").fetchone()
    assert row["hash_hex"] != "1234"
    assert "1234" not in row["hash_hex"]
    assert len(row["salt_hex"]) == 32   # 16 bytes hex-encoded


def test_reset_pin_uses_a_fresh_salt_and_invalidates_old_pin():
    store = PinStore(":memory:")
    store.set_pin("1234")
    row1 = store._conn.execute("SELECT salt_hex FROM core_pin").fetchone()
    store.set_pin("5678")
    row2 = store._conn.execute("SELECT salt_hex FROM core_pin").fetchone()
    assert row1["salt_hex"] != row2["salt_hex"]     # fresh salt each set
    assert store.verify("1234") is False            # old PIN no longer works
    assert store.verify("5678") is True


def test_two_stores_same_pin_get_different_hashes():
    # Different random salts -> different hash even for the identical PIN, so a
    # leaked db can't be rainbow-tabled across installs.
    a, b = PinStore(":memory:"), PinStore(":memory:")
    a.set_pin("1234")
    b.set_pin("1234")
    ra = a._conn.execute("SELECT hash_hex FROM core_pin").fetchone()
    rb = b._conn.execute("SELECT hash_hex FROM core_pin").fetchone()
    assert ra["hash_hex"] != rb["hash_hex"]
