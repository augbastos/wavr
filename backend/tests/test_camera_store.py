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
                       "rtsp_url": "rtsp://u:p@10.0.0.5/s1", "confidence": 0.5}

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
