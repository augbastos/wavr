"""Walk-to-calibrate wizard backend (Spec A): the feet-pixel sample sink, the pose
`on_feet` seam, the known-floor-spots helper, and the calib-sample / calib-spots /
calib-session endpoints.

ADR-0002 is the load-bearing invariant under test: every path here carries ONLY a
pixel COORDINATE + image DIMENSIONS + a confidence scalar -- NEVER a frame/crop/image.
No real camera or frame is used; detections are mocked.
"""
import types

import pytest
from fastapi.testclient import TestClient

import wavr.sources.camera as _cam
from wavr.app import create_app, _camera_factory
from wavr.calib_sample import CalibSampleStore
from wavr.camera_store import CameraStore
from wavr.config import load_config
from wavr.housemap import DEFAULT_MAP
from wavr.localize import floor_spots_for_room, homography_from_points


# --------------------------------------------------------------------------- #
# CalibSampleStore: coordinate-only, TTL'd, never a frame.
# --------------------------------------------------------------------------- #

def test_store_records_and_reads_feet_pixel():
    s = CalibSampleStore()
    s.record("cam_q", (200.0, 500.0), 1280, 720, 0.9)
    got = s.latest("cam_q")
    assert got is not None
    assert got["feet_px"] == (200.0, 500.0)
    assert got["img_w"] == 1280.0 and got["img_h"] == 720.0
    assert got["confidence"] == 0.9
    # ADR-0002: the record carries ONLY a coordinate + dims + scalars, never a frame.
    assert set(got) == {"feet_px", "img_w", "img_h", "confidence", "age_s"}


def test_store_returns_none_when_no_sample():
    assert CalibSampleStore().latest("cam_q") is None


def test_store_stale_sample_reads_as_none():
    # A sample older than max_age_s must read as None so the wizard can't capture a
    # ghost position from before the walker moved. max_age_s=-1 forces every sample stale.
    s = CalibSampleStore()
    s.record("cam_q", (10.0, 20.0), 640, 480, 0.8)
    assert s.latest("cam_q", max_age_s=-1.0) is None      # any age > -1 -> stale
    assert s.latest("cam_q", max_age_s=100.0) is not None  # generous window -> fresh


def test_store_drops_malformed_sample():
    s = CalibSampleStore()
    s.record("cam_q", (float("nan"), 20.0), 640, 480, 0.8)   # non-finite feet
    s.record("cam_q", (10.0, 20.0), 0, 480, 0.8)             # zero width
    s.record("cam_q", "not-a-point", 640, 480, 0.8)          # malformed
    assert s.latest("cam_q") is None


def test_store_clear():
    s = CalibSampleStore()
    s.record("cam_q", (1.0, 2.0), 640, 480, 0.7)
    s.clear("cam_q")
    assert s.latest("cam_q") is None


def test_store_bounds_camera_count():
    s = CalibSampleStore(max_cameras=2)
    s.record("a", (1.0, 1.0), 10, 10, 0.5)
    s.record("b", (2.0, 2.0), 10, 10, 0.5)
    s.record("c", (3.0, 3.0), 10, 10, 0.5)   # evicts the oldest ("a")
    assert s.latest("a") is None
    assert s.latest("c") is not None


# --------------------------------------------------------------------------- #
# yolo_pose_detect on_feet seam: emits the highest-confidence person's feet pixel.
# --------------------------------------------------------------------------- #

class _Frame:
    """Minimal frame stand-in: only .shape is read (never the pixels) -- ADR-0002."""
    shape = (720, 1280, 3)   # (h, w, c)


def _pose_result(boxes):
    n = len(boxes)
    return types.SimpleNamespace(
        boxes=types.SimpleNamespace(
            cls=[0] * n,
            conf=[c for _xyxy, c in boxes],
            xyxy=[xyxy for xyxy, _c in boxes],
        ),
        keypoints=types.SimpleNamespace(xy=[[(0.0, 0.0)] * 17] * n),
    )


def test_on_feet_emits_highest_confidence_person(monkeypatch):
    # two people; on_feet must receive the more-confident one's FEET pixel + dims.
    boxes = [((100.0, 100.0, 300.0, 500.0), 0.6),
             ((600.0, 200.0, 800.0, 700.0), 0.95)]
    monkeypatch.setattr(_cam, "_pose_model", lambda: (lambda frame: [_pose_result(boxes)]))
    seen = []
    _cam.yolo_pose_detect(_Frame(), 0.0,
                          on_feet=lambda feet, size, conf: seen.append((feet, size, conf)))
    assert len(seen) == 1
    feet, size, conf = seen[0]
    assert feet == (700.0, 700.0)          # bottom-centre of the 0.95 box
    assert size == (1280.0, 720.0)
    assert conf == 0.95
    # ADR-0002: on_feet is handed a coordinate + dims + scalar, never a frame.
    assert isinstance(feet, tuple) and len(feet) == 2


