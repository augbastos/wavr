"""Frigate's person counts as evidence, and the three ways a count could lie.

A late subscriber hears no count (topics are change-only and not retained), a
camera with detection switched off still reports its last count, and a Frigate
that stopped leaves its last counts standing. Each is covered below; none needs
a broker, a camera or Frigate.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from wavr.frigate import (
    DEFAULT_CONFIDENCE, FrigateConfig, FrigateConfigError, FrigateSource,
    FrigateTracker, descriptor,
)
from wavr.fusion import FusionEngine, TRUSTED_ABSENCE_MODALITIES
from wavr.mqtt_subscriber import CONNECTED, DISCONNECTED, check_topic

T0 = datetime(2026, 9, 23, 20, 0, 0, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def cfg():
    # `front_door` is a camera, `yard` a zone -- Frigate puts both at the same
    # topic level, and only the operator knows which is which.
    return FrigateConfig(names={"front_door": "hall", "yard": "garden"})


def online(t):
    t.on_message("frigate/available", b"online", at(0))
    return t


# -- What is subscribed ----------------------------------------------------------

def test_no_image_or_speech_topic_can_ever_be_received():
    topics = cfg().topics()
    assert topics == ["frigate/available",
                      "frigate/front_door/person", "frigate/front_door/detect/state",
                      "frigate/yard/person", "frigate/yard/detect/state"]
    joined = " ".join(topics)
    for never in ("snapshot", "#", "+", "events", "transcription",
                  "tracked_object_update", "reviews"):
        assert never not in joined
    for t in topics:
        check_topic(t)


def test_a_name_that_is_really_a_frigate_topic_or_widens_the_subscription_is_refused():
    for bad in ("events", "available", "+", "cam/person", "#"):
        with pytest.raises(FrigateConfigError):
            FrigateConfig(names={bad: "hall"})


# -- The evidence ----------------------------------------------------------------

def test_a_person_appearing_is_camera_presence_with_a_count():
    t = online(FrigateTracker(cfg()))
    [ev] = t.on_message("frigate/front_door/person", b"2", at(1))
    assert (ev.room, ev.modality, ev.presence, ev.count) == ("hall", "camera", True, 2)
    assert ev.confidence == DEFAULT_CONFIDENCE
    assert ev.sensor_id == "frigate:front_door"


def test_zero_is_absence_only_when_detection_is_known_to_be_running():
    """`camera` absence lowers a room's confidence. A camera whose detection is
    OFF still reports its last count, so its zero must not do that."""
    assert "camera" in TRUSTED_ABSENCE_MODALITIES
    t = online(FrigateTracker(cfg()))
    assert t.on_message("frigate/front_door/person", b"0", at(1)) == [], \
        "detection state unknown: unknown is not empty"
    t.on_message("frigate/front_door/detect/state", b"ON", at(2))
    [ev] = t.on_message("frigate/front_door/person", b"0", at(3))
    assert (ev.presence, ev.count, ev.confidence) == (False, 0, 0.0)
    t.on_message("frigate/front_door/detect/state", b"OFF", at(4))
    assert t.tick(at(5)) == [], "detection switched off: the zero is no longer a look"


def test_a_zone_only_ever_contributes_presence():
    t = online(FrigateTracker(cfg()))
    [a] = t.on_message("frigate/yard/person", b"1", at(1))
    assert (a.room, a.presence) == ("garden", True)
    assert t.on_message("frigate/yard/person", b"0", at(2)) == []


def test_a_zone_transition_moves_the_person():
    t = online(FrigateTracker(FrigateConfig(names={"yard": "garden", "porch": "hall"})))
    t.on_message("frigate/yard/person", b"1", at(1))
    evs = t.on_message("frigate/porch/person", b"1", at(2))
    evs += t.on_message("frigate/yard/person", b"0", at(2))
    assert [(e.room, e.presence) for e in evs] == [("hall", True)]
    assert [(e.room, e.presence) for e in t.tick(at(3))] == [("hall", True)]


def test_a_held_count_is_reasserted_now_while_frigate_is_online():
    t = online(FrigateTracker(cfg()))
    t.on_message("frigate/front_door/person", b"2", at(1))
    [ev] = t.tick(at(600))
    assert (ev.count, ev.ts) == (2, at(600).isoformat()), \
        "Frigate is online and has not changed its answer: the answer is current"


def test_a_frigate_that_stops_or_drops_leaves_nothing_standing():
    for value in (b"offline", b"stopped"):
        t = online(FrigateTracker(cfg()))
        t.on_message("frigate/front_door/person", b"2", at(1))
        assert t.on_message("frigate/available", value, at(2)) == []
        assert t.tick(at(3)) == [], value


def test_nothing_is_reasserted_before_frigate_says_it_is_online():
    t = FrigateTracker(cfg())
    [ev] = t.on_message("frigate/front_door/person", b"1", at(1))
    assert ev.presence
    assert t.tick(at(2)) == []


def test_malformed_and_unmapped_messages_produce_nothing():
    t = online(FrigateTracker(cfg()))
    for bad in (b"two", b"-1", b"1e9", b"", b"99999"):
        assert t.on_message("frigate/front_door/person", bad, at(1)) == []
    assert t.on_message("frigate/garage/person", b"1", at(1)) == []
    assert t.on_message("frigate/front_door/detect/state", b"MAYBE", at(1)) == []
    assert t.dropped == {"unknown_name": 1, "malformed": 6}


def test_a_lost_broker_forgets_everything():
    t = online(FrigateTracker(cfg()))
    t.on_message("frigate/front_door/person", b"1", at(1))
    assert t.on_message(DISCONNECTED, b"", at(2)) == []
    assert t.tick(at(3)) == []
    assert t.on_message(CONNECTED, b"", at(4)) == []


def test_the_declaration_is_lan_reach_and_a_count_ceiling():
    d = descriptor()
    assert (d.reach, d.precision_ceiling) == ("lan", "count")


def test_fused_a_trusted_zero_lowers_the_room():
    t = online(FrigateTracker(cfg()))
    clock = [at(0)]
    fe = FusionEngine(now_fn=lambda: clock[0])
    t.on_message("frigate/front_door/detect/state", b"ON", at(0))
    for ev in t.on_message("frigate/front_door/person", b"1", at(0)):
        fe.update(ev)
    assert fe.state("hall").occupied
    assert fe.state("hall").person_count == 1


async def test_the_source_over_a_transport():
    async def transport(topics):
        yield (CONNECTED, b"", False)
        yield ("frigate/available", b"online", True)
        yield ("frigate/front_door/person", b"1", False)
        await asyncio.sleep(3600)

    src = FrigateSource(cfg(), transport=transport)
    gen = src.events()
    ev = await asyncio.wait_for(gen.__anext__(), 2)
    await gen.aclose()
    assert (ev.room, ev.count) == ("hall", 1)
