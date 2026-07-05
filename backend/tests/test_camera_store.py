import sqlite3
import pytest
from wavr.camera_store import CameraStore

def _store(tmp_path):
    return CameraStore(str(tmp_path / "t.db"))

def test_add_and_list(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://u:p@10.0.0.5/s1", 0.5)
    s.add("cam_quarto", "quarto", "rtsp://u:p@10.0.0.6/s1", 0.4)
    rows = s.list()
    assert [r["name"] for r in rows] == ["cam_quarto", "cam_sala"]   # sorted
    assert rows[1] == {"name": "cam_sala", "room": "sala",
                       "rtsp_url": "rtsp://u:p@10.0.0.5/s1", "confidence": 0.5,
                       "mac": None}                                  # F3: additive, null by default

def test_duplicate_name_raises(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://x", 0.5)
    with pytest.raises(sqlite3.IntegrityError):
        s.add("cam_sala", "sala", "rtsp://y", 0.5)

def test_get_and_delete(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://x", 0.5)
    assert s.get("cam_sala")["room"] == "sala"
    assert s.get("missing") is None
    assert s.delete("cam_sala") is True
    assert s.delete("cam_sala") is False   # already gone
    assert s.get("cam_sala") is None

def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "t.db")
    CameraStore(p).add("cam_sala", "sala", "rtsp://x", 0.5)
    assert CameraStore(p).get("cam_sala") is not None   # survived reopen


# ---- F3: mac column (additive migration + set_url/set_mac) ----------------------

def test_add_persists_mac_and_get_includes_it(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://u:p@10.0.0.5/s1", 0.5, mac="AA-BB-CC-DD-EE-FF")
    assert s.get("cam_sala")["mac"] == "AA-BB-CC-DD-EE-FF"   # stored verbatim (route normalizes)
    assert "mac" in s.list()[0]

def test_add_without_mac_stores_null(tmp_path):
    s = _store(tmp_path)
    s.add("cam_q", "quarto", "rtsp://x", 0.4)                # old 4-arg call still works
    assert s.get("cam_q")["mac"] is None

def test_set_url_round_trip(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://u:p@10.0.0.5/s1", 0.5)
    assert s.set_url("cam_sala", "rtsp://u:p@10.0.0.9/s1") is True
    assert s.get("cam_sala")["rtsp_url"] == "rtsp://u:p@10.0.0.9/s1"
    assert s.set_url("missing", "rtsp://x") is False         # no row changed

def test_set_mac_round_trip(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://x", 0.5)
    assert s.set_mac("cam_sala", "aa:bb:cc:dd:ee:ff") is True
    assert s.get("cam_sala")["mac"] == "aa:bb:cc:dd:ee:ff"
    assert s.set_mac("cam_sala", None) is True               # clearable
    assert s.get("cam_sala")["mac"] is None

def test_migrate_adds_mac_column_to_old_schema(tmp_path):
    # A DB created before the F3 `mac` column existed (old 4-column schema) must gain
    # the column on open via _migrate(), not raise.
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE cameras (name TEXT PRIMARY KEY, room TEXT NOT NULL,"
        " rtsp_url TEXT NOT NULL, confidence REAL NOT NULL);"
        " INSERT INTO cameras VALUES ('legacy', 'sala', 'rtsp://x', 0.5);")
    conn.commit(); conn.close()
    s = CameraStore(p)                                       # __init__ -> _migrate()
    row = s.get("legacy")
    assert row["mac"] is None                                # column added, back-filled null
    s.set_mac("legacy", "aa:bb:cc:dd:ee:ff")                 # newly-added column is writable
    assert s.get("legacy")["mac"] == "aa:bb:cc:dd:ee:ff"
