import asyncio
import contextlib
import time

import pytest
from wavr.sources.camera import CameraPrivacySignal, CameraSource, Detection, classify_posture
from wavr.events import Target
from wavr.sources._dhcp_raw import reset_open_guards
import wavr.sources.camera as _cam


@pytest.fixture(autouse=True)
def _reset_open_guards():
    # `_dhcp_raw._timed_out_openers` is a module global shared with camera.py's
    # rtsp_frames (both go through `open_with_timeout`) -- persists for the whole
    # test session by design (that's the point in production). Several tests below
    # deliberately trigger a genuine RTSP open/read timeout, which would otherwise
    # permanently poison a later, unrelated test reusing the same "RTSP open (host)"
    # / "RTSP read (host)" `what` label. Mirrors test_dhcp_source.py's own fixture.
    reset_open_guards()
    yield
    reset_open_guards()


async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out

def _frames_factory(items, released):
    async def frames(url):
        try:
            for f in items:
                yield f
        finally:
            released["v"] = True  # release() proxy — proves deterministic teardown
    return frames

async def test_camera_present_when_person_detected():
    released = {"v": False}
    src = CameraSource("quarto", rtsp_url="rtsp://x",
                       frames=_frames_factory(["frameA"], released),
                       detect=lambda f: Detection(count=1, confidence=0.92))
    [ev] = await _first_n(src, 1)
    assert ev.room == "quarto"
    assert ev.modality == "camera"
    assert ev.presence is True
    assert ev.confidence == 0.92
    assert ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.ts.endswith("+00:00")

async def test_camera_absent_when_no_person():
    released = {"v": False}
    src = CameraSource("quintal", rtsp_url="rtsp://x",
                       frames=_frames_factory(["f"], released),
                       detect=lambda f: Detection(count=0, confidence=0.0))
    [ev] = await _first_n(src, 1)
    assert ev.presence is False
    assert ev.confidence == 0.0

async def test_camera_releases_capture_on_aclose():
    released = {"v": False}
    src = CameraSource("quarto", rtsp_url="rtsp://x",
                       frames=_frames_factory(["f1", "f2", "f3"], released),
                       detect=lambda f: Detection(count=1, confidence=0.5))
    agen = src.events()
    await agen.__anext__()          # pull one event, mid-stream
    await agen.aclose()             # disable → must run frames() finally
    assert released["v"] is True    # RTSP released deterministically, not GC-deferred

async def test_rtsp_frames_reads_then_releases(monkeypatch):
    from wavr.sources import camera
    opened = {"cap": None, "released": False}
    def fake_open(url):
        opened["cap"] = f"cap:{url}"
        return opened["cap"]
    reads = iter([(True, "frame1"), (True, "frame2"), (False, None)])  # (ok, frame); (False,_) ends
    monkeypatch.setattr(camera, "_open_capture", fake_open)
    monkeypatch.setattr(camera, "_read", lambda cap: next(reads))
    monkeypatch.setattr(camera, "_release", lambda cap: opened.__setitem__("released", True))
    got = []
    agen = camera.rtsp_frames("rtsp://cam")
    async for f in agen:
        got.append(f)
    assert got == ["frame1", "frame2"]
    assert opened["released"] is True   # released after the stream ends

def test_yolo_detect_counts_persons(monkeypatch):
    from wavr.sources import camera
    # Fake YOLO result: two boxes, classes [person=0, chair=56], confs [0.9, 0.7]
    class _Boxes:
        cls = [0, 56]
        conf = [0.9, 0.7]
    class _Result:
        boxes = _Boxes()
    monkeypatch.setattr(camera, "_model", lambda: (lambda frame, **kw: [_Result()]))
    det = camera.yolo_detect("frame")
    assert det.count == 1            # only the person box
    assert det.confidence == 0.9

def test_yolo_detect_filters_by_confidence_threshold(monkeypatch):
    from wavr.sources import camera
    # Fake YOLO result: two person boxes (cls=0), confs [0.9, 0.3]
    class _Boxes:
        cls = [0, 0]
        conf = [0.9, 0.3]
    class _Result:
        boxes = _Boxes()
    monkeypatch.setattr(camera, "_model", lambda: (lambda frame, **kw: [_Result()]))
    det = camera.yolo_detect("frame", conf_threshold=0.5)
    assert det.count == 1             # only the 0.9 box clears the threshold
    assert det.confidence == 0.9
    det_default = camera.yolo_detect("frame")  # default 0.0 keeps all
    assert det_default.count == 2
    assert det_default.confidence == 0.9

