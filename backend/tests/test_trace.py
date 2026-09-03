"""A trace replays identically, and carries no person in it.

Two properties, both load-bearing:

  * **determinism** — the same file twice gives the same room states, or it is a
    recording rather than a reproduction and no bug can be studied with it;
  * **sanitisation** — a trace is a minute-by-minute record of where people were,
    so being able to share one at all depends on it being empty of them.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.events import Identity, SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.trace import (
    SANITIZED_ALWAYS, TRACE_VERSION, TraceError, TraceRecorder, load, replay,
    sanitize, save, summarize,
)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def ev(sec, room="sala", modality="camera", present=True, sensor_id="hall-cam",
       count=None, targets=(), identities=(), breathing=None):
    return SensingEvent(
        room=room, modality=modality, presence=present, motion=0.0,
        breathing_bpm=breathing, heart_bpm=None,
        confidence=0.9 if present else 0.0,
        ts=(T0 + timedelta(seconds=sec)).isoformat(),
        count=count, sensor_id=sensor_id, targets=targets, identities=identities)


def _recorded(*events, label="test"):
    r = TraceRecorder(label=label)
    for e in events:
        r.record(e)
    return r.to_dict()


def _engine(now_fn):
    return FusionEngine(now_fn=now_fn)


# -- Determinism, which is the whole point ------------------------------------

def test_the_same_trace_replays_to_the_same_states():
    trace = _recorded(ev(0), ev(5, present=False), ev(10))
    a = replay(trace, _engine)
    b = replay(trace, _engine)
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]


def test_replay_ages_against_the_trace_not_the_wall_clock():
    """The trick that makes replay possible at all.

    A trace recorded months ago must behave as it did then. Using the wall clock
    would decay every source in it to nothing and produce an empty house.
    """
    trace = _recorded(ev(0), ev(1))
    states = replay(trace, _engine)
    assert states[-1].occupied is True
    assert states[-1].sources[0]["health"] == "fresh"


def test_a_replay_reproduces_staleness_faithfully():
    """A gap in the recording is part of what happened. Replay must show the
    source going stale exactly where it did, not smooth it over."""
    trace = _recorded(ev(0), ev(200, sensor_id="other-cam", room="sala"))
    states = replay(trace, _engine)
    by_id = {s["sensor_id"]: s for s in states[-1].sources}
    assert by_id["hall-cam"]["health"] in ("stale", "dead")
    assert by_id["other-cam"]["health"] == "fresh"


def test_every_state_is_returned_not_just_the_last():
    trace = _recorded(ev(0), ev(1), ev(2))
    assert len(replay(trace, _engine)) == 3


def test_the_on_state_hook_sees_each_step():
    seen = []
    trace = _recorded(ev(0), ev(1))
    replay(trace, _engine, on_state=seen.append)
    assert len(seen) == 2


def test_a_malformed_timestamp_is_reproduced_not_skipped():
    """It is part of what the trace recorded. Skipping it would replay an
    idealised version of the situation instead of the one that broke."""
    r = TraceRecorder()
    r.record(ev(0))
    r.record(SensingEvent(room="sala", modality="network", presence=True,
                          motion=0.0, breathing_bpm=None, heart_bpm=None,
                          confidence=0.5, ts="not-a-timestamp"))
    states = replay(r.to_dict(), _engine)
    assert len(states) == 2, "the bad event still went through the engine"


# -- Sanitisation --------------------------------------------------------------

def test_identity_labels_and_vitals_are_always_removed():
    """There is no debugging reason to know it was Ana, or what her breathing
    rate was."""
    trace = _recorded(ev(0, identities=(Identity(person="Ana", source="ble", rssi=-60),),
                         breathing=14.2))
    clean = sanitize(trace)
    blob = str(clean)
    assert "Ana" not in blob
    assert clean["events"][0]["event"]["breathing_bpm"] is None
    assert clean["events"][0]["event"]["identities"] == []


def test_positions_are_kept_by_default():
    """A positional bug cannot be reproduced without positions. Dropping them
    always would make the sanitised trace useless for the case that most needs
    one."""
    trace = _recorded(ev(0, targets=(Target(id=1, x=1.5, y=2.5),)))
    clean = sanitize(trace)
    assert clean["events"][0]["event"]["targets"][0]["x"] == 1.5


def test_positions_can_be_dropped_for_a_trace_that_leaves_the_house():
    trace = _recorded(ev(0, targets=(Target(id=1, x=1.5, y=2.5),)))
    clean = sanitize(trace, drop_positions=True)
    t = clean["events"][0]["event"]["targets"][0]
    assert t["x"] is None and t["y"] is None
    assert "target positions" in clean["sanitized_removed"]


def test_a_sanitised_trace_records_what_was_removed():
    """So a recipient can check the promise instead of trusting a filename."""
    clean = sanitize(_recorded(ev(0)))
    assert clean["sanitized"] is True
    for field in SANITIZED_ALWAYS:
        assert field in clean["sanitized_removed"]


def test_a_raw_trace_is_marked_raw():
    assert _recorded(ev(0))["sanitized"] is False


def test_sanitising_does_not_change_what_replay_produces():
    """Removing identities and vitals must not alter the fusion result — if it
    did, a sanitised trace would reproduce a different bug from the real one."""
    trace = _recorded(ev(0, identities=(Identity(person="Ana", source="ble"),), breathing=14.0),
                      ev(3))
    raw_states = [s.to_dict() for s in replay(trace, _engine)]
    clean_states = [s.to_dict() for s in replay(sanitize(trace), _engine)]
    for r, c in zip(raw_states, clean_states):
        assert r["occupied"] == c["occupied"]
        assert r["confidence"] == c["confidence"]
        assert r["person_count"] == c["person_count"]


def test_sanitize_refuses_something_that_is_not_a_trace():
    with pytest.raises(TraceError):
        sanitize("not a trace")


# -- Bounds --------------------------------------------------------------------

def test_the_recorder_is_bounded_and_says_so():
    """An unbounded recorder left on by accident fills the disk with a movement
    log of somebody's home."""
    r = TraceRecorder(max_events=3)
    for i in range(10):
        r.record(ev(i))
    trace = r.to_dict()
    assert len(trace["events"]) == 3
    assert trace["truncated"] is True and trace["dropped"] == 7


