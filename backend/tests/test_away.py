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

def test_notify_not_called_on_first_determination():
    notified = []
    m = AwayMonitor(notify=notified.append)   # no publish injected -- must default to a no-op
    m.handle(_rs("sala", True))
    assert notified == []                     # first determination -> no arrived/left edge

def test_notify_called_on_left_and_arrived_edges():
    notified = []
    m = AwayMonitor(notify=notified.append, away_grace=1)
    m.handle(_rs("sala", True))                            # home (first, no notify)
    m.handle(_rs("sala", False))                            # grace 1 -> away
    assert notified == ["Wavr: casa vazia"]
    m.handle(_rs("sala", True))                             # away -> home
    assert notified == ["Wavr: casa vazia", "Wavr: alguém chegou em casa"]

def test_notify_message_is_derived_only_no_room_or_coords():
    notified = []
    m = AwayMonitor(notify=notified.append, away_grace=1)
    m.handle(_rs("sala", True))
    m.handle(_rs("sala", False))
    assert notified == ["Wavr: casa vazia"]
    for msg in notified:
        assert "sala" not in msg and "occupied" not in msg and "confidence" not in msg

def test_notify_is_optional_publish_only_still_works():
    msgs = []
    # no `notify` at all -- publish-only behaviour must be unchanged
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1)
    m.handle(_rs("sala", True))
    m.handle(_rs("sala", False))
    assert ("wavr/house/event", "left", False) in msgs

def test_publish_defaults_to_noop_when_omitted():
    # An ntfy-only caller (no MQTT publisher) must still drive edge detection.
    m = AwayMonitor(notify=lambda msg: None, away_grace=1)
    m.handle(_rs("sala", True))
    m.handle(_rs("sala", False))   # must not raise despite no publish callable

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