def test_yolo_detect_calls_model_with_verbose_false(monkeypatch):
    # PERF/LOW fix: ultralytics DEFAULT_CFG.verbose is True -> a log line per
    # predict() call, i.e. per camera frame at a 0.5s interval. yolo_detect
    # must explicitly pass verbose=False to suppress that spam.
    from wavr.sources import camera
    calls = []
    class _Boxes:
        cls = []
        conf = []
    class _Result:
        boxes = _Boxes()
    def fake_model(frame, **kw):
        calls.append(kw)
        return [_Result()]
    monkeypatch.setattr(camera, "_model", lambda: fake_model)
    camera.yolo_detect("frame")
    # conf= is the MEDIUM-HIGH conf-floor fix (see test_yolo_detect_passes_conf_floor_*
    # below) -- assert verbose alongside it rather than an exact-dict equality so this
    # test stays only about the verbose flag.
    assert calls == [{"verbose": False, "conf": 0.01}]

def test_yolo_pose_detect_calls_model_with_verbose_false(monkeypatch):
    # Same fix, pose-predict path.
    from wavr.sources import camera
    calls = []
    class _Boxes:
        cls = []
        conf = []
    class _Result:
        boxes = _Boxes()
        keypoints = None
    def fake_model(frame, **kw):
        calls.append(kw)
        return [_Result()]
    monkeypatch.setattr(camera, "_pose_model", lambda: fake_model)
    camera.yolo_pose_detect("frame", 0.0)
    assert calls == [{"verbose": False, "conf": 0.01}]


# ---- MEDIUM-HIGH conf-floor fix: conf= forwarded to the model, floored at 0.01 -------

def test_yolo_detect_passes_conf_floor_to_model(monkeypatch):
    from wavr.sources import camera
    calls = []
    class _Boxes:
        cls = []
        conf = []
    class _Result:
        boxes = _Boxes()
    def fake_model(frame, **kw):
        calls.append(kw.get("conf"))
        return [_Result()]
    monkeypatch.setattr(camera, "_model", lambda: fake_model)
    camera.yolo_detect("frame", conf_threshold=0.1)
    camera.yolo_detect("frame", conf_threshold=0.0)   # 0.0 floors to 0.01, never a literal 0
    camera.yolo_detect("frame", conf_threshold=0.5)
    assert calls == [0.1, 0.01, 0.5]


def test_yolo_pose_detect_passes_conf_floor_to_model(monkeypatch):
    from wavr.sources import camera
    calls = []
    class _Boxes:
        cls = []
        conf = []
    class _Result:
        boxes = _Boxes()
        keypoints = None
    def fake_model(frame, **kw):
        calls.append(kw.get("conf"))
        return [_Result()]
    monkeypatch.setattr(camera, "_pose_model", lambda: fake_model)
    camera.yolo_pose_detect("frame", confidence=0.15)
    camera.yolo_pose_detect("frame", confidence=0.0)
    assert calls == [0.15, 0.01]


def _real_weights_cached() -> bool:
    """True only if yolov8n.pt is already on disk in the CWD -- this matches how
    `_model()`'s bare "yolov8n.pt" resolves when pytest is run from the repo root
    (this repo's own run command). Guards the real-model regression test below so
    a machine without the weights cached (and possibly no network) SKIPS it
    instead of attempting a network download mid-test-run -- the rest of this
    suite must stay deterministic and network-free everywhere."""
    import os
    return os.path.exists("yolov8n.pt")


@pytest.mark.skipif(not _real_weights_cached(),
                    reason="yolov8n.pt not cached in CWD; skip network-dependent real-model test")
def test_yolo_detect_real_model_surfaces_sub_quarter_conf_detections():
    # HONEST real-model regression (not a hand-rolled fake Results/Boxes object):
    # ultralytics' OWN predict() defaults conf to 0.25 and strips anything below
    # that INSIDE the model call, before yolo_detect's own `>= conf_threshold`
    # filter ever runs -- a mocked `_model()` that ignores its conf= kwarg (as
    # every other test above does) cannot catch that class of regression. This
    # loads the REAL cached yolov8n.pt and proves conf=0.05 is actually forwarded
    # to ultralytics (not silently left at its 0.25 default) by reading back
    # ultralytics' own recorded predictor.args.conf after a real inference call.
    import numpy as np
    from wavr.sources import camera
    _cam._YOLO_MODEL = None
    frame = np.zeros((64, 64, 3), dtype="uint8")   # real ndarray -- a real inference pass
    det = camera.yolo_detect(frame, conf_threshold=0.05)
    assert isinstance(det, camera.Detection)        # the real predict pipeline ran end to end
    model = camera._YOLO_MODEL
    assert model is not None, "real yolov8n.pt failed to load"
    assert model.predictor.args.conf == pytest.approx(0.05), (
        "yolo_detect must forward conf_threshold to ultralytics -- otherwise "
        "ultralytics' own 0.25 default silently strips sub-0.25 detections "
        "before yolo_detect's own filter ever sees them"
    )
    _cam._YOLO_MODEL = None   # don't leak the loaded real model into later tests