def test_truncation_keeps_the_beginning():
    """A trace is opened to study how a situation developed. Losing the start
    loses the explanation and keeps only the symptom."""
    r = TraceRecorder(max_events=2)
    for i in range(5):
        r.record(ev(i))
    kept = [row["event"]["ts"] for row in r.to_dict()["events"]]
    assert kept == [ev(0).ts, ev(1).ts]


# -- Round trip ----------------------------------------------------------------

def test_a_trace_survives_being_written_and_read(tmp_path):
    trace = _recorded(ev(0), ev(1, present=False))
    path = save(trace, tmp_path / "t.json")
    again = load(path)
    assert [s.to_dict() for s in replay(trace, _engine)] == \
           [s.to_dict() for s in replay(again, _engine)]


def test_an_unreadable_file_is_refused_clearly(tmp_path):
    p = tmp_path / "junk.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(TraceError, match="could not read"):
        load(p)


def test_a_file_that_is_not_a_trace_is_refused(tmp_path):
    p = tmp_path / "other.json"
    p.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(TraceError, match="not a Wavr trace"):
        load(p)


def test_a_future_trace_version_is_refused_rather_than_guessed(tmp_path):
    """Read under the wrong shape, a trace produces room states that look
    plausible and are wrong — worse than an error message."""
    trace = _recorded(ev(0))
    trace["version"] = TRACE_VERSION + 1
    p = save(trace, tmp_path / "future.json")
    with pytest.raises(TraceError, match="cannot be read"):
        load(p)


# -- Summary -------------------------------------------------------------------

def test_a_summary_says_what_is_inside_without_replaying_it():
    trace = _recorded(ev(0, room="sala", sensor_id="hall-cam"),
                      ev(30, room="cozinha", sensor_id="kitchen-radar",
                         modality="mmwave"))
    s = summarize(trace)
    assert s["events"] == 2
    assert s["rooms"] == ["cozinha", "sala"]
    assert s["sensors"] == ["hall-cam", "kitchen-radar"]
    assert s["modalities"] == ["camera", "mmwave"]
    assert s["span_s"] == 30.0
    assert s["sanitized"] is False


def test_the_summary_flags_a_sanitised_trace():
    s = summarize(sanitize(_recorded(ev(0))))
    assert s["sanitized"] is True
    assert "identities" in s["sanitized_removed"]
