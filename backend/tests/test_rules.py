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
