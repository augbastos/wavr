"""App-level wiring for the internet/gateway monitor (Feature B): opt-in via
config or direct injection, /api/status shape, and notify-on-transition
through the real create_app lifespan. Mirrors test_away_wiring.py /
test_notifier_wiring.py's style -- an injected monitor always wins over cfg,
same as notify/rules_publish/narrator."""
import asyncio
import time

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.internet_monitor import InternetMonitor


def _build(internet_monitor=None):
    return create_app(
        sources=[], camera_store=CameraStore(":memory:"),
        internet_monitor=internet_monitor,
    )


# ---- opt-in default-off: inert, no task, null status -------------------------

def test_internet_monitor_off_by_default_inert(monkeypatch):
    monkeypatch.delenv("WAVR_INTERNET_MONITOR", raising=False)
    app = _build()
    with TestClient(app) as client:
        r = client.get("/api/status")
        body = r.json()
        assert body["features"]["internet_monitor"] is False
        assert body["internet"] == {"ok": None, "since": None}


def test_features_internet_monitor_reflects_config_flag(monkeypatch):
    monkeypatch.setenv("WAVR_INTERNET_MONITOR", "1")
    try:
        # Inject a harmless fake monitor so the cfg-driven real-ping pathway in
        # create_app is bypassed (an injected monitor always wins, same rule as
        # notify/rules_publish/narrator) -- this test only checks that the
        # `features.internet_monitor` flag mirrors cfg, like every other flag.
        async def check():
            return True
        m = InternetMonitor(check=check, fail_threshold=1)
        app = _build(internet_monitor=m)
        with TestClient(app) as client:
            assert client.get("/api/status").json()["features"]["internet_monitor"] is True
    finally:
        monkeypatch.delenv("WAVR_INTERNET_MONITOR", raising=False)


# ---- injected monitor: status surfaces, starts/stops with the app lifespan ---

def test_injected_monitor_status_surfaces_on_api_status():
    async def check():
        return True
    m = InternetMonitor(check=check, interval=0.01, fail_threshold=1)
    app = _build(internet_monitor=m)
    with TestClient(app) as client:
        r = client.get("/api/status")
        for _ in range(50):
            if r.json()["internet"]["ok"] is not None:
                break
            time.sleep(0.01)
            r = client.get("/api/status")
        assert r.json()["internet"]["ok"] is True
        assert r.json()["internet"]["since"] is not None


async def test_injected_monitor_starts_with_lifespan_and_runs_checks():
    calls = []

    async def check():
        calls.append(1)
        return True
    m = InternetMonitor(check=check, interval=0.01, fail_threshold=1)
    app = _build(internet_monitor=m)
    async with app.router.lifespan_context(app):
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls                                   # background task ran >=1 check
    # lifespan exit above must have stopped the task cleanly (cancel-safe stop()
    # never raises) -- reaching here without an exception proves it.


async def test_injected_monitor_transition_fires_notify_via_app_wiring():
    notified = []
    results = iter([True, False, False, False])

    async def check():
        try:
            return next(results)
        except StopIteration:
            return False    # exhausted -> keep reporting down (idempotent tail)
    m = InternetMonitor(check=check, interval=0.005, fail_threshold=3, notify=notified.append)
    app = _build(internet_monitor=m)
    async with app.router.lifespan_context(app):
        for _ in range(300):
            if notified:
                break
            await asyncio.sleep(0.01)
    assert notified == ["Wavr: internet caiu"]


async def test_injected_monitor_single_drop_does_not_notify_via_app_wiring():
    notified = []
    results = iter([True, False, True, True, True])   # a single drop, then recovery

    async def check():
        try:
            return next(results)
        except StopIteration:
            return True
    m = InternetMonitor(check=check, interval=0.005, fail_threshold=3, notify=notified.append)
    app = _build(internet_monitor=m)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.15)          # let several checks run past the sequence
    assert notified == []                  # a single drop must never cross the debounce
