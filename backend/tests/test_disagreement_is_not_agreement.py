"""Two sources that contradict each other must not read like two that agree.

`agreement` sat inert in `fusion.py` for a long time, and the module said so in
its own docstring: every first-party source emits `confidence=0.0` with
`presence=False`, so an absent source contributed zero mass to the numerator
AND the denominator, dropped out of the ratio, and `agreement` was identically
1.0 whenever anything was present. The fused confidence was exactly `strength`
— the best present evidence — and no amount of contradiction moved it.

The distinction that makes the fix safe was already in this module, decided for
a different question: `TRUSTED_ABSENCE_MODALITIES`. A fresh mmWave
`presence=False` is a still person the radar lost — the very dropout the count
latch exists to bridge — and reading it as an empty room would flicker the
house every time somebody sat still. A fresh CAMERA `presence=False` is a look
at an empty room. The count latch has acted on that difference for months; the
confidence path had never heard of it.

So this file is the matrix, one scenario per test, each stating what it holds:

    two strong sources agreeing      -> unchanged
    two strong sources conflicting   -> confidence falls
    strong + weak conflicting        -> falls less than strong + strong
    current + stale                  -> the stale one asserts nothing
    a source that cannot prove absence -> its silence is not a verdict
    an offline source                -> contributes nothing either way
    no evidence at all               -> zero, and not "empty"
    presence-only beside count-capable -> presence survives, count does not

The point of writing it as a matrix rather than as one test of the new
behaviour: the risk in this change is not that disagreement fails to register,
it is that something ELSE starts registering as disagreement. Most of these
tests assert that nothing moved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import (COUNTING_MODALITIES, TRUSTED_ABSENCE_MODALITIES,
                         FusionEngine)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _ev(room, modality, present, *, conf=0.9, sec=0, sensor_id=None,
        count=None):
    return SensingEvent(
        room=room, modality=modality, presence=present, motion=0.0,
        breathing_bpm=None, heart_bpm=None,
        # The wire convention every first-party source follows: an absence
        # carries confidence 0.0. It is exactly why a dissent cannot be scaled
        # by the event's own confidence.
        confidence=(conf if present else 0.0),
        ts=(T0 + timedelta(seconds=sec)).isoformat(),
        count=count, sensor_id=(sensor_id or f"{modality}-1"))


def _motor(sec=0):
    return FusionEngine(now_fn=lambda: T0 + timedelta(seconds=sec))


# -- The premise these tests rest on ------------------------------------------

def test_the_two_kinds_of_absence_are_still_different_kinds():
    """The control. Every scenario below turns on this distinction, and if the
    sets ever collapsed into each other, half this file would pass by accident.
    """
    assert "camera" in TRUSTED_ABSENCE_MODALITIES, (
        "the modality this file uses as the one that can prove absence no "
        "longer claims to")
    assert "mmwave" not in TRUSTED_ABSENCE_MODALITIES, (
        "radar now counts as proof of absence; the dropout tests below are no "
        "longer testing what they say")
    assert TRUSTED_ABSENCE_MODALITIES <= COUNTING_MODALITIES


# -- Agreement, which must not move -------------------------------------------

def test_two_strong_sources_that_agree_read_as_two_strong_sources_that_agree():
    f = _motor()
    f.update(_ev("sala", "camera", True, conf=0.90))
    juntos = f.update(_ev("sala", "mmwave", True, conf=0.88))

    so_camera = _motor().update(_ev("sala", "camera", True, conf=0.90))
    assert juntos.confidence >= so_camera.confidence, (
        f"two sources that agree ({juntos.confidence}) came out below the "
        f"stronger one alone ({so_camera.confidence})")


# -- Conflict, which must ----------------------------------------------------

def test_two_strong_sources_that_conflict_do_not_read_as_two_that_agree():
    """The headline. Same two readings, one flipped, and the numbers must part."""
    concordam = _motor()
    concordam.update(_ev("sala", "camera", True, conf=0.90, sensor_id="cam-a"))
    juntos = concordam.update(
        _ev("sala", "camera", True, conf=0.88, sensor_id="cam-b"))

    discordam = _motor()
    discordam.update(_ev("sala", "camera", True, conf=0.90, sensor_id="cam-a"))
    brigando = discordam.update(_ev("sala", "camera", False, sensor_id="cam-b"))

    assert brigando.confidence < juntos.confidence, (
        f"agreeing came out at {juntos.confidence} and conflicting at "
        f"{brigando.confidence} — a contradiction reads as certain as a "
        f"corroboration")


def test_a_weak_dissent_costs_less_than_a_strong_one():
    """Reliability is claim-specific, and it applies to a dissent too.

    A camera the Core has measured as unreliable disagreeing is worth less than
    a healthy one disagreeing. Driven through the engine's own reliability
    hook rather than a second mechanism.
    """
    def fraca(sensor_id, room):
        return (0.2, "measured unreliable") if sensor_id == "cam-b" else (1.0, "")

    forte = _motor()
    forte.update(_ev("sala", "camera", True, conf=0.90, sensor_id="cam-a"))
    com_forte = forte.update(_ev("sala", "camera", False, sensor_id="cam-b"))

    f = FusionEngine(now_fn=lambda: T0, reliability_fn=fraca)
    f.update(_ev("sala", "camera", True, conf=0.90, sensor_id="cam-a"))
    com_fraca = f.update(_ev("sala", "camera", False, sensor_id="cam-b"))

    assert com_fraca.confidence > com_forte.confidence, (
        f"a dissent from a camera measured at 0.2 reliability cost the same as "
        f"a healthy one ({com_fraca.confidence} vs {com_forte.confidence})")


def test_a_dissent_never_erases_the_evidence_that_somebody_is_there():
    """A disagreement is uncertainty, not a verdict for the other side."""
    f = _motor()
    f.update(_ev("sala", "camera", True, conf=0.95, sensor_id="cam-a"))
    rs = f.update(_ev("sala", "camera", False, sensor_id="cam-b"))
    assert rs.confidence > 0.0, (
        "one camera saying empty drove the confidence to zero; the other "
        "camera can still see somebody")


# -- The absences that are NOT evidence ---------------------------------------

def test_a_source_that_cannot_prove_absence_is_not_treated_as_proof_of_absence():
    """A still person vanishing from radar is silence, not a claim."""
    f = _motor()
    sozinho = f.update(_ev("quarto", "camera", True, conf=0.90))
    com_radar_mudo = f.update(_ev("quarto", "mmwave", False))
    assert com_radar_mudo.confidence == sozinho.confidence, (
        f"a radar dropout moved the confidence from {sozinho.confidence} to "
        f"{com_radar_mudo.confidence}")


def test_a_network_source_going_quiet_is_not_a_claim_about_the_room():
    """Network presence is the weakest evidence in the product and it cannot
    see a room at all. Its silence must be worth exactly nothing."""
    f = _motor()
    sozinho = f.update(_ev("hall", "camera", True, conf=0.90))
    com_rede = f.update(_ev("hall", "network", False))
    assert com_rede.confidence == sozinho.confidence, (
        f"a quiet network source moved the number to {com_rede.confidence}")


def test_a_stale_dissent_asserts_nothing():
    """Freshness cuts both ways, or a camera unplugged yesterday keeps voting."""
    UMA_HORA = 3600
    f = _motor(sec=UMA_HORA)
    f.update(_ev("cozinha", "camera", False, sec=0, sensor_id="cam-velha"))
    rs = f.update(_ev("cozinha", "camera", True, conf=0.9, sec=UMA_HORA,
                      sensor_id="cam-nova"))

    limpo = _motor(sec=UMA_HORA).update(
        _ev("cozinha", "camera", True, conf=0.9, sec=UMA_HORA,
            sensor_id="cam-nova"))

    assert rs.confidence == limpo.confidence, (
        f"a camera whose last word was an hour ago still lowered the number "
        f"({rs.confidence} vs {limpo.confidence})")


# -- No evidence is not evidence of nothing -----------------------------------

def test_a_room_with_no_evidence_at_all_is_not_a_room_reported_empty():
    """Zero confidence, and `occupied` False — but the reason must be absence
    of evidence, which is what the empty `sources[]` says."""
    f = _motor()
    rs = f.update(_ev("garagem", "camera", False))
    assert rs.confidence == 0.0
    assert rs.occupied is False
    # The dissent is still ON THE RECORD, which is how a reader tells "nothing
    # is watching" from "something looked and saw nobody".
    assert any(s["presence"] is False for s in rs.sources), (
        "a camera that looked and saw an empty room left no trace; that is "
        "indistinguishable from a room nothing watches")


# -- Count and presence are different claims ----------------------------------

def test_a_presence_only_source_still_vouches_for_presence_when_the_camera_dissents():
    """The claim-specific rule, end to end.

    A camera saying "empty" releases the count — it can see, and it counted
    nobody. It does not silence a presence-only source, which is making a
    different and weaker claim it is entitled to make. The room reads
    "somebody is here, we do not know how many", which is the honest answer.
    """
    f = _motor()
    f.update(_ev("sala", "mmwave", True, conf=0.85))
    rs = f.update(_ev("sala", "camera", False))

    assert rs.person_count is None, (
        f"the camera looked and saw nobody, and the room still claims "
        f"{rs.person_count} people")
    assert any(s["modality"] == "mmwave" and s["presence"] for s in rs.sources), (
        "the radar's own reading was dropped from the record")
    assert rs.confidence > 0.0, (
        "a camera's dissent silenced a presence-only source that is making a "
        "claim the camera did not contradict")
