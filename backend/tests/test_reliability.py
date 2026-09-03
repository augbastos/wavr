"""What a sensor has earned, and the refusal to guess before it has earned it.

The load-bearing test in this file is the one asserting that an UNMEASURED
sensor is treated at full trust. Every other behaviour here is a refinement; that
one is the difference between "Wavr learns which of your sensors to trust" and
"Wavr quietly penalises every sensor you just installed".
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.reliability import (
    CAP_ABSENCE, CAP_COUNT, CAP_PRESENCE, CAP_STILL, FACTOR_CEIL, FACTOR_FLOOR,
    MIN_SAMPLES, ReliabilityStore,
)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    s = ReliabilityStore(":memory:")
    yield s
    s.close()


def _record(store, sensor, cap, correct, wrong=0, room=""):
    for _ in range(correct):
        store.record(sensor, cap, True, room=room)
    for _ in range(wrong):
        store.record(sensor, cap, False, room=room)


# -- The rule that matters most ----------------------------------------------

def test_an_unmeasured_sensor_is_at_full_trust(store):
    """A new sensor is not a suspect sensor.

    If an unmeasured sensor were penalised, every fresh install would show
    confidence sagging for a reason the operator cannot discover, and adding a
    camera would make the room read WORSE until it had proven itself.
    """
    p = store.profile("brand-new-cam", CAP_PRESENCE)
    assert p.factor == FACTOR_CEIL == 1.0
    assert p.measured is False
    assert p.samples == 0
    assert "Not measured yet" in p.reason


def test_a_partly_measured_sensor_is_still_at_full_trust(store):
    """Below the sample floor, Wavr says how far it has got rather than
    forming an opinion on three data points."""
    _record(store, "hall-cam", CAP_PRESENCE, correct=1, wrong=2)
    p = store.profile("hall-cam", CAP_PRESENCE)
    assert p.factor == 1.0, "3 checks is not a verdict"
    assert p.measured is False
    assert f"3 of {MIN_SAMPLES}" in p.reason


def test_unmeasured_accuracy_is_none_not_zero(store):
    """0% would render as "this sensor is always wrong" for a sensor nobody
    ever checked. The distinction this whole codebase defends."""
    assert store.profile("nobody", CAP_PRESENCE).accuracy is None


# -- Earning, and losing, weight ---------------------------------------------

def test_a_sensor_that_is_always_right_keeps_full_weight(store):
    _record(store, "kitchen-cam", CAP_PRESENCE, correct=12)
    p = store.profile("kitchen-cam", CAP_PRESENCE)
    assert p.measured is True
    assert p.factor == 1.0
    assert p.accuracy == 1.0
    assert "12 of 12" in p.reason


def test_a_sensor_that_is_often_wrong_loses_weight(store):
    _record(store, "mirror-cam", CAP_PRESENCE, correct=3, wrong=9)
    p = store.profile("mirror-cam", CAP_PRESENCE)
    assert p.measured is True
    assert p.factor < 1.0
    assert "25%" in p.reason and "less weight" in p.reason


def test_weight_never_reaches_zero(store):
    """A sensor that is wrong every time still carries information, and a factor
    of 0 would delete it from the merge silently — which is a configuration
    decision (disable it), not something reliability may do by itself."""
    _record(store, "broken", CAP_PRESENCE, correct=0, wrong=20)
    assert store.profile("broken", CAP_PRESENCE).factor == FACTOR_FLOOR
    assert FACTOR_FLOOR > 0


def test_good_behaviour_cannot_exceed_the_modality_ceiling(store):
    """Reliability may only ever REDUCE trust. Otherwise a lucky PIR could
    out-vote a camera, which no amount of good behaviour justifies — the
    modality weight is a claim about physics."""
    _record(store, "lucky-pir", CAP_PRESENCE, correct=100)
    assert store.profile("lucky-pir", CAP_PRESENCE).factor <= FACTOR_CEIL


# -- A sensor is not uniformly good ------------------------------------------

def test_capabilities_are_scored_independently(store):
    """A PIR can be excellent at noticing arrival and useless at holding a
    still person. One number would lose exactly the useful half."""
    _record(store, "hall-pir", CAP_PRESENCE, correct=12)
    _record(store, "hall-pir", CAP_STILL, correct=1, wrong=11)

    assert store.profile("hall-pir", CAP_PRESENCE).factor == 1.0
    assert store.profile("hall-pir", CAP_STILL).factor < 0.6


def test_the_same_sensor_is_scored_per_room(store):
    """A camera moved from the hall to the kitchen has earned nothing in the
    kitchen. Rooms are separate rows so its old record cannot vouch for a place
    it has never seen."""
    _record(store, "roaming-cam", CAP_PRESENCE, correct=12, room="hall")
    assert store.profile("roaming-cam", CAP_PRESENCE, room="hall").measured is True
    assert store.profile("roaming-cam", CAP_PRESENCE, room="kitchen").measured is False


def test_forgetting_a_sensor_clears_every_capability_and_room(store):
    _record(store, "moved", CAP_PRESENCE, correct=12, room="hall")
    _record(store, "moved", CAP_COUNT, correct=12, room="hall")
    assert store.forget("moved") == 2
    assert store.profile("moved", CAP_PRESENCE, room="hall").measured is False


# -- Input hygiene ------------------------------------------------------------

def test_an_unknown_capability_is_ignored_not_stored(store):
    """Called from the validation loop while somebody is walking around a house.
    A caller's typo must not abort their session, and must not create a row that
    would surface later as a profile nobody can explain."""
    store.record("cam", "vibes", True)
    assert store.list_profiles("cam") == []


def test_an_empty_sensor_id_is_ignored(store):
    """A source with no instance identity cannot accumulate a reputation — its
    readings are not attributable to anything."""
    store.record("", CAP_PRESENCE, True)
    assert store.list_profiles() == []


# -- Fusion actually uses it, and shows that it did ---------------------------

def _cam(room, sensor_id, present, conf=0.9):
    return SensingEvent(room=room, modality="camera", presence=present, motion=0.0,
                        breathing_bpm=None, heart_bpm=None,
                        confidence=conf if present else 0.0, ts=T0.isoformat(),
                        sensor_id=sensor_id)


def test_reliability_lowers_the_confidence_of_an_unreliable_sensor():
    trusted = FusionEngine(now_fn=lambda: T0)
    a = trusted.update(_cam("hall", "mirror-cam", True))

    doubted = FusionEngine(now_fn=lambda: T0,
                           reliability_fn=lambda sid, room: (0.5, "wrong half the time"))
    b = doubted.update(_cam("hall", "mirror-cam", True))

    assert b.confidence < a.confidence


def test_the_adjustment_is_published_with_its_reason():
    """The pairing that makes this defensible. A weight that moves the answer
    invisibly is the "86% that means nothing" the product exists to avoid."""
    f = FusionEngine(now_fn=lambda: T0,
                     reliability_fn=lambda sid, room: (0.5, "wrong half the time"))
    rs = f.update(_cam("hall", "mirror-cam", True))

    src = rs.sources[0]
    assert src["reliability"] == 0.5
    assert src["reliability_reason"] == "wrong half the time"


def test_a_full_trust_sensor_carries_no_reliability_noise():
    """A `reliability: 1.0` on every row trains a reader to stop looking. Only
    rows where the answer actually moved carry one."""
    f = FusionEngine(now_fn=lambda: T0,
                     reliability_fn=lambda sid, room: (1.0, "always right"))
    rs = f.update(_cam("hall", "good-cam", True))
    assert "reliability" not in rs.sources[0]


def test_no_reliability_wired_is_byte_identical_to_before():
    plain = FusionEngine(now_fn=lambda: T0).update(_cam("hall", "cam", True))
    wired = FusionEngine(now_fn=lambda: T0,
                         reliability_fn=lambda sid, room: (1.0, "")
                         ).update(_cam("hall", "cam", True))
    assert plain.confidence == wired.confidence
    assert plain.sources == wired.sources


def test_a_sensor_with_no_identity_is_never_penalised():
    """Anonymous sources are unattributable, so they cannot have earned or lost
    anything. The lookup must not even be attempted for them."""
    calls = []

    def spy(sid, room):
        calls.append(sid)
        return (0.1, "should never be applied")

    f = FusionEngine(now_fn=lambda: T0, reliability_fn=spy)
    rs = f.update(_cam("hall", "", True))
    assert calls == [], "no lookup for an anonymous source"
    assert "reliability" not in rs.sources[0]


def test_a_failing_reliability_lookup_does_not_punish_the_sensor():
    """A storage fault is Wavr's problem, not the sensor's. Falling back to the
    modality constant is the honest degradation; dropping the source would make
    a database hiccup look like an empty room."""
    def angry(sid, room):
        raise RuntimeError("db locked")

    f = FusionEngine(now_fn=lambda: T0, reliability_fn=angry)
    rs = f.update(_cam("hall", "cam", True))
    plain = FusionEngine(now_fn=lambda: T0).update(_cam("hall", "cam", True))
    assert rs.confidence == plain.confidence
    assert rs.occupied is plain.occupied


def test_the_store_plugs_into_fusion_directly(store):
    """End to end, with the real store rather than a lambda."""
    _record(store, "mirror-cam", CAP_PRESENCE, correct=2, wrong=10, room="hall")

    def rel(sensor_id, room):
        p = store.profile(sensor_id, CAP_PRESENCE, room=room)
        return p.factor, p.reason

    f = FusionEngine(now_fn=lambda: T0, reliability_fn=rel)
    rs = f.update(_cam("hall", "mirror-cam", True))
    assert rs.sources[0]["reliability"] < 1.0
    assert "2 of 12" in rs.sources[0]["reliability_reason"]
