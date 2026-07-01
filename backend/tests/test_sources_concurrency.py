import asyncio
import pytest
from wavr.sourcemanager import SourceManager


class _FakeSource:
    """Emits `label` events on a fixed cadence; `delay` lets one source be slow."""
    def __init__(self, label, delay=0.0):
        self._label = label
        self._delay = delay
    async def events(self):
        while True:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._label


async def test_slow_source_does_not_starve_fast_ones():
    got = []
    async def on_event(ev):
        got.append(ev)
    mgr = SourceManager(on_event)
    mgr.register("fast", lambda: _FakeSource("fast", delay=0.001), True)
    mgr.register("slow", lambda: _FakeSource("slow", delay=0.05), True)
    mgr.register("sim", lambda: _FakeSource("sim", delay=0.001), True)
    await mgr.start()
    await asyncio.sleep(0.1)
    await mgr.stop()
    # All three ran concurrently; the slow one didn't block the fast ones.
    assert "fast" in got and "sim" in got and "slow" in got
    # Fast sources produced many more events than the slow one in the same window.
    assert got.count("fast") > got.count("slow") * 3
    # Global stop cancelled every task.
    assert all(not s["active"] for s in mgr.status()["sources"])


def test_default_sources_lists_network_ruview_sim(monkeypatch):
    monkeypatch.delenv("WAVR_NET_MACS", raising=False)
    from wavr.config import load_config
    from wavr.app import _default_sources
    srcs = _default_sources(load_config())
    enabled = {name: en for name, factory, en in srcs}
    assert enabled == {"network": True, "ruview": True, "sim": False}