def _reset_active():
    _cam._ACTIVE = 0
    _cam._YOLO_MODEL = None

async def test_camera_survives_transient_detect_error():
    _reset_active()
    calls = {"n": 0}
    def detect(f):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad frame")   # transient
        return Detection(count=1, confidence=0.7)
    async def frames(url):
        yield "f"                              # one frame per connection
    src = CameraSource("quarto", frames=frames, detect=detect, reconnect_delay=0)
    [ev] = await _first_n(src, 1)              # 1st frame errors -> reconnect -> 2nd ok
    assert ev.presence is True and calls["n"] == 2

async def test_last_camera_stop_releases_model(monkeypatch):
    _reset_active()
    calls = {"n": 0}
    monkeypatch.setattr(_cam, "release_model", lambda: calls.__setitem__("n", calls["n"] + 1))
    async def frames(url):
        while True:
            yield "f"
    src = CameraSource("quarto", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    agen = src.events()
    await agen.__anext__()
    await agen.aclose()
    assert calls["n"] == 1                      # only/last camera stopped -> released

async def test_model_not_released_while_another_camera_active(monkeypatch):
    _reset_active()
    calls = {"n": 0}
    monkeypatch.setattr(_cam, "release_model", lambda: calls.__setitem__("n", calls["n"] + 1))
    async def frames(url):
        while True:
            yield "f"
    a = CameraSource("quarto", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    b = CameraSource("quintal", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    ag_a, ag_b = a.events(), b.events()
    await ag_a.__anext__(); await ag_b.__anext__()   # _ACTIVE == 2
    await ag_a.aclose()
    assert calls["n"] == 0                            # one still active -> not released
    await ag_b.aclose()
    assert calls["n"] == 1                            # last stopped -> released

def test_release_model_nulls_global_and_is_torch_safe():
    _cam._YOLO_MODEL = "sentinel"
    _cam.release_model()                              # torch absent -> suppressed, no raise
    assert _cam._YOLO_MODEL is None


# ---- Task 6: posture classification + pose-mode wiring ----

# COCO-17 indices: 5,6 shoulders / 11,12 hips / 13,14 knees / 15,16 ankles
def _kp(shoulder_y, hip_y, knee_y, x=100.0, dx=0.0):
    kps = [(0.0, 0.0)] * 17
    kps[5] = (x, shoulder_y); kps[6] = (x + 10, shoulder_y)
    kps[11] = (x + dx, hip_y); kps[12] = (x + dx + 10, hip_y)
    kps[13] = (x + dx, knee_y); kps[14] = (x + dx + 10, knee_y)
    kps[15] = (x + dx, knee_y + 80); kps[16] = (x + dx + 10, knee_y + 80)
    return kps


def test_posture_standing():
    assert classify_posture(_kp(100, 300, 450)) == "standing"   # big hip->knee drop


def test_posture_sitting():
    assert classify_posture(_kp(100, 300, 330)) == "sitting"    # knees near hip level


def test_posture_lying():
    assert classify_posture(_kp(200, 210, 215, dx=300)) == "lying"  # torso horizontal


def test_posture_missing_keypoints_none():
    assert classify_posture([(0.0, 0.0)] * 17) is None


async def test_camera_pose_mode_attaches_targets():
    async def frames(url):
        yield "frame1"

    def detect(frame):
        return Detection(count=1, confidence=0.9)

    def fake_pose(frame, confidence):
        return [Target(id=1, x=None, y=None, posture="sitting", confidence=0.9)]

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0,
                       frames=frames, detect=detect,
                       pose=True, pose_detect=fake_pose)
    [ev] = await _first_n(src, 1)
    assert ev.targets and ev.targets[0].posture == "sitting"


async def test_camera_events_count_matches_detect_when_pose_off():
    # Plain (non-pose) path: SensingEvent.count/confidence come straight from
    # Detection -- the honest per-source count wavr.fusion's COUNTING_MODALITIES
    # (camera/mmwave) trusts. modality is always "camera" -- must stay in that set.
    async def frames(url):
        yield "frame1"

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0,
                       frames=frames, detect=lambda f: Detection(count=3, confidence=0.6))
    [ev] = await _first_n(src, 1)
    assert ev.modality == "camera"
    assert ev.count == 3
    assert ev.confidence == 0.6


async def test_camera_pose_mode_count_derives_from_targets_not_detect():
    # MEDIUM double-inference fix: when pose is ON, count/confidence must be DERIVED
    # from the pose targets (len(targets), max target confidence) -- never from a
    # second yolo_detect() call. `detect` here returns a DELIBERATELY WRONG count/
    # confidence; if the source ever read it, this test would fail loudly instead of
    # silently trusting a stale/disagreeing number.
    calls = []

    async def frames(url):
        yield "frame1"

    def detect(frame):
        calls.append(frame)
        return Detection(count=99, confidence=0.01)   # must never be used

    def fake_pose(frame, confidence):
        return [Target(id=1, x=None, y=None, posture="standing", confidence=0.4),
                Target(id=2, x=None, y=None, posture="sitting", confidence=0.9)]

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0,
                       frames=frames, detect=detect,
                       pose=True, pose_detect=fake_pose)
    [ev] = await _first_n(src, 1)
    assert ev.count == 2                     # len(targets), not detect's 99
    assert ev.confidence == 0.9               # max target confidence, not detect's 0.01
    assert calls == []                        # detect() never invoked -- no double inference


