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


def test_a_camera_that_says_empty_now_lowers_the_number_as_well_as_the_record():
    """The deliberate decision this test was written to force.

    It used to assert the opposite — that a camera saying "empty" changed the
    RECORD and not the NUMBER — and it said why it was worded that way:
    "Asserted explicitly so that if absence ever starts carrying weight, this
    test forces the decision to be deliberate." It went red the moment the
    change landed, which is the whole reason it was worth writing.

    The decision: a fresh negative from a modality that CAN prove absence is
    evidence. Two cameras in one room, one seeing somebody and one seeing an
    empty room, is a genuinely uncertain situation, and it must not read like
    two cameras agreeing. The room can still be occupied — a camera with a
    partial view is a real thing — and `occupied` stays True off the present
    camera's own count; what falls is the certainty, which is the honest part.

    Only trusted-absence modalities do this. `test_a_radar_dropout_is_not_a_
    verdict` below holds the other half.
    """
    f = FusionEngine(now_fn=lambda: T0)
    alone = f.update(cam("office", "office-cam", True, conf=0.9))

    f2 = FusionEngine(now_fn=lambda: T0)
    f2.update(cam("office", "office-cam", True, conf=0.9))
    disputed = f2.update(cam("office", "corner-cam", False))

    assert disputed.confidence < alone.confidence, (
        f"a camera saying the room is empty left the confidence at "
        f"{disputed.confidence} — the same as the one camera alone, so the "
        f"disagreement is in the record and nowhere else")
    assert disputed.confidence > 0, (
        "one dissent erased the present camera entirely; a disagreement is "
        "uncertainty, not proof of an empty room")
    assert len(disputed.sources) == 2, "and the disagreement is still recorded"


def test_a_radar_dropout_is_not_a_verdict():
    """The other half, and the reason this is not just "absence counts now".

    A person who sits still vanishes from mmWave. If that silence lowered the
    room's confidence, every quiet evening would look like a disagreement —
    which is why `TRUSTED_ABSENCE_MODALITIES` exists and why the count latch a
    hundred lines below already refuses to release on a radar negative.

    So the same shape as the test above, with radar in place of the second
    camera, must leave the number exactly where it was.
    """
    f = FusionEngine(now_fn=lambda: T0)
    alone = f.update(cam("lounge", "lounge-cam", True, conf=0.9))

    f2 = FusionEngine(now_fn=lambda: T0)
    f2.update(cam("lounge", "lounge-cam", True, conf=0.9))
    quieto = f2.update(SensingEvent(
        room="lounge", modality="mmwave", presence=False, motion=0.0,
        breathing_bpm=None, heart_bpm=None, confidence=0.0, ts=_at(0),
        count=None, sensor_id="lounge-radar"))

    assert quieto.confidence == alone.confidence, (
        f"a radar that stopped seeing a still person moved the confidence to "
        f"{quieto.confidence}; a dropout is silence, not a claim that the room "
        f"is empty")


def test_a_camera_that_stopped_reporting_asserts_nothing():
    """Staleness applies to a dissent exactly as it applies to a claim.

    A camera whose last word was "empty" an hour ago is not evidence about the
    room now, and letting it keep voting would be the freezing-on-a-last-
    reading failure the freshness decay exists to prevent — just pointed the
    other way.
    """
    UMA_HORA = 3600
    agora = T0 + timedelta(seconds=UMA_HORA)

    f = FusionEngine(now_fn=lambda: agora)
    f.update(cam("study", "study-cam-old", False, sec=0))        # an hour ago
    rs = f.update(cam("study", "study-cam", True, conf=0.9, sec=UMA_HORA))

    f2 = FusionEngine(now_fn=lambda: agora)
    sozinho = f2.update(cam("study", "study-cam", True, conf=0.9,
                            sec=UMA_HORA))

    assert rs.confidence == sozinho.confidence, (
        f"a camera that last spoke an hour ago still lowered the number to "
        f"{rs.confidence}; a stale source asserts nothing, in either direction")


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
