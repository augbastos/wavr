import asyncio
import pytest
from wavr.sourcemanager import SourceManager
from wavr.sources.camera import CameraSource, Detection


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
