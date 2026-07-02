import pytest
from wavr.sources.camera import CameraSource, Detection

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