def test_on_feet_not_called_without_person(monkeypatch):
    monkeypatch.setattr(_cam, "_pose_model", lambda: (lambda frame: [_pose_result([])]))
    seen = []
    _cam.yolo_pose_detect(_Frame(), 0.0, on_feet=lambda *a: seen.append(a))
    assert seen == []


def test_on_feet_coexists_with_localizer(monkeypatch):
    boxes = [((100.0, 100.0, 300.0, 500.0), 0.9)]
    monkeypatch.setattr(_cam, "_pose_model", lambda: (lambda frame: [_pose_result(boxes)]))
    seen = []
    targets = _cam.yolo_pose_detect(
        _Frame(), 0.0,
        localize=lambda feet, size: (1.5, 2.0, 0.85),
        on_feet=lambda feet, size, conf: seen.append(feet))
    assert targets[0].x == 1.5 and targets[0].y == 2.0    # positioned via localizer
    assert seen == [(200.0, 500.0)]                       # AND feet pixel sampled


# --------------------------------------------------------------------------- #
# floor_spots_for_room: centroid + corners, non-collinear, deduped.
# --------------------------------------------------------------------------- #

def test_floor_spots_centroid_plus_corners():
    poly = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]   # quarto (DEFAULT_MAP)
    spots = floor_spots_for_room(poly)
    assert len(spots) == 5                       # centroid + 4 corners
    assert spots[0] == pytest.approx((5.95, 1.5))  # centroid first
    assert set(spots[1:]) == {(4.2, 0.0), (7.7, 0.0), (7.7, 3.0), (4.2, 3.0)}


def test_floor_spots_are_non_collinear_solvable():
    # The 4 corners must be non-collinear enough to solve a homography (the degeneracy
    # guard would raise otherwise): pair them with 4 distinct image pixels.
    poly = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]
    floor = floor_spots_for_room(poly)[1:]        # the corners
    img = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
    h = homography_from_points(img, floor)        # raises on degenerate -> must not
    assert h.shape == (3, 3)


def test_floor_spots_degenerate_polygon_empty():
    assert floor_spots_for_room([[0.0, 0.0], [1.0, 1.0]]) == []   # < 3 vertices
    assert floor_spots_for_room([]) == []