async def test_camera_pose_mode_zero_targets_reports_not_present():
    # Honest empty case: pose ON, zero targets this frame -> count 0, presence False,
    # confidence 0.0 (never a stale/fabricated number).
    async def frames(url):
        yield "frame1"

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0,
                       frames=frames, detect=lambda f: Detection(count=1, confidence=0.9),
                       pose=True, pose_detect=lambda f, c: [])
    [ev] = await _first_n(src, 1)
    assert ev.count == 0
    assert ev.presence is False
    assert ev.confidence == 0.0


async def test_camera_pose_default_off_targets_empty():
    async def frames(url):
        yield "frame1"

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0,
                       frames=frames, detect=lambda f: Detection(count=1, confidence=0.9))
    [ev] = await _first_n(src, 1)
    assert ev.targets == ()                            # pose=False default -> unchanged behavior


def test_yolo_pose_detect_builds_posture_targets(monkeypatch):
    from wavr.sources import camera

    class _Keypoints:
        xy = [[(100.0, 100.0), (110.0, 100.0)] + [(0.0, 0.0)] * 15]  # only shoulders set -> None posture

    class _Boxes:
        cls = [0]
        conf = [0.85]

    class _Result:
        boxes = _Boxes()
        keypoints = _Keypoints()

    monkeypatch.setattr(camera, "_pose_model", lambda: (lambda frame, **kw: [_Result()]))
    targets = camera.yolo_pose_detect("frame", 0.0)
    assert len(targets) == 1
    t = targets[0]
    assert t.x is None and t.y is None
    assert t.confidence == 0.85
    assert t.posture is None                            # missing hip/knee keypoints


async def test_last_camera_stop_releases_pose_model(monkeypatch):
    _reset_active()
    calls = {"n": 0}
    monkeypatch.setattr(_cam, "release_model", lambda: calls.__setitem__("n", calls["n"] + 1))
    async def frames(url):
        while True:
            yield "f"
    src = CameraSource("quarto", frames=frames, detect=lambda f: Detection(1, 0.5),
                       pose=True, pose_detect=lambda f, c: [], interval=0)
    agen = src.events()
    await agen.__anext__()
    await agen.aclose()
    assert calls["n"] == 1                              # both models unload via the same path


def test_release_model_also_nulls_pose_model():
    _cam._YOLO_MODEL = "sentinel"
    _cam._POSE_MODEL = "sentinel"
    _cam.release_model()
    assert _cam._YOLO_MODEL is None
    assert _cam._POSE_MODEL is None


# ---- Spec A: feet point + positioned Target emission ---------------------------

def test_feet_point_is_bottom_centre():
    # bbox (x1,y1,x2,y2) -> ((x1+x2)/2, y2)
    assert _cam.feet_point((10.0, 20.0, 30.0, 80.0)) == (20.0, 80.0)


class _Frame:
    """Minimal frame stand-in: only .shape is read (never the pixels) -- ADR-0002."""
    shape = (720, 1280, 3)   # (h, w, c)


def _pose_result_with_box(xyxy, conf=0.9):
    import types
    return types.SimpleNamespace(
        boxes=types.SimpleNamespace(cls=[0], conf=[conf], xyxy=[xyxy]),
        keypoints=types.SimpleNamespace(xy=[[(0.0, 0.0)] * 17]),  # no usable kpts -> posture None
    )


