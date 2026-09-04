"""Events an application can act on — and the ones Wavr refuses to emit.

The naming rule is the point. Wavr detects presence, not people; it does not
know who walked in or that anyone walked at all. An application built against
`person.entered_room` would make promises to its own users that Wavr cannot
keep, and the failure would surface as Wavr's bug.
"""
from wavr.spatial_events import (
    EV_AGREEMENT, EV_COUNT, EV_DISAGREEMENT, EV_OCCUPANCY, EV_PRECISION,
    EV_SENSOR_OFFLINE, EV_SENSOR_ONLINE, EVENT_TYPES, SpatialEvents,
)


def state(room="kitchen", occupied=False, count=None, precision="room",
          sources=(), ts="2026-09-04T12:00:00+00:00", **kw):
    base = {"room": room, "occupied": occupied, "confidence": 0.8,
            "person_count": count, "precision_level": precision,
            "precision_next": "add_counting_sensor", "sources": list(sources),
            "ts": ts}
    base.update(kw)
    return base


def src(sensor_id, modality="camera", presence=True, health="fresh"):
    return {"sensor_id": sensor_id, "modality": modality,
            "presence": presence, "health": health}


def _kinds(events):
    return [e["event"] for e in events]


# -- The naming rule -----------------------------------------------------------

def test_no_event_claims_to_know_about_a_person():
    """Wavr detects presence. It does not know it was a person, and it does not
    know they entered rather than became detectable."""
    for kind in EVENT_TYPES:
        assert not kind.startswith("person."), kind
        assert "entered" not in kind and "left" not in kind


def test_there_is_no_transition_event():
    """Wavr can say one room's occupancy changed and another's changed too.
    Calling that one person moving is an inference topology can ASSESS and
    nothing here may ASSERT."""
    assert not any("transition" in k for k in EVENT_TYPES)


# -- The first sighting is not a change ----------------------------------------

def test_the_first_state_for_a_room_emits_nothing():
    """Otherwise every application sees a burst of "everything just happened" at
    startup and reacts to a house that was already like that."""
    ev = SpatialEvents()
    assert ev.observe(state(occupied=True, count=2)) == []


def test_the_second_state_diffs_against_the_first():
    ev = SpatialEvents()
    ev.observe(state(occupied=False))
    assert _kinds(ev.observe(state(occupied=True))) == [EV_OCCUPANCY]


def test_an_unchanged_state_emits_nothing():
    ev = SpatialEvents()
    ev.observe(state(occupied=True, count=1))
    assert ev.observe(state(occupied=True, count=1)) == []


def test_a_room_with_no_name_is_ignored():
    assert SpatialEvents().observe({"occupied": True}) == []


# -- Occupancy -----------------------------------------------------------------

def test_occupancy_carries_the_confidence_and_precision_behind_it():
    """An application deciding whether to act on "occupied" needs to know how
    sure and how detailed — otherwise it treats a Wi-Fi guess like a camera."""
    ev = SpatialEvents()
    ev.observe(state(occupied=False))
    out = ev.observe(state(occupied=True, precision="count"))[0]
    assert out["occupied"] is True
    assert out["confidence"] == 0.8
    assert out["precision"] == "count"
    assert out["room"] == "kitchen"


# -- Count, where None is a value ----------------------------------------------

def test_losing_the_ability_to_count_is_an_event():
    """An application showing "2 people" has to know to stop showing it. A
    silent drop to unknown leaves a stale number on somebody's wall."""
    ev = SpatialEvents()
    ev.observe(state(occupied=True, count=2))
    out = ev.observe(state(occupied=True, count=None))[0]
    assert out["event"] == EV_COUNT
    assert out["count"] is None and out["previous"] == 2
    assert out["count_known"] is False


def test_gaining_a_count_is_an_event_too():
    ev = SpatialEvents()
    ev.observe(state(occupied=True, count=None))
    out = ev.observe(state(occupied=True, count=3))[0]
    assert out["count"] == 3 and out["count_known"] is True


# -- Precision -----------------------------------------------------------------

def test_precision_changes_carry_what_would_improve_it():
    ev = SpatialEvents()
    ev.observe(state(precision="room"))
    out = ev.observe(state(precision="count"))[0]
    assert out["event"] == EV_PRECISION
    assert out["previous"] == "room" and out["precision"] == "count"
    assert out["how_to_improve"] == "add_counting_sensor"


# -- Per-sensor health, only possible since the merge stopped collapsing them ---

def test_a_sensor_going_quiet_is_named():
    ev = SpatialEvents()
    ev.observe(state(sources=[src("hall-cam"), src("hall-radar", "mmwave")]))
    out = ev.observe(state(sources=[src("hall-cam")]))
    assert _kinds(out) == [EV_SENSOR_OFFLINE]
    assert out[0]["sensor_id"] == "hall-radar"
    assert out[0]["modality"] == "mmwave"


