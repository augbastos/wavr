import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage


def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}


def _build(hub=None, notify=None):
    return create_app(
        sources=[], hub=hub or Hub(), storage=Storage(":memory:"), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), notify=notify,
    )


# ---- opt-in default-off -------------------------------------------------------

def test_ntfy_off_by_default_no_away_task_and_no_post(monkeypatch):
    monkeypatch.delenv("WAVR_NTFY_URL", raising=False)
    monkeypatch.delenv("WAVR_MQTT_ENABLED", raising=False)
    hub = Hub()
    app = _build(hub=hub)
    with TestClient(app) as client:
        assert hub._subscribers == set()   # neither mqtt nor ntfy configured -> no away task
        assert client.get("/api/status").json()["features"]["ntfy"] is False


def test_ntfy_url_configured_but_no_event_means_no_post(monkeypatch):
    # Setting WAVR_NTFY_URL alone (no injected transport, no edge event fired)
    # must not attempt any real network call -- the notifier is only built lazily
    # and only ever invoked from an actual arrived/left/rogue edge.
    monkeypatch.setenv("WAVR_NTFY_URL", "http://nas.local:8080/wavr")
    try:
        app = _build()
        with TestClient(app) as client:
            r = client.get("/api/status")
            assert r.status_code == 200
            assert r.json()["features"]["ntfy"] is True
    finally:
        monkeypatch.delenv("WAVR_NTFY_URL", raising=False)


# ---- derived-only edge wiring (house arrived/left via injected notify) -------

async def test_injected_notify_fires_on_house_left_and_arrived(monkeypatch):
    monkeypatch.delenv("WAVR_AWAY_GRACE", raising=False)   # default grace = 3
    notified = []
    hub = Hub()
    app = _build(hub=hub, notify=notified.append)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert len(hub._subscribers) == 1              # away monitor only (ntfy, no mqtt)
        await hub.publish(_rs("sala", True))            # first determination -> no notify
        await asyncio.sleep(0.02)
        assert notified == []
        for _ in range(3):                              # away_grace default 3
            await hub.publish(_rs("sala", False))
        await asyncio.sleep(0.02)
        assert notified == ["Wavr: casa vazia"]
        await hub.publish(_rs("sala", True))
        await asyncio.sleep(0.02)
    assert notified == ["Wavr: casa vazia", "Wavr: alguém chegou em casa"]


async def test_injected_notify_messages_are_derived_only():
    notified = []
    hub = Hub()
    app = _build(hub=hub, notify=notified.append)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        await hub.publish(_rs("sala", True))
        for _ in range(3):
            await hub.publish(_rs("sala", False))
        await asyncio.sleep(0.02)
    assert notified == ["Wavr: casa vazia"]
    for msg in notified:
        # no room name, no coordinate/vitals keys, no MAC-shaped tokens
        assert "sala" not in msg
        for leak in ("occupied", "confidence", "x=", "y=", "bpm"):
            assert leak not in msg.lower()