def test_yolo_pose_detect_positions_target_with_localizer(monkeypatch):
    monkeypatch.setattr(_cam, "_pose_model",
                        lambda: (lambda frame, **kw: [_pose_result_with_box((100.0, 100.0, 300.0, 500.0))]))
    seen = {}
    def fake_localize(feet, img_size):
        seen["feet"] = feet
        seen["img_size"] = img_size
        return (1.5, 2.0, 0.85)           # room-local x, y, position-quality conf
    targets = _cam.yolo_pose_detect(_Frame(), 0.0, localize=fake_localize)
    assert len(targets) == 1
    t = targets[0]
    assert (t.x, t.y) == (1.5, 2.0)
    assert t.confidence == 0.85           # position-quality rides Target.confidence
    assert seen["feet"] == (200.0, 500.0)  # bottom-centre of the box
    assert seen["img_size"] == (1280.0, 720.0)


def test_yolo_pose_detect_none_safe_when_localizer_returns_none(monkeypatch):
    monkeypatch.setattr(_cam, "_pose_model",
                        lambda: (lambda frame, **kw: [_pose_result_with_box((100.0, 100.0, 300.0, 500.0), conf=0.7)]))
    targets = _cam.yolo_pose_detect(_Frame(), 0.0, localize=lambda feet, sz: None)
    assert len(targets) == 1
    t = targets[0]
    assert t.x is None and t.y is None    # ray missed floor -> no fabricated point
    assert t.confidence == 0.7            # falls back to detection confidence


def test_yolo_pose_detect_no_localizer_keeps_xy_none(monkeypatch):
    monkeypatch.setattr(_cam, "_pose_model",
                        lambda: (lambda frame, **kw: [_pose_result_with_box((100.0, 100.0, 300.0, 500.0))]))
    targets = _cam.yolo_pose_detect(_Frame(), 0.0)   # no localizer at all
    assert targets[0].x is None and targets[0].y is None


async def test_camera_emits_positioned_target_end_to_end(monkeypatch):
    import functools
    from wavr.localize import make_localizer, MountPose
    monkeypatch.setattr(_cam, "_pose_model",
                        lambda: (lambda frame, **kw: [_pose_result_with_box((600.0, 300.0, 700.0, 700.0))]))
    # quarto polygon from DEFAULT_MAP; a mount prior -> monocular estimate.
    poly = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]
    loc = make_localizer(poly, mount=MountPose(pos_x=4.2, pos_y=0.0, height=2.4,
                                               tilt_deg=40.0, yaw_deg=45.0, hfov_deg=90.0))
    pose_detect = functools.partial(_cam.yolo_pose_detect, localize=loc)

    async def frames(url):
        yield _Frame()

    src = CameraSource("quarto", rtsp_url="rtsp://x", interval=0, frames=frames,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       pose=True, pose_detect=pose_detect)
    [ev] = await _first_n(src, 1)
    assert ev.targets, "camera should emit a target"
    t = ev.targets[0]
    assert t.x is not None and t.y is not None      # POSITIONED, not room-centred
    assert t.confidence == pytest.approx(0.45)      # monocular position-quality


def test_camera_factory_enables_pose_when_calibrated():
    # Directly verify the app wiring: a camera with a stored mount -> pose ON + a
    # localizer bound into pose_detect; without one -> pose OFF (room-centred).
    from wavr.app import _camera_factory
    from wavr.config import load_config
    from wavr.localize import MountPose
    from wavr.housemap import DEFAULT_MAP
    cfg = load_config()
    cam = {"name": "cam_q", "room": "quarto", "rtsp_url": "rtsp://x", "confidence": 0.4}

    class _Calib:
        def __init__(self, row):
            self._row = row
        def get(self, name):
            return self._row

    calibrated = _Calib({"mount": MountPose(pos_x=4.2, pos_y=0.0), "homography": None,
                         "img_w": None, "img_h": None})
    src = _camera_factory(cam, cfg, None, calibrated, DEFAULT_MAP)()
    assert src._pose is True
    assert src._pose_detect is not None

    uncalibrated = _Calib(None)
    src2 = _camera_factory(cam, cfg, None, uncalibrated, DEFAULT_MAP)()
    assert src2._pose is False           # no calibration -> unchanged, room-centred


# ---- F3: edge-triggered health hook (name+bool only, never a frame) -------------