def test_a_sensor_coming_back_is_named():
    ev = SpatialEvents()
    ev.observe(state(sources=[src("hall-cam")]))
    out = ev.observe(state(sources=[src("hall-cam"), src("hall-radar", "mmwave")]))
    assert _kinds(out) == [EV_SENSOR_ONLINE]
    assert out[0]["sensor_id"] == "hall-radar"


def test_a_stale_sensor_counts_as_offline():
    ev = SpatialEvents()
    ev.observe(state(sources=[src("hall-cam")]))
    out = ev.observe(state(sources=[src("hall-cam", health="stale")]))
    assert _kinds(out) == [EV_SENSOR_OFFLINE]


def test_anonymous_sources_produce_no_sensor_events():
    """An event that cannot name which sensor went offline is not actionable,
    and one per anonymous source would train people to ignore the stream."""
    ev = SpatialEvents()
    ev.observe(state(sources=[src(""), src("hall-cam")]))
    assert ev.observe(state(sources=[src("hall-cam")])) == []


# -- Disagreement --------------------------------------------------------------

def test_sensors_starting_to_disagree_is_an_event():
    ev = SpatialEvents()
    ev.observe(state(sources=[src("cam", presence=True),
                              src("radar", "mmwave", presence=True)]))
    out = ev.observe(state(sources=[src("cam", presence=False),
                                    src("radar", "mmwave", presence=True)]))
    assert EV_DISAGREEMENT in _kinds(out)
    said = {s["sensor_id"]: s["says"] for s in
            [e for e in out if e["event"] == EV_DISAGREEMENT][0]["sensors"]}
    assert said == {"cam": "empty", "radar": "occupied"}


def test_resolving_a_disagreement_is_an_event_too():
    ev = SpatialEvents()
    ev.observe(state(sources=[src("cam", presence=False),
                              src("radar", "mmwave", presence=True)]))
    out = ev.observe(state(sources=[src("cam", presence=True),
                                    src("radar", "mmwave", presence=True)]))
    assert EV_AGREEMENT in _kinds(out)


def test_a_sustained_disagreement_is_not_re_emitted():
    """It is a state change, not a heartbeat. Re-emitting would flood the stream
    for as long as two sensors argued."""
    ev = SpatialEvents()
    conflicting = [src("cam", presence=False), src("radar", "mmwave", presence=True)]
    ev.observe(state(sources=[src("cam"), src("radar", "mmwave")]))
    ev.observe(state(sources=conflicting))
    assert ev.observe(state(sources=list(conflicting))) == []


# -- Several changes in one frame ----------------------------------------------

def test_one_frame_can_carry_several_events():
    """A radar coming back can flip occupancy, restore a count and raise
    precision at once. Collapsing those would force every consumer to guess
    which they were being told about."""
    ev = SpatialEvents()
    ev.observe(state(occupied=False, count=None, precision="room",
                     sources=[src("cam")]))
    out = ev.observe(state(occupied=True, count=2, precision="count",
                           sources=[src("cam"), src("radar", "mmwave")]))
    kinds = set(_kinds(out))
    assert {EV_OCCUPANCY, EV_COUNT, EV_PRECISION, EV_SENSOR_ONLINE} <= kinds


# -- Housekeeping --------------------------------------------------------------

def test_rooms_are_tracked_independently():
    ev = SpatialEvents()
    ev.observe(state(room="kitchen", occupied=False))
    ev.observe(state(room="hall", occupied=False))
    assert _kinds(ev.observe(state(room="kitchen", occupied=True))) == [EV_OCCUPANCY]
    assert ev.observe(state(room="hall", occupied=False)) == []


def test_forgetting_a_room_resets_its_baseline():
    """A renamed room's old entry would otherwise sit there forever, and the new
    name would look brand new on its next reading."""
    ev = SpatialEvents()
    ev.observe(state(occupied=False))
    ev.forget("kitchen")
    assert ev.observe(state(occupied=True)) == [], "first sighting again"


def test_the_callback_sees_every_event():
    seen = []
    ev = SpatialEvents(on_event=seen.append)
    ev.observe(state(occupied=False))
    ev.observe(state(occupied=True, count=1))
    assert len(seen) == 2 and {e["event"] for e in seen} == {EV_OCCUPANCY, EV_COUNT}


def test_every_event_carries_a_room_and_a_time():
    ev = SpatialEvents()
    ev.observe(state(occupied=False))
    for e in ev.observe(state(occupied=True, count=1)):
        assert e["room"] == "kitchen"
        assert e["at"] == "2026-09-04T12:00:00+00:00"
