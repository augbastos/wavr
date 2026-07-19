"""No-motion (stillness) detection: the honesty gate (never judge a blind room), the
continuous-still timer, and the engine's once-per-episode no_motion firing."""
from datetime import datetime, timedelta, timezone

from wavr.routines import RoutineStore, RoutinesEngine
from wavr.stillness import (STILL_MAX_FRAME_GAP_S, STILL_MOVE_GRACE_S,
                            StillnessDetector, room_motionless)


# --------------------------------------------------------------------------- #
# room_motionless — the honesty gate
# --------------------------------------------------------------------------- #
def test_room_motionless_cannot_judge_an_empty_or_blind_room():
    assert room_motionless(False, [{"velocity": 0.0}]) is None, "not occupied -> unknowable"
    assert room_motionless(True, []) is None, "occupied but NO velocity signal -> unknowable"
    assert room_motionless(True, [{"id": 1}]) is None, "target without velocity -> unknowable"


def test_room_motionless_true_when_still_false_when_moving():
    assert room_motionless(True, [{"velocity": 0.02}, {"velocity": 0.0}]) is True
    assert room_motionless(True, [{"velocity": 0.02}, {"velocity": 0.9}]) is False, "one mover -> moving"


# --------------------------------------------------------------------------- #
# StillnessDetector — the continuous-still timer
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

    def iso(self):
        return self.t.isoformat()

    def add(self, seconds):
        self.t += timedelta(seconds=seconds)


def _still_run(d, clock, seconds, step=60, room="sala"):
    """Feed still frames `step` seconds apart (well within the observation window,
    like the real per-frame cadence) to represent `seconds` of continuous stillness,
    returning the final elapsed."""
    elapsed = 0.0
    for _ in range(int(seconds // step)):
        clock.add(step)
        elapsed = d.update(room, True)
    return elapsed


def test_stillness_accumulates_and_resets_on_real_movement():
    c = _Clock()
    d = StillnessDetector(now_fn=c.iso)
    assert d.update("sala", True) == 0.0, "first still frame = baseline, 0 elapsed"
    assert _still_run(d, c, 600) == 600.0, "10 min of in-window still frames"
    assert _still_run(d, c, 600) == 1200.0, "20 min still"
    # sustained movement (> grace) ends the episode
    c.add(1); assert d.update("sala", False) == 1200.0, "a brief move is within grace -> frozen"
    c.add(STILL_MOVE_GRACE_S + 1); assert d.update("sala", False) == 0.0, "sustained move -> reset"
    c.add(300); assert d.update("sala", True) == 0.0, "new episode starts fresh"


def test_stillness_tolerates_a_brief_unknowable_gap():
    c = _Clock()
    d = StillnessDetector(now_fn=c.iso)
    d.update("sala", True)
    assert _still_run(d, c, 3600) == 3600.0                  # 1h of in-window still frames
    c.add(2); assert d.update("sala", None) == 3600.0        # brief unknowable -> frozen, not reset
    c.add(1); assert d.update("sala", True) >= 3600.0        # still counting after the blip


def test_stillness_does_not_stitch_across_a_long_silence():
    # F2 (ADR-0003): a still frame arriving after a gap LONGER than a frame window is a
    # room we did NOT observe continuously -- it must start fresh, never claim the whole
    # unobserved gap as "continuously still" (the WAVR_REFUSE_S=0 / node-went-silent case).
    c = _Clock()
    d = StillnessDetector(now_fn=c.iso)
    d.update("sala", True)
    c.add(STILL_MAX_FRAME_GAP_S + 1)
    assert d.update("sala", True) == 0.0, "an unobserved gap is not stitched into stillness"


def test_malformed_ts_ends_the_episode_truthfully():
    # F4: a malformed ts returns 0.0 AND ends the episode, so the sentinel is honest and a
    # later good frame can't re-fire a phantom episode left dangling by the bad frame.
    c = _Clock()
    d = StillnessDetector(now_fn=c.iso)
    d.update("sala", True)
    c.add(60); assert d.update("sala", True) == 60.0
    assert d.update("sala", True, ts="not-a-timestamp") == 0.0, "malformed ts -> episode ended"
    c.add(5); assert d.update("sala", True) == 0.0, "no stale re-fire; the clock restarted"


def test_sustained_unknowable_ends_the_episode_no_false_stillness():
    c = _Clock()
    d = StillnessDetector(now_fn=c.iso)
    d.update("sala", True); c.add(3600); d.update("sala", True)
    c.add(STILL_MOVE_GRACE_S + 5)
    assert d.update("sala", None) == 0.0, "lost the ability to judge -> stop asserting stillness"


# --------------------------------------------------------------------------- #
# Engine on_stillness — once per episode, re-arm on reset
# --------------------------------------------------------------------------- #
def _eng_with_no_motion(minutes=180):
    s = RoutineStore(":memory:")
    r = s.add("guardian", "no_motion", trigger_params={"room": "sala", "minutes": minutes},
              actions=[{"kind": "notify", "params": {"message": "no movement"}}])
    s.set_enabled(r["id"], True)
    return RoutinesEngine(s, sensing_on=lambda: True), r["id"]


def test_no_motion_fires_once_when_threshold_crossed_then_holds():
    eng, rid = _eng_with_no_motion(minutes=180)   # 3h = 10800s
    assert eng.on_stillness("sala", 100) == [], "under threshold -> no fire"
    assert [x["id"] for x in eng.on_stillness("sala", 10800)] == [rid], "3h crossed -> fires"
    assert eng.on_stillness("sala", 11000) == [], "still still -> does NOT re-fire same episode"


def test_no_motion_re_arms_after_movement():
    eng, rid = _eng_with_no_motion(minutes=1)
    assert [x["id"] for x in eng.on_stillness("sala", 120)] == [rid], "fires"
    assert eng.on_stillness("sala", 0) == [], "movement (elapsed 0) ends the episode + re-arms"
    assert [x["id"] for x in eng.on_stillness("sala", 120)] == [rid], "fires again next episode"


def test_no_motion_is_per_room():
    eng, rid = _eng_with_no_motion(minutes=1)
    assert eng.on_stillness("cozinha", 10000) == [], "a different room never fires the sala routine"


def test_no_motion_cache_refreshes_when_a_routine_is_enabled_later():
    # F3: on_stillness uses a version-gated cache to skip a per-frame store read. Prove the
    # cache stays LIVE -- a routine added/enabled after the engine was built still fires.
    s = RoutineStore(":memory:")
    eng = RoutinesEngine(s, sensing_on=lambda: True)
    assert eng.on_stillness("sala", 10000) == [], "no no_motion routine yet -> nothing fires"
    r = s.add("guardian", "no_motion", trigger_params={"room": "sala", "minutes": 1},
              actions=[{"kind": "notify", "params": {"message": "m"}}])
    s.set_enabled(r["id"], True)
    assert [x["id"] for x in eng.on_stillness("sala", 120)] == [r["id"]], \
        "the store version bumped, the cache refreshed, the new routine takes effect"


def test_no_motion_requires_room_and_positive_minutes():
    import pytest
    with pytest.raises(ValueError):
        RoutineStore(":memory:").add("x", "no_motion", trigger_params={"room": "sala"},
                                     actions=[{"kind": "notify", "params": {"message": "m"}}])
    with pytest.raises(ValueError):
        RoutineStore(":memory:").add("x", "no_motion", trigger_params={"room": "sala", "minutes": 0},
                                     actions=[{"kind": "notify", "params": {"message": "m"}}])
