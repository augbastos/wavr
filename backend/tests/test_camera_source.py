import pytest
from wavr.sources.camera import CameraSource, Detection, classify_posture
from wavr.events import Target
import wavr.sources.camera as _cam

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
    monkeypatch.setattr(camera, "_model", lambda: (lambda frame: [_Result()]))
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
    monkeypatch.setattr(camera, "_model", lambda: (lambda frame: [_Result()]))
    det = camera.yolo_detect("frame", conf_threshold=0.5)
    assert det.count == 1             # only the 0.9 box clears the threshold
    assert det.confidence == 0.9
    det_default = camera.yolo_detect("frame")  # default 0.0 keeps all
    assert det_default.count == 2
    assert det_default.confidence == 0.9

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

    monkeypatch.setattr(camera, "_pose_model", lambda: (lambda frame: [_Result()]))
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
