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


# ---- Build C4: intrusion / routine-anomaly / house-status forwarding ----

def test_handle_intrusion_publishes_on_change_only():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle_intrusion("sala", True)
    eng.handle_intrusion("sala", True)     # unchanged -> no re-publish
    eng.handle_intrusion("sala", False)
    topics = [(t, p, r) for t, p, r in msgs if t == "wavr/watch/rooms/sala/intrusion"]
    assert [p for _, p, _ in topics] == ["ON", "OFF"]
    assert all(r is True for _, _, r in topics)   # retained


def test_handle_intrusion_house_level_room_agnostic_topic():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle_intrusion(None, True)
    assert msgs == [("wavr/watch/house/intrusion", "ON", True)]
    for t, p, r in msgs:
        assert "sala" not in t and "sala" not in p   # never names a room


def test_handle_intrusion_scopes_are_independent():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle_intrusion("sala", True)
    eng.handle_intrusion("quarto", True)
    topics = {t for t, _, _ in msgs}
    assert topics == {"wavr/watch/rooms/sala/intrusion", "wavr/watch/rooms/quarto/intrusion"}


def test_handle_routine_anomaly_publishes_on_change_only():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle_routine_anomaly("sala", False)   # initial baseline still publishes
    eng.handle_routine_anomaly("sala", False)   # unchanged -> no re-publish
    eng.handle_routine_anomaly("sala", True)
    topics = [(t, p, r) for t, p, r in msgs if t == "wavr/rooms/sala/routine_anomaly"]
    assert [p for _, p, _ in topics] == ["OFF", "ON"]
    assert all(r is True for _, _, r in topics)


def test_handle_house_status_dedupes_on_unchanged_status_score_reasons():
    import json
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    ok = {"status": "ok", "score": 0, "reasons": [], "ts": "t1"}
    eng.handle_house_status(ok)
    eng.handle_house_status({**ok, "ts": "t2"})   # only `ts` differs -> still a no-op
    assert len([m for m in msgs if m[0] == "wavr/house/status"]) == 1

    alert = {"status": "alert", "score": 4,
             "reasons": [{"layer": "physical", "kind": "intrusion",
                         "what": "unrecognized person in sala", "severity": "alert",
                         "ts": "t3"}], "ts": "t3"}
    eng.handle_house_status(alert)
    status_msgs = [m for m in msgs if m[0] == "wavr/house/status"]
    assert len(status_msgs) == 2
    topic, payload, retain = status_msgs[-1]
    assert retain is True
    body = json.loads(payload)
    assert body == alert   # forwarded byte-for-byte, no re-ranking/new field


def test_derived_mqtt_topics_and_payloads_carry_no_geometry_or_identity():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle_intrusion("sala", True)
    eng.handle_intrusion(None, True)
    eng.handle_routine_anomaly("sala", True)
    eng.handle_house_status({
        "status": "alert", "score": 4,
        "reasons": [{"layer": "physical", "kind": "intrusion",
                     "what": "unrecognized person in sala", "severity": "alert",
                     "ts": "t"}],
        "ts": "t",
    })
    blob = " ".join(f"{t} {p}" for t, p, _ in msgs).lower()
    for word in ("target", "pose", "vital", "position"):
        assert word not in blob
