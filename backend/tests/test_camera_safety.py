import asyncio
import pytest
from wavr.sourcemanager import SourceManager
from wavr.sources.camera import CameraSource, Detection


def test_default_sources_register_both_cameras_boot_off(monkeypatch):
    for v in ("WAVR_CAM_QUARTO_URL", "WAVR_CAM_QUINTAL_URL"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    from wavr.app import _default_sources
    srcs = {name: enabled for name, factory, enabled in _default_sources(load_config())}
    assert srcs["camera_quarto"] is False   # SAFETY: boot OFF
    assert srcs["camera_quintal"] is False   # SAFETY: boot OFF


async def test_camera_toggle_on_then_off_releases_rtsp():
    released = {"v": False}
    async def frames(url):
        try:
            while True:
                yield "frame"
                await asyncio.sleep(0.001)
        finally:
            released["v"] = True   # RTSP release proxy
    events = []
    async def on_event(ev):
        events.append(ev)
    mgr = SourceManager(on_event)
    mgr.register("camera_quarto",
                 lambda: CameraSource("quarto", "rtsp://x", frames=frames,
                                      detect=lambda f: Detection(1, 0.8), interval=0),
                 enabled=False)                     # boots OFF
    await mgr.start()
    assert all(not s["active"] for s in mgr.status()["sources"])  # nothing reading while OFF
    await mgr.set_enabled("camera_quarto", True)    # conscious enable
    await asyncio.sleep(0.02)
    assert any(s["name"] == "camera_quarto" and s["active"] for s in mgr.status()["sources"])
    assert events and events[0].modality == "camera"
    await mgr.set_enabled("camera_quarto", False)   # kill-switch
    await asyncio.sleep(0.01)
    assert released["v"] is True                    # HARD kill: RTSP released on disable
    await mgr.stop()
