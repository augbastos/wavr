import asyncio

from wavr.app import create_app
from wavr.hub import Hub


def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}


async def test_away_monitor_publishes_house_state_when_enabled():
    msgs = []
    hub = Hub()
    app = create_app(sources=[], hub=hub, rules_publish=lambda t, p, r: msgs.append((t, p, r)))
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)             # let the just-created rules+away tasks subscribe first
        await hub.publish(_rs("sala", True))
        await asyncio.sleep(0.02)
    assert any(t == "wavr/house/state" for t, _, _ in msgs)      # away monitor ran


async def test_no_away_task_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_MQTT_ENABLED", raising=False)
    hub = Hub()
    app = create_app(sources=[], hub=hub)                         # no publisher, mqtt off
    async with app.router.lifespan_context(app):
        pass
    assert hub._subscribers == set()                             # no rules AND no away subscriber