def test_floor_spots_dedup_coincident_vertices():
    # A polygon with a duplicated vertex must not yield a duplicate spot.
    poly = [[0.0, 0.0], [2.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    spots = floor_spots_for_room(poly)
    assert len(spots) == len(set(spots))          # no coincident spots


# --------------------------------------------------------------------------- #
# _camera_factory sampling wiring: pose ON + feet recorded even with NO calibration.
# --------------------------------------------------------------------------- #

def test_factory_sampling_records_feet_without_calibration(monkeypatch):
    cfg = load_config()
    cam = {"name": "cam_q", "room": "quarto", "rtsp_url": "rtsp://x", "confidence": 0.0}

    class _NoCalib:
        def get(self, name):
            return None

    store = CalibSampleStore()
    src = _camera_factory(cam, cfg, None, _NoCalib(), DEFAULT_MAP,
                          sample_store=store, sampling=True)()
    assert src._pose is True                       # sampling forces pose ON
    assert src._pose_detect is not None
    # Drive the built pose_detect with a mocked detection and confirm the feet pixel
    # lands in the store (the on_feet closure fired). No frame involved.
    boxes = [((600.0, 300.0, 700.0, 700.0), 0.9)]
    monkeypatch.setattr(_cam, "_pose_model", lambda: (lambda frame: [_pose_result(boxes)]))
    src._pose_detect(_Frame(), 0.0)
    got = store.latest("cam_q")
    assert got is not None and got["feet_px"] == (650.0, 700.0)


def test_factory_no_sampling_is_unchanged():
    # Without sampling + no calibration, pose stays OFF (byte-identical to before).
    cfg = load_config()
    cam = {"name": "cam_q", "room": "quarto", "rtsp_url": "rtsp://x", "confidence": 0.0}

    class _NoCalib:
        def get(self, name):
            return None

    src = _camera_factory(cam, cfg, None, _NoCalib(), DEFAULT_MAP)()
    assert src._pose is False


# --------------------------------------------------------------------------- #
# Endpoints: calib-sample, calib-spots, calib-session.
# --------------------------------------------------------------------------- #

_SEED = {"name": "cam_q", "room": "quarto",
         "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}


def _client(tmp_path, monkeypatch, seed=True):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    # Point the house map at a nonexistent path so load_house_map falls back to
    # DEFAULT_MAP (which has the 'quarto' room), keeping the test hermetic + independent
    # of any repo-root house.json.
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "nohouse.json"))
    store = CameraStore(str(tmp_path / "cams.db"))
    if seed:
        store.add(**_SEED)
    app = create_app(sources=[], camera_store=store)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_calib_sample_null_when_no_detection(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        body = c.get("/api/cameras/cam_q/calib-sample").json()
        assert body["camera"] == "cam_q"
        assert body["person"] is False
        assert body["feet_px"] is None
        assert body["img_w"] is None and body["img_h"] is None


def test_calib_sample_surfaces_recorded_feet_pixel(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        # Record a coordinate through the same in-memory store the endpoint reads.
        c.app.state.calib_sample.record("cam_q", (321.0, 654.0), 1280, 720, 0.88)
        body = c.get("/api/cameras/cam_q/calib-sample").json()
        assert body["person"] is True
        assert body["feet_px"] == [321.0, 654.0]
        assert body["img_w"] == 1280.0 and body["img_h"] == 720.0
        assert body["confidence"] == 0.88
        # ADR-0002: the response is a coordinate + dims + scalar -- NO frame/image key.
        assert set(body) == {"camera", "person", "feet_px", "img_w", "img_h", "confidence"}


def test_calib_sample_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, seed=False) as c:
        assert c.get("/api/cameras/nope/calib-sample").status_code == 404


def test_calib_spots_returns_centre_and_corners(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        body = c.get("/api/cameras/cam_q/calib-spots").json()
        assert body["room"] == "quarto"
        spots = body["spots"]
        assert len(spots) == 5
        assert spots[0]["label"] == "centre"
        assert {s["label"] for s in spots[1:]} == {"corner-1", "corner-2", "corner-3", "corner-4"}
        assert (spots[0]["x"], spots[0]["y"]) == pytest.approx((5.95, 1.5))


def test_calib_spots_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, seed=False) as c:
        assert c.get("/api/cameras/nope/calib-spots").status_code == 404


def test_walk_collected_points_solve_and_persist_homography(tmp_path, monkeypatch):
    # Integration: use the wizard's real floor spots (calib-spots) as floor_points and
    # a plausible set of captured feet pixels as image_points; the EXISTING PUT
    # /calibration solves + persists the homography.
    with _client(tmp_path, monkeypatch) as c:
        spots = c.get("/api/cameras/cam_q/calib-spots").json()["spots"][1:]  # corners
        floor_points = [[s["x"], s["y"]] for s in spots]
        image_points = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
        r = c.put("/api/cameras/cam_q/calibration",
                  json={"image_points": image_points, "floor_points": floor_points,
                        "img_w": 1280, "img_h": 720})
        assert r.status_code == 200
        body = r.json()
        assert body["localizes"] is True and len(body["homography"]) == 9
        # persisted: a fresh GET reflects it
        assert c.get("/api/cameras/cam_q/calibration").json()["localizes"] is True


def test_calib_session_toggles_sampling_flag(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        # Toggle the system OFF first so starting a session does NOT spawn a real camera
        # task on a bogus rtsp url (this test asserts the control-plane response only).
        c.post("/api/system/toggle", json={"on": False})
        start = c.post("/api/cameras/cam_q/calib-session", json={"active": True}).json()
        assert start["sampling"] is True
        # end the session -> sampling off + store cleared
        c.app.state.calib_sample.record("cam_q", (1.0, 2.0), 640, 480, 0.7)
        stop = c.post("/api/cameras/cam_q/calib-session", json={"active": False}).json()
        assert stop["sampling"] is False
        assert c.app.state.calib_sample.latest("cam_q") is None   # cleared on stop


def test_calib_session_requires_local_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    store = CameraStore(str(tmp_path / "cams.db"))
    store.add(**_SEED)
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/cameras/cam_q/calib-session", json={"active": True})
        assert r.status_code == 403


def test_calib_session_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, seed=False) as c:
        r = c.post("/api/cameras/nope/calib-session", json={"active": True})
        assert r.status_code == 404
