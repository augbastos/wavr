import asyncio

import pytest

from wavr.events import SensingEvent
from wavr.sourcemanager import SourceManager


class FakeSource:
    def __init__(self, room):
        self.room = room

    async def events(self):
        while True:
            yield SensingEvent(self.room, "sim", True, 1.0, None, None, 0.5,
                               "2026-07-01T10:00:00+00:00")
            await asyncio.sleep(0.001)


async def test_start_runs_enabled_sources_and_feeds_on_event():
    got = []
    m = SourceManager(lambda e: got.append(e) or asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"), enabled=True)
    await m.start()
    await asyncio.sleep(0.05)
    await m.stop()
    assert got and got[0].room == "sala"
    assert m.status()["running"] is False


async def test_disable_source_stops_its_task():
    m = SourceManager(lambda e: asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"))
    await m.start()
    await m.set_enabled("a", False)
    src = [s for s in m.status()["sources"] if s["name"] == "a"][0]
    assert src["enabled"] is False and src["active"] is False
    await m.stop()


async def test_global_stop_zeroes_active_tasks():
    m = SourceManager(lambda e: asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"))
    m.register("b", lambda: FakeSource("quarto"))
    await m.start()
    assert all(s["active"] for s in m.status()["sources"])
    await m.set_running(False)
    assert not any(s["active"] for s in m.status()["sources"])


async def test_register_enabled_while_running_spawns_task_immediately():
    m = SourceManager(lambda e: asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"))
    await m.start()
    # register a second, enabled source AFTER start() — must not silently no-op
    m.register("b", lambda: FakeSource("quarto"), enabled=True)
    src = [s for s in m.status()["sources"] if s["name"] == "b"][0]
    assert src["enabled"] is True and src["active"] is True
    await m.stop()


async def test_self_terminated_source_reports_inactive():
    got = []
    async def on_event(ev):
        got.append(ev)

    class _Finite:
        async def events(self):
            yield "x"          # emit once, then the generator ends naturally

    mgr = SourceManager(on_event)
    mgr.register("finite", lambda: _Finite(), True)
    await mgr.start()
    await asyncio.sleep(0.02)   # let it emit and complete
    status = {s["name"]: s["active"] for s in mgr.status()["sources"]}
    assert status["finite"] is False   # completed task must not report active
    assert "x" in got
    await mgr.stop()


async def test_unregister_removes_source_and_kills_task():
    async def on_event(ev):
        pass
    mgr = SourceManager(on_event)
    mgr.register("cam_x", lambda: FakeSource("sala"), True)   # FakeSource = existing infinite fixture
    await mgr.start()
    assert any(s["name"] == "cam_x" and s["active"] for s in mgr.status()["sources"])
    await mgr.unregister("cam_x")
    names = [s["name"] for s in mgr.status()["sources"]]
    assert "cam_x" not in names                          # gone from the roster
    with pytest.raises(KeyError):
        await mgr.unregister("cam_x")                     # already gone
