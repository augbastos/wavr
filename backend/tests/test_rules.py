import asyncio
import json
import pytest
from wavr.rules import RulesEngine
from wavr.hub import Hub

def _rs(room, occupied, confidence=0.8, ts="2026-07-02T10:00:00+00:00"):
    return {"room": room, "occupied": occupied, "confidence": confidence,
            "vitals": {}, "sources": [], "explanation": "", "ts": ts}

def test_handle_publishes_retained_state_each_call():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", True, 0.77))
    state = [m for m in msgs if m[0] == "wavr/rooms/sala/state"]
    assert len(state) == 1
    topic, payload, retain = state[0]
    assert retain is True
    assert json.loads(payload) == {"occupied": True, "confidence": 0.77,
                                    "ts": "2026-07-02T10:00:00+00:00"}

def test_edge_event_only_on_transition():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", False))   # first sighting -> no edge event
    eng.handle(_rs("sala", False))   # no change -> no edge event
    eng.handle(_rs("sala", True))    # vacant -> occupied -> event
    eng.handle(_rs("sala", True))    # no change -> no event
    eng.handle(_rs("sala", False))   # occupied -> vacant -> event
    events = [m for m in msgs if m[0] == "wavr/rooms/sala/event"]
    assert [p for _, p, _ in events] == ["occupied", "vacant"]
    assert all(r is False for _, _, r in events)   # events are not retained

def test_edge_events_are_per_room():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", False)); eng.handle(_rs("quarto", False))
    eng.handle(_rs("sala", True))                       # only sala flips
    events = [m for m in msgs if m[0].endswith("/event")]
    assert events == [("wavr/rooms/sala/event", "occupied", False)]

def test_prefix_is_configurable():
    msgs = []
    RulesEngine(lambda t, p, r: msgs.append(t), prefix="casa").handle(_rs("sala", True))
    assert msgs[0] == "casa/rooms/sala/state"

async def test_run_consumes_hub_and_unsubscribes():
    msgs = []
    hub = Hub()
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    task = asyncio.create_task(eng.run(hub))
    await asyncio.sleep(0)                              # let it subscribe
    await hub.publish(_rs("sala", True))
    await asyncio.sleep(0.01)
    assert any(t == "wavr/rooms/sala/state" for t, _, _ in msgs)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub._subscribers == set()                   # unsubscribed on cancel

def test_room_with_mqtt_wildcard_is_slugged_to_legal_topic():
    # A room named with '#'/'+' would otherwise build an illegal MQTT wildcard
    # topic that paho rejects -> the room silently never reaches HA.
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("Kids#1", True))     # first sighting -> state only
    eng.handle(_rs("Kids#1", False))    # flip -> event
    state = {t for t, _, _ in msgs if t.endswith("/state")}
    event = [t for t, _, _ in msgs if t.endswith("/event")]
    assert state == {"wavr/rooms/kids_1/state"}   # retained state each call, one topic
    assert event == ["wavr/rooms/kids_1/event"]
    for t, _, _ in msgs:                 # no wildcard ever reaches a published topic
        assert "#" not in t and "+" not in t
        assert t.count("/") == 3         # room name did not inject an extra level

def test_room_with_slash_does_not_inject_topic_level():
    msgs = []
    RulesEngine(lambda t, p, r: msgs.append(t)).handle(_rs("Sala/Cozinha", True))
    assert msgs[0] == "wavr/rooms/sala_cozinha/state"
