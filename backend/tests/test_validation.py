"""The guided walk: a person states the truth, Wavr grades itself against it.

The tests that matter are the refusals — the cases where Wavr declines to score
a sensor. A validation system that grades everything it sees will build
reputations out of noise, and the operator will find a healthy camera demoted
for having been offline during a test they ran once.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.reliability import (
    CAP_ABSENCE, CAP_COUNT, CAP_PRESENCE, ReliabilityStore,
)
from wavr.validation import (
    STATE_ABANDONED, STATE_FINISHED, ValidationError, ValidationSession,
    ValidationStore, WINDOW_MAX_S,
)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, sec):
        self.t += timedelta(seconds=sec)


def src(sensor_id, modality, presence, health="fresh", count=None):
    return {"sensor_id": sensor_id, "modality": modality, "presence": presence,
            "health": health, "count": count, "confidence": 0.9, "age_s": 0}


@pytest.fixture
def scene(tmp_path):
    db = str(tmp_path / "wavr.db")
    store = ValidationStore(db)
    rel = ReliabilityStore(db)
    clock = _Clock()
    state = {"sources": []}
    session = ValidationSession(store, rel, lambda room: state, now_fn=clock)
    yield session, store, rel, clock, state
    store.close()
    rel.close()


# -- The walk ------------------------------------------------------------------

def test_a_full_walk_produces_a_verdict_and_records_evidence(scene):
    session, store, rel, clock, state = scene
    sid = session.start("kitchen")["session_id"]

    # "I am in the kitchen." The camera sees me; the PIR does not.
    session.declare(sid, occupied=True)
    state["sources"] = [src("kitchen-cam", "camera", True),
                        src("kitchen-pir", "pir", False)]
    for _ in range(6):
        clock.tick(1)
        session.sample(sid)

    # "I have left." Both agree the room is empty.
    session.declare(sid, occupied=False)
    state["sources"] = [src("kitchen-cam", "camera", False),
                        src("kitchen-pir", "pir", False)]
    for _ in range(6):
        clock.tick(1)
        session.sample(sid)

    summary = session.finish(sid)
    by_id = {s["sensor_id"]: s for s in summary["sensors"]}

    assert by_id["kitchen-cam"]["accuracy"] == 1.0
    assert by_id["kitchen-cam"]["verdict"] == "excellent"
    # The PIR was right only about the empty half.
    assert by_id["kitchen-pir"]["accuracy"] == 0.5
    assert "less weight" in summary["conclusion"]

    # And the evidence reached reliability, one record per CHECK.
    assert rel.profile("kitchen-cam", CAP_PRESENCE, room="kitchen").samples == 6
    assert rel.profile("kitchen-pir", CAP_PRESENCE, room="kitchen").correct == 0
    assert rel.profile("kitchen-pir", CAP_ABSENCE, room="kitchen").correct == 6


def test_the_session_is_stored_and_shows_in_history(scene):
    session, store, rel, clock, state = scene
    sid = session.start("hall")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("hall-cam", "camera", True)]
    clock.tick(1)
    session.sample(sid)
    session.finish(sid)

    hist = store.history("hall")
    assert len(hist) == 1
    assert hist[0]["state"] == STATE_FINISHED
    assert hist[0]["summary"]["room"] == "hall"


# -- Latency: the number a household actually feels ---------------------------

def test_first_agreement_is_the_measured_response_time(scene):
    """Not average agreement — the moment it first noticed. That is what a
    person experiences as "the light takes three seconds"."""
    session, store, rel, clock, state = scene
    sid = session.start("hall")["session_id"]
    session.declare(sid, occupied=True)

    state["sources"] = [src("slow-cam", "camera", False)]
    for _ in range(3):
        clock.tick(1)
        session.sample(sid)             # 1s, 2s, 3s: still wrong
    state["sources"] = [src("slow-cam", "camera", True)]
    clock.tick(1)
    session.sample(sid)                 # 4s: first agreement
    clock.tick(5)
    session.sample(sid)                 # later agreement changes nothing

    summary = session.finish(sid)
    assert summary["sensors"][0]["typical_response_s"] == 4.0


# -- The refusals, which are the point ----------------------------------------

def test_an_offline_sensor_is_excluded_not_marked_wrong(scene):
    """Grading a sensor for being unplugged builds a reputation out of its
    absence, and the operator later finds a healthy sensor demoted with no way
    to see why."""
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("den-cam", "camera", False, health="dead")]
    for _ in range(5):
        clock.tick(1)
        session.sample(sid)

    summary = session.finish(sid)
    assert summary["sensors"] == [], "it could not have known; it is not graded"
    assert rel.profile("den-cam", CAP_PRESENCE, room="den").samples == 0
    assert "Nothing was measured" in summary["conclusion"]


def test_a_stale_sensor_is_excluded_too(scene):
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("den-cam", "camera", False, health="stale")]
    clock.tick(1)
    result = session.sample(sid)
    assert result["graded_sensors"] == 0


def test_an_anonymous_source_cannot_build_a_reputation(scene):
    """A reading nobody can attribute cannot earn or lose anything."""
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("", "network", True)]
    clock.tick(1)
    assert session.sample(sid)["graded_sensors"] == 0


def test_a_non_counting_source_is_not_graded_on_count(scene):
    """`count is None` means "this source does not count" — the honest silence
    the product is built on, never a wrong answer."""
    session, store, rel, clock, state = scene
    sid = session.start("kitchen")["session_id"]
    session.declare(sid, occupied=True, count=2)
    state["sources"] = [src("hall-pir", "pir", True, count=None)]
    for _ in range(4):
        clock.tick(1)
        session.sample(sid)
    session.finish(sid)
    assert rel.profile("hall-pir", CAP_COUNT, room="kitchen").samples == 0


def test_a_counting_source_is_graded_on_its_number(scene):
    session, store, rel, clock, state = scene
    sid = session.start("kitchen")["session_id"]
    session.declare(sid, occupied=True, count=2)
    state["sources"] = [src("kitchen-cam", "camera", True, count=2)]
    for _ in range(4):
        clock.tick(1)
        session.sample(sid)
    state["sources"] = [src("kitchen-cam", "camera", True, count=5)]
    for _ in range(4):
        clock.tick(1)
        session.sample(sid)
    session.finish(sid)

    p = rel.profile("kitchen-cam", CAP_COUNT, room="kitchen")
    assert p.samples == 8 and p.correct == 4


def test_an_expired_window_stops_grading(scene):
    """The person wandered off. Whatever the sensors say now has nothing to do
    with the truth they last declared, and grading it manufactures failures."""
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("den-cam", "camera", False)]
    clock.tick(WINDOW_MAX_S + 1)
    result = session.sample(sid)
    assert result["sampled"] is False
    assert result["reason"] == "window_expired"


def test_abandoning_records_nothing(scene):
    """A walk somebody gave up on is not evidence, and must not be able to
    quietly demote a sensor."""
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("den-cam", "camera", False)]
    for _ in range(9):
        clock.tick(1)
        session.sample(sid)

    out = session.abandon(sid)
    assert out["recorded"] is False
    assert rel.profile("den-cam", CAP_PRESENCE, room="den").samples == 0
    assert store.get(sid)["state"] == STATE_ABANDONED


# -- Session hygiene -----------------------------------------------------------

def test_sampling_before_declaring_is_refused(scene):
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    with pytest.raises(ValidationError, match="declare"):
        session.sample(sid)


def test_starting_twice_resumes_rather_than_splitting_the_walk(scene):
    """Two live sessions for one room would each grade half of it."""
    session, store, rel, clock, state = scene
    first = session.start("den")
    second = session.start("den")
    assert second["session_id"] == first["session_id"]
    assert second["resumed"] is True


def test_a_finished_session_cannot_be_reopened(scene):
    session, store, rel, clock, state = scene
    sid = session.start("den")["session_id"]
    session.declare(sid, occupied=True)
    session.finish(sid)
    with pytest.raises(ValidationError, match="finished"):
        session.declare(sid, occupied=False)


def test_an_unknown_session_is_refused(scene):
    session, _, _, _, _ = scene
    with pytest.raises(ValidationError, match="unknown"):
        session.sample("nope")


def test_a_session_survives_a_core_restart(scene, tmp_path):
    """Somebody walking their house must not lose the work because the process
    bounced. The windows rehydrate from storage."""
    session, store, rel, clock, state = scene
    sid = session.start("hall")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = [src("hall-cam", "camera", True)]
    clock.tick(1)
    session.sample(sid)

    # A brand-new session object over the same store: the old one's in-memory
    # windows are gone.
    revived = ValidationSession(store, rel, lambda room: state, now_fn=clock)
    clock.tick(1)
    assert revived.sample(sid)["graded_sensors"] == 1
    summary = revived.finish(sid)
    assert summary["sensors"][0]["checks"] == 2, "both samples survived"


def test_a_room_nobody_sensed_says_so_plainly(scene):
    session, store, rel, clock, state = scene
    sid = session.start("garage")["session_id"]
    session.declare(sid, occupied=True)
    state["sources"] = []
    clock.tick(1)
    session.sample(sid)
    summary = session.finish(sid)
    assert "Nothing was measured in garage" in summary["conclusion"]
