import asyncio

from wavr.app import create_app
from wavr.hub import Hub


def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}


# Drive the app's lifespan directly (same context manager TestClient invokes on
# startup/shutdown) instead of through TestClient's separate portal thread, so the
# test and the rules task share one event loop -- no cross-loop asyncio.Queue issues.

async def test_injected_publisher_receives_roomstate_via_hub():
    msgs = []
    hub = Hub()
    app = create_app(sources=[], hub=hub, rules_publish=lambda t, p, r: msgs.append((t, p, r)))
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)                    # let the just-created tasks run to their first await
        assert len(hub._subscribers) == 2         # rules engine + away monitor subscribed on startup
        await hub.publish(_rs("sala", True))
        await asyncio.sleep(0.02)                 # let the rules task drain the queue
    assert any(t == "wavr/rooms/sala/state" for t, _, _ in msgs)


async def test_egress_master_blocks_mqtt_publish(monkeypatch):
    # Audit fix (P1): the "Egress: blocked" system-toggles master must gate MQTT
    # publish too -- the Egress screen's own row for it claims exactly that. Previously
    # `_rules_publish` had no egress-master check at all, so blocking egress from the
    # System tab left the rules-engine roomstate publish (and everything else sharing
    # this same `_rules_publish` reference) reachable.
    from wavr.connector_store import ConnectorStore
    monkeypatch.setenv("WAVR_DB", ":memory:")
    msgs = []
    store = ConnectorStore(":memory:")
    store.upsert("sys:egress", "system", "egress")
    store.set_enabled("sys:egress", False)   # operator blocked egress from the System tab
    hub = Hub()
    app = create_app(sources=[], hub=hub, rules_publish=lambda t, p, r: msgs.append((t, p, r)),
                     connector_store=store)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        await hub.publish(_rs("sala", True))
        await asyncio.sleep(0.02)
    assert msgs == []   # egress blocked -> zero MQTT publish attempted despite a wired publisher


async def test_no_rules_task_when_disabled_and_no_publisher(monkeypatch):
    monkeypatch.delenv("WAVR_MQTT_ENABLED", raising=False)   # disabled default
    hub = Hub()
    app = create_app(sources=[], hub=hub)                     # no rules_publish, mqtt off
    async with app.router.lifespan_context(app):
        assert hub._subscribers == set()                      # nothing subscribed -> no rules engine
