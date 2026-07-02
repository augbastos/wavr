import asyncio
import pytest
from wavr.away import AwayMonitor
from wavr.hub import Hub

def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}

def test_first_occupied_sets_home_retained_no_event():
    msgs = []
    AwayMonitor(lambda t, p, r: msgs.append((t, p, r))).handle(_rs("sala", True))
    assert msgs == [("wavr/house/state", "home", True)]   # retained state, NO arrived event on first determination

def test_first_away_sets_away_retained_no_event():
    msgs = []
    AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1).handle(_rs("sala", False))
    assert msgs == [("wavr/house/state", "away", True)]   # retained away state, NO left event on first determination

def test_away_is_debounced_home_is_immediate():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=3)
    m.handle(_rs("sala", True))                            # home
    msgs.clear()
    m.handle(_rs("sala", False))                           # all-vacant streak 1 -> not yet away
    m.handle(_rs("sala", False))                           # streak 2
    assert msgs == []                                      # debounced, nothing published yet
    m.handle(_rs("sala", False))                           # streak 3 == grace -> away
    assert msgs == [("wavr/house/state", "away", True),
                    ("wavr/house/event", "left", False)]

def test_arrived_event_on_away_to_home():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1)
    m.handle(_rs("sala", True))                            # home (first, no event)
    m.handle(_rs("sala", False))                           # grace 1 -> away
    msgs.clear()
    m.handle(_rs("sala", True))                            # away -> home
    assert msgs == [("wavr/house/state", "home", True),
                    ("wavr/house/event", "arrived", False)]

def test_house_is_any_room_occupied():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1)
    m.handle(_rs("sala", True)); m.handle(_rs("quarto", True))
    msgs.clear()
    m.handle(_rs("sala", False))                           # sala vacant but quarto still occupied -> still home
    assert not any(t == "wavr/house/state" and p == "away" for t, p, r in msgs)

def test_no_duplicate_state_when_unchanged():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)))
    m.handle(_rs("sala", True)); m.handle(_rs("sala", True))
    assert [m2 for m2 in msgs if m2[0] == "wavr/house/state"] == [("wavr/house/state", "home", True)]

async def test_run_consumes_hub_and_unsubscribes():
    msgs = []
    hub = Hub()
    task = asyncio.create_task(AwayMonitor(lambda t, p, r: msgs.append(t)).run(hub))
    await asyncio.sleep(0)
    await hub.publish(_rs("sala", True))
    await asyncio.sleep(0.01)
    assert "wavr/house/state" in msgs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub._subscribers == set()
