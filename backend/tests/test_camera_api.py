import pytest
from fastapi.testclient import TestClient
from wavr.app import create_app
from wavr.camera_store import CameraStore

def _client(tmp_path, seed=None):
    store = CameraStore(str(tmp_path / "cams.db"))
    if seed:
        for c in seed:
            store.add(**c)
    app = create_app(
        sources=[],                       # no default sources -> isolate camera behavior
        camera_store=store,
    )
    return TestClient(app, headers={"X-Wavr-Local": "1"})

def test_post_adds_camera_as_boot_off_source(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_sala", "room": "sala",
                                         "rtsp_url": "rtsp://u:pw@10.0.0.5/s1", "confidence": 0.5})
        assert r.status_code == 200
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert "cam_sala" in sysrc
        assert sysrc["cam_sala"]["enabled"] is False       # SAFETY: boots OFF

def test_get_masks_credentials(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_sala", "room": "sala",
                                  "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}]) as c:
        [cam] = c.get("/api/cameras").json()
        assert "secret" not in cam["rtsp_url"]             # password never echoed
        assert cam["name"] == "cam_sala" and cam["room"] == "sala"

def test_persisted_cameras_registered_on_startup(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" in sysrc                            # loaded from store at boot

def test_delete_removes_camera(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        assert c.delete("/api/cameras/cam_q").status_code == 200
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" not in sysrc
        assert c.delete("/api/cameras/cam_q").status_code == 404   # already gone

def test_duplicate_name_rejected(tmp_path):
    with _client(tmp_path) as c:
        body = {"name": "cam_sala", "room": "sala", "rtsp_url": "rtsp://x", "confidence": 0.5}
        assert c.post("/api/cameras", json=body).status_code == 200
        assert c.post("/api/cameras", json=body).status_code == 409   # duplicate

def test_camera_endpoints_require_local_header(tmp_path):
    store = CameraStore(str(tmp_path / "c.db"))
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/cameras", json={"name": "x", "room": "r",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5})
        assert r.status_code == 403