async def test_health_hook_edge_triggers_down_then_recovery():
    _reset_active()
    calls = []
    def on_health(name, healthy):
        calls.append((name, healthy))

    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            return          # 1st connection yields NO frame -> stream ends -> down edge
            yield           # (unreachable) make this an async generator
        yield "frameA"      # 2nd connection yields a frame -> recovery edge

    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q",
                       frames=frames, detect=lambda f: Detection(count=1, confidence=0.9),
                       on_health=on_health, unhealthy_secs=0.0,
                       reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)                       # pulls the recovery frame's event
    assert ev.presence is True                          # normal event still flows
    assert ("cam_q", False) in calls                    # down fired
    assert ("cam_q", True) in calls                     # recovery fired
    assert calls.count(("cam_q", False)) == 1           # edge-triggered: exactly once
    assert calls.count(("cam_q", True)) == 1
    # ADR-0002: the callback ever only receives (str name, bool) -- NEVER a frame.
    for name, healthy in calls:
        assert isinstance(name, str) and isinstance(healthy, bool)


async def test_health_hook_not_fired_before_unhealthy_secs():
    _reset_active()
    calls = []
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            return          # 1st connection: brief blip, no frame
            yield
        yield "frameA"      # 2nd connection recovers quickly

    # unhealthy_secs is large, so a blip that recovers before the threshold must NOT
    # report down (nor a spurious recovery, since it was never reported down).
    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(1, 0.5),
                       on_health=lambda n, h: calls.append((n, h)),
                       unhealthy_secs=999.0, reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert calls == []              # blip < unhealthy_secs -> no down, no recovery


async def test_health_hook_absent_callback_is_noop():
    _reset_active()
    async def frames(url):
        yield "f"
    # No on_health wired -> behaves exactly like before (no crash, event flows).
    src = CameraSource("quarto", rtsp_url="rtsp://x", frames=frames,
                       detect=lambda f: Detection(1, 0.8), interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True


async def test_health_hook_bad_callback_never_breaks_source():
    _reset_active()
    def bad(name, healthy):
        raise RuntimeError("monitor down")
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            return
            yield
        yield "frameA"
    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(1, 0.9), on_health=bad,
                       unhealthy_secs=0.0, reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)   # a throwing callback must not break the loop
    assert ev.presence is True


# ---- Tapo privacy mode: CameraPrivacySignal edge hook (never cries wolf) --------

async def test_privacy_signal_edge_triggers_then_recovers():
    _reset_active()
    calls = []
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            raise CameraPrivacySignal("covered")
            yield          # (unreachable) keeps this an async generator
        yield "frameA"     # 2nd connection: real frame -> privacy recovery edge

    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       on_privacy=lambda n, a: calls.append((n, a)),
                       reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True                              # normal event still flows
    assert calls == [("cam_q", True), ("cam_q", False)]      # edge-triggered exactly once each


async def test_privacy_signal_never_reports_down_across_many_cycles():
    # Even with unhealthy_secs=0.0 (which would immediately down-latch a generic
    # failure), a camera that keeps producing the privacy signature must NEVER be
    # reported unhealthy -- an indefinitely-covered camera is not a fault.
    _reset_active()
    calls = []
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] <= 5:
            raise CameraPrivacySignal("covered")
            yield
        yield "frameA"

    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       on_health=lambda n, h: calls.append(("health", n, h)),
                       on_privacy=lambda n, a: calls.append(("privacy", n, a)),
                       unhealthy_secs=0.0, reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert not any(c[0] == "health" for c in calls)           # never cried wolf
    assert calls.count(("privacy", "cam_q", True)) == 1       # edge-triggered once
    assert ("privacy", "cam_q", False) in calls                # recovery on the real frame


async def test_privacy_claim_drops_on_subsequent_genuine_failure():
    # privacy (cycle 1) -> a DIFFERENT, genuine failure (cycle 2, not the privacy
    # signature) -> the stale privacy claim is dropped and the ordinary down latch
    # takes over normally (unhealthy_secs=0.0) -> recovers on the real frame (cycle 3).
    _reset_active()
    calls = []
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            raise CameraPrivacySignal("covered")
            yield
        if state["conn"] == 2:
            raise RuntimeError("genuinely down now")
            yield
        yield "frameA"

    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       on_health=lambda n, h: calls.append(("health", n, h)),
                       on_privacy=lambda n, a: calls.append(("privacy", n, a)),
                       unhealthy_secs=0.0, reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert calls == [
        ("privacy", "cam_q", True),
        ("privacy", "cam_q", False),
        ("health", "cam_q", False),
        ("health", "cam_q", True),
    ]


