"""Two sensors of the same kind are two voices, not one slot.

The merge used to key on modality alone, so a second camera in a room silently
replaced the first. Everything here is a behaviour that was impossible then:
independent decay, visible disagreement, and a second sensor actually adding
evidence instead of overwriting it.
"""
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _at(sec):
    return (T0 + timedelta(seconds=sec)).isoformat()


def cam(room, sensor_id, present, conf=0.9, count=None, sec=0):
    return SensingEvent(room=room, modality="camera", presence=present, motion=0.0,
                        breathing_bpm=None, heart_bpm=None,
                        confidence=conf if present else 0.0, ts=_at(sec),
                        count=count, sensor_id=sensor_id)


def _engine():
    # now_fn pinned so freshness is measured against the fixture's clock, not
    # the wall clock — otherwise every event is instantly ancient.
    return FusionEngine(now_fn=lambda: T0 + timedelta(seconds=90))


# -- Coexistence: the bug this change fixes -----------------------------------

def test_two_cameras_in_one_room_both_survive_the_merge():
    """The regression. Before this, `hall-b` overwrote `hall-a` and the room had
    one camera no matter how many were installed."""
    f = FusionEngine(now_fn=lambda: T0)
    f.update(cam("hall", "hall-a", True))
    rs = f.update(cam("hall", "hall-b", True))

    ids = sorted(s["sensor_id"] for s in rs.sources)
    assert ids == ["hall-a", "hall-b"], "both cameras are in the merge"


def test_a_source_with_no_identity_still_occupies_one_slot():
    """Backward compatibility. Every non-camera source emits one instance per
    room with an empty id, so `(modality, "")` must behave exactly as the old
    per-modality slot did — the second event replaces the first."""
    f = FusionEngine(now_fn=lambda: T0)
    f.update(cam("hall", "", True, conf=0.5))
    rs = f.update(cam("hall", "", True, conf=0.9))

    assert len(rs.sources) == 1
    assert rs.sources[0]["confidence"] == 0.9, "the newer reading replaced the older"


# -- Disagreement is visible, not resolved away -------------------------------

def test_two_cameras_that_disagree_both_appear():
    """The anti-skeptic requirement: never hide dissent behind a merged number.

    Before, the disagreement could not even be represented — one camera erased
    the other, so the room reported whichever spoke last with full confidence.
    """
    f = FusionEngine(now_fn=lambda: T0)
    f.update(cam("office", "office-cam", True, conf=0.9))
    rs = f.update(cam("office", "corner-cam", False))

    by_id = {s["sensor_id"]: s for s in rs.sources}
    assert by_id["office-cam"]["presence"] is True
    assert by_id["corner-cam"]["presence"] is False, "the dissent is on the record"


def test_dissent_is_recorded_even_though_absence_carries_no_mass():
    """What the per-sensor merge does and does not change.

    It changes the RECORD: both cameras are in `sources[]`, so a human or an
    agent can see that one of them says empty. It does not change the NUMBER,
    and that is the pre-existing design rather than an oversight — every
    first-party source emits `confidence=0.0` when it reports absence, so a
    camera saying "empty" contributes zero mass. A camera that fails to see a
    still person would otherwise be able to argue a room empty, which is the
    weaker claim.

    Asserted explicitly so that if absence ever starts carrying weight, this
    test forces the decision to be deliberate.
    """
    f = FusionEngine(now_fn=lambda: T0)
    alone = f.update(cam("office", "office-cam", True, conf=0.9))

    f2 = FusionEngine(now_fn=lambda: T0)
    f2.update(cam("office", "office-cam", True, conf=0.9))
    disputed = f2.update(cam("office", "corner-cam", False))

    assert disputed.confidence == alone.confidence, "absence carries no mass"
    assert len(disputed.sources) == 2, "but the disagreement is on the record"


# -- Independent freshness ----------------------------------------------------

def test_one_camera_going_stale_does_not_gate_the_other():
    """Decay is per sensor. A camera that stopped reporting an hour ago must not
    drag a healthy one down, and — before this change — the stale one would have
    been the room's only camera anyway."""
    f = _engine()
    f.update(cam("hall", "old-cam", True, sec=0))       # 90s old at fuse time
    rs = f.update(cam("hall", "new-cam", True, sec=88))  # 2s old

    by_id = {s["sensor_id"]: s for s in rs.sources}
    assert by_id["old-cam"]["health"] != "fresh"
    assert by_id["new-cam"]["health"] == "fresh"
    assert rs.occupied is True, "the healthy camera still carries the room"


# -- Counting stays honest across two counters --------------------------------

def test_the_higher_weighted_count_wins_but_both_counts_are_shown():
    """Deterministic resolution, transparent inputs.

    Two cameras counting differently is a real disagreement. The merge picks one
    (same precedence rule as before) and still puts both numbers in `sources[]`,
    so nobody has to trust the resolution blindly.
    """
    f = FusionEngine(now_fn=lambda: T0)
    f.update(cam("kitchen", "kitchen-a", True, count=2))
    rs = f.update(cam("kitchen", "kitchen-b", True, count=3))

    counts = sorted(s["count"] for s in rs.sources)
    assert counts == [2, 3], "both counts are visible"
    assert rs.person_count in (2, 3), "one of them was chosen, none invented"


def test_a_second_camera_reporting_empty_does_not_fabricate_a_zero():
    """UNKNOWN != FALSE survives the change.

    A camera that says empty contributes absence evidence; it must never turn
    the room's count into a fabricated 0 when the other camera counted nobody
    either — with no counting source vouching for a number, the answer is None.
    """
    f = FusionEngine(now_fn=lambda: T0)
    f.update(cam("den", "den-a", False))
    rs = f.update(cam("den", "den-b", False))

    assert rs.person_count is None, "no count-capable source vouched; not zero"
    assert rs.occupied is False
