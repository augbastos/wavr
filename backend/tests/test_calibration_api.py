"""Spec A calibration endpoints: GET/PUT/DELETE /api/cameras/{name}/calibration.

Uses TestClient with an injected CameraStore (tmp db) and WAVR_DB pointed at a tmp
file so the CalibrationStore never touches the repo's wavr.db. The camera lives in
`quarto`, whose polygon comes from the default house map (DEFAULT_MAP).
"""
import os

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore

_SEED = {"name": "cam_q", "room": "quarto",
         "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}


def _client(tmp_path, monkeypatch, seed=True):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))   # isolate calib sqlite
    store = CameraStore(str(tmp_path / "cams.db"))
    if seed:
        store.add(**_SEED)
    app = create_app(sources=[], camera_store=store)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


# A non-degenerate 4-point marking: image pixels -> quarto floor corners (metres).
_IMG_PTS = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
_FLOOR_PTS = [[4.2, 3.0], [7.7, 3.0], [7.7, 0.0], [4.2, 0.0]]


def test_get_calibration_default_is_empty(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        body = c.get("/api/cameras/cam_q/calibration").json()
        assert body["camera"] == "cam_q"
        assert body["mount"] is None and body["homography"] is None
        assert body["localizes"] is False


def test_get_calibration_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, seed=False) as c:
        assert c.get("/api/cameras/nope/calibration").status_code == 404


def test_put_mount_enables_localization(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        mount = {"pos_x": 4.2, "pos_y": 0.0, "height": 2.4, "tilt_deg": 35.0,
                 "yaw_deg": 45.0, "hfov_deg": 90.0}
        r = c.put("/api/cameras/cam_q/calibration", json={"mount": mount})
        assert r.status_code == 200
        body = r.json()
        assert body["localizes"] is True
        assert body["mount"]["pos_x"] == 4.2 and body["mount"]["tilt_deg"] == 35.0
        # persisted: a fresh GET reflects it
        assert c.get("/api/cameras/cam_q/calibration").json()["mount"]["yaw_deg"] == 45.0
        # calibration never auto-enables the camera (still boot-OFF)
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert sysrc["cam_q"]["enabled"] is False


def test_put_mount_out_of_range_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"mount": {"pos_x": 0.0, "pos_y": 0.0, "tilt_deg": 500.0}})
        assert r.status_code == 422
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is False


def test_put_mount_huge_int_literal_is_422_not_500(tmp_path, monkeypatch):
    # Audit HIGH regression: a raw `10**400`-shaped JSON int used to raise an
    # unhandled OverflowError deep in validate_mount -- an unhandled 500. Must be a
    # clean 422, same as any other malformed mount value.
    with _client(tmp_path, monkeypatch) as c:
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"mount": {"pos_x": 10**400, "pos_y": 0.0}})
        assert r.status_code == 422
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is False


def test_put_homography_huge_int_literal_correspondence_is_422_not_500(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        img_pts = [[10**400, 600.0]] + _IMG_PTS[1:]
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": img_pts, "floor_points": _FLOOR_PTS,
                        "img_w": 1280, "img_h": 720})
        assert r.status_code == 422


def test_put_four_point_homography_round_trips(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": _IMG_PTS, "floor_points": _FLOOR_PTS,
                        "img_w": 1280, "img_h": 720})
        assert r.status_code == 200
        body = r.json()
        assert body["localizes"] is True
        assert len(body["homography"]) == 9
        assert body["img_w"] == 1280 and body["img_h"] == 720


def test_put_degenerate_homography_rejected(tmp_path, monkeypatch):
    # All four image points collinear -> degenerate -> 422, never persisted.
    with _client(tmp_path, monkeypatch) as c:
        collinear = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": collinear, "floor_points": _FLOOR_PTS,
                        "img_w": 1280, "img_h": 720})
        assert r.status_code == 422
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is False


def test_put_too_few_points_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": _IMG_PTS[:3], "floor_points": _FLOOR_PTS[:3],
                        "img_w": 1280, "img_h": 720})
        assert r.status_code == 422


def test_put_homography_missing_img_size_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": _IMG_PTS, "floor_points": _FLOOR_PTS})
        assert r.status_code == 422


def test_put_empty_body_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.put("/api/cameras/cam_q/calibration", json={}).status_code == 400


def test_put_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, seed=False) as c:
        r = c.put("/api/cameras/nope/calibration",
                  json={"mount": {"pos_x": 0.0, "pos_y": 0.0}})
        assert r.status_code == 404


def test_calibration_requires_local_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    store = CameraStore(str(tmp_path / "cams.db"))
    store.add(**_SEED)
    with TestClient(create_app(sources=[], camera_store=store)) as c:  # no X-Wavr-Local
        assert c.put("/api/cameras/cam_q/calibration",
                     json={"mount": {"pos_x": 0.0, "pos_y": 0.0}}).status_code == 403
        assert c.delete("/api/cameras/cam_q/calibration").status_code == 403


def test_delete_calibration_reverts_to_room_centred(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        c.put("/api/cameras/cam_q/calibration",
              json={"mount": {"pos_x": 4.2, "pos_y": 0.0}})
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is True
        r = c.delete("/api/cameras/cam_q/calibration")
        assert r.status_code == 200 and r.json()["removed"] is True
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is False


def test_calibration_writes_no_image_file(tmp_path, monkeypatch):
    # ADR-0002: calibration is coordinates + matrices only. No frame/snapshot is ever
    # written to disk by any calibration operation.
    with _client(tmp_path, monkeypatch) as c:
        c.put("/api/cameras/cam_q/calibration",
              json={"image_points": _IMG_PTS, "floor_points": _FLOOR_PTS,
                    "img_w": 1280, "img_h": 720})
        c.put("/api/cameras/cam_q/calibration",
              json={"mount": {"pos_x": 4.2, "pos_y": 0.0}})
        c.get("/api/cameras/cam_q/calibration")
    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".frame", ".raw"}
    for root, _dirs, files in os.walk(tmp_path):
        for f in files:
            assert os.path.splitext(f)[1].lower() not in image_exts, f"image file written: {f}"