async def test_privacy_signal_clears_a_stale_down_latch():
    # A camera already latched DOWN (genuine fault) that then starts reproducing the
    # privacy signature must have its down-latch cleared -- it is not both.
    _reset_active()
    calls = []
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            raise RuntimeError("dead")
            yield
        if state["conn"] == 2:
            raise CameraPrivacySignal("covered")
            yield
        yield "frameA"

    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       on_health=lambda n, h: calls.append(("health", n, h)),
                       on_privacy=lambda n, a: calls.append(("privacy", n, a)),
                       unhealthy_secs=0.0, reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert calls == [
        ("health", "cam_q", False),     # cycle 1: genuine fault -> down latched
        ("health", "cam_q", True),      # cycle 2: privacy signal clears the stale down
        ("privacy", "cam_q", True),
        ("privacy", "cam_q", False),    # cycle 3: real frame -> privacy recovers too
    ]


async def test_privacy_hook_absent_callback_is_noop():
    _reset_active()
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            raise CameraPrivacySignal("covered")
            yield
        yield "frameA"
    # No on_privacy wired -> must not crash; behaves exactly like before this feature.
    src = CameraSource("quarto", rtsp_url="rtsp://x", frames=frames,
                       detect=lambda f: Detection(1, 0.8), reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is True


async def test_privacy_hook_bad_callback_never_breaks_source():
    _reset_active()
    def bad(name, active):
        raise RuntimeError("monitor down")
    state = {"conn": 0}
    async def frames(url):
        state["conn"] += 1
        if state["conn"] == 1:
            raise CameraPrivacySignal("covered")
            yield
        yield "frameA"
    src = CameraSource("quarto", rtsp_url="rtsp://x", name="cam_q", frames=frames,
                       detect=lambda f: Detection(1, 0.9), on_privacy=bad,
                       reconnect_delay=0, interval=0)
    [ev] = await _first_n(src, 1)    # a throwing callback must not break the loop
    assert ev.presence is True


# ---- Tapo privacy mode: rtsp_frames' real-adapter signature ---------------------

async def test_rtsp_frames_raises_privacy_signal_when_opened_but_empty(monkeypatch):
    from wavr.sources import camera
    class FakeCap:
        def isOpened(self): return True
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: (False, None))   # zero frames, ever
    monkeypatch.setattr(camera, "_release", lambda cap: None)
    agen = camera.rtsp_frames("rtsp://cam")
    with pytest.raises(camera.CameraPrivacySignal):
        async for _ in agen:
            pass


async def test_rtsp_frames_no_privacy_signal_when_never_opened(monkeypatch):
    # A capture that never opens (isOpened() False -- e.g. wrong IP/creds/camera off
    # the network) must NOT raise the privacy signal -- it ends cleanly, unaffected,
    # exactly like before this feature (feeds the ordinary down-latch path).
    from wavr.sources import camera
    class FakeCap:
        def isOpened(self): return False
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: (False, None))
    monkeypatch.setattr(camera, "_release", lambda cap: None)
    agen = camera.rtsp_frames("rtsp://cam")
    got = []
    async for f in agen:
        got.append(f)
    assert got == []


async def test_rtsp_frames_no_privacy_signal_when_frames_flowed(monkeypatch):
    # A capture that opened AND produced at least one real frame before ending is an
    # ordinary transient blip/reconnect -- not a privacy candidate.
    from wavr.sources import camera
    class FakeCap:
        def isOpened(self): return True
    reads = iter([(True, "f1"), (False, None)])
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: next(reads))
    monkeypatch.setattr(camera, "_release", lambda cap: None)
    agen = camera.rtsp_frames("rtsp://cam")
    got = []
    async for f in agen:
        got.append(f)
    assert got == ["f1"]


def test_is_opened_never_raises_on_odd_object():
    from wavr.sources import camera
    assert camera._is_opened("not a capture") is False
    assert camera._is_opened(object()) is False


# ---- HIGH stalled-read-leak fix: open()/read() are bounded, never hang forever --------

async def test_rtsp_frames_stalled_read_times_out_and_releases(monkeypatch):
    # The G9 field bug this fixes: a stalled/powered-off-but-ARP-resident camera's
    # `cap.read()` genuinely never returns. Before this fix, `await
    # asyncio.to_thread(_read, cap)` had no timeout at all -- this await, and
    # therefore this generator's `finally: _release(cap)`, would never run. Now
    # CAMERA_READ_TIMEOUT bounds it, so the capture IS released promptly, not after
    # the full stall.
    from wavr.sources import camera
    monkeypatch.setattr(camera, "CAMERA_READ_TIMEOUT", 0.05)
    class FakeCap:
        def isOpened(self): return True
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: time.sleep(5))  # far > 0.05s -- must not be awaited fully
    released = {"v": False}
    monkeypatch.setattr(camera, "_release", lambda cap: released.__setitem__("v", True))

    agen = camera.rtsp_frames("rtsp://stalled-cam")
    with pytest.raises(OSError):
        await agen.__anext__()
    assert released["v"] is True        # finally ran promptly, not after the 5s stall


