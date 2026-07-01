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