async def test_rtsp_frames_stalled_open_times_out_without_hanging(monkeypatch):
    from wavr.sources import camera
    monkeypatch.setattr(camera, "CAMERA_OPEN_TIMEOUT", 0.05)
    monkeypatch.setattr(camera, "_open_capture", lambda url: time.sleep(5))
    agen = camera.rtsp_frames("rtsp://stalled-open-cam")
    with pytest.raises(OSError):
        await agen.__anext__()


async def test_rtsp_frames_read_timeout_sticky_guard_blocks_retry(monkeypatch):
    # Bounds worst-case leaked-thread count for a genuinely-wedged _read to ONE per
    # host for the process's lifetime, instead of leaking a fresh thread every
    # reconnect_delay cycle forever (mirrors _dhcp_raw's own sticky-guard test,
    # test_raw_dhcp_listen_does_not_retry_after_a_genuine_timeout).
    from wavr.sources import camera
    monkeypatch.setattr(camera, "CAMERA_READ_TIMEOUT", 0.05)
    class FakeCap:
        def isOpened(self): return True
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: time.sleep(5))
    monkeypatch.setattr(camera, "_release", lambda cap: None)

    agen1 = camera.rtsp_frames("rtsp://sticky-cam")
    with pytest.raises(OSError):
        await agen1.__anext__()

    # A second connection attempt against the SAME host must fail fast WITHOUT
    # calling _read again -- that call would leak yet another background thread.
    calls = []
    monkeypatch.setattr(camera, "_read", lambda cap: calls.append(1))
    agen2 = camera.rtsp_frames("rtsp://sticky-cam")
    with pytest.raises(OSError):
        await agen2.__anext__()
    assert calls == []


async def test_rtsp_frames_timeout_error_never_leaks_credentials(monkeypatch):
    # The `what` label surfaces inside the raised OSError's message -- it must be
    # built from the HOST only (rtsp_host()), never the raw url, since an rtsp://
    # URL can carry embedded username:password credentials.
    from wavr.sources import camera
    monkeypatch.setattr(camera, "CAMERA_OPEN_TIMEOUT", 0.05)
    monkeypatch.setattr(camera, "_open_capture", lambda url: time.sleep(5))
    agen = camera.rtsp_frames("rtsp://admin:sup3rSecret@10.0.0.9:554/stream1")
    with pytest.raises(OSError) as exc_info:
        await agen.__anext__()
    msg = str(exc_info.value)
    assert "sup3rSecret" not in msg
    assert "admin" not in msg
    assert "10.0.0.9" in msg            # the bare host is fine to surface


async def test_camera_source_disable_task_completes_promptly_on_stalled_read(monkeypatch):
    # End-to-end mirror of SourceManager._kill's real disable pattern (cancel the
    # task driving events(), then await it with a bound) against a `_read` that
    # genuinely never returns -- proves "a stalled read can't hang a disable" at
    # the point that actually matters in production, not just at the rtsp_frames
    # unit level above.
    _reset_active()
    from wavr.sources import camera
    monkeypatch.setattr(camera, "CAMERA_READ_TIMEOUT", 0.05)
    class FakeCap:
        def isOpened(self): return True
    monkeypatch.setattr(camera, "_open_capture", lambda url: FakeCap())
    monkeypatch.setattr(camera, "_read", lambda cap: time.sleep(5))  # genuinely wedged
    released = {"n": 0}
    monkeypatch.setattr(camera, "_release", lambda cap: released.__setitem__("n", released["n"] + 1))

    src = CameraSource("quarto", rtsp_url="rtsp://x", detect=lambda f: Detection(0, 0.0),
                       reconnect_delay=0)
    agen = src.events()

    async def _run():
        async for _ev in agen:
            pass

    task = asyncio.ensure_future(_run())
    await asyncio.sleep(0.02)           # let it actually enter the stalled read
    task.cancel()
    # SourceManager._kill's own pattern: cancel + a bounded wait. 2s is far below
    # the 5s stall -- if this times out, the disable genuinely hung.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert released["n"] >= 1           # the capture WAS released, not abandoned
