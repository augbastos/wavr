"""Each source's share of the vote, published by the one thing that computes it.

The dashboard's evidence panel used to derive this from a COPY of
`DEFAULT_WEIGHTS` kept in the frontend. That copy knew the modality constants
and nothing else — not the freshness decay, not the per-sensor reliability
factor — so it printed an authoritative-looking percentage that was wrong for
precisely the two sensors somebody opens that panel about: the one that is aging
and the one that has been measured as unreliable.
"""
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine


def ev(modality, confidence, *, room="sala", ts=None, sensor_id=None,
       presence=True):
    return SensingEvent(
        room=room, modality=modality, presence=presence, confidence=confidence,
        ts=(ts or datetime.now(timezone.utc)).isoformat(),
        motion=False, breathing_bpm=None, heart_bpm=None,
        sensor_id=sensor_id or modality)


def sources_for(events, **kw):
    f = FusionEngine(**kw)
    for e in events:
        f.update(e)
    return f.state("sala").to_dict()["sources"]


def test_the_shares_sum_to_the_whole_vote():
    rows = sources_for([ev("camera", 0.9), ev("ble", 0.6)])
    assert round(sum(r["share"] for r in rows), 3) == 1.0


def test_an_aging_source_contributes_less_than_its_nominal_weight():
    """The freshness decay is half of what the frontend's copy did not know."""
    now = datetime.now(timezone.utc)
    fresh = sources_for([ev("camera", 0.9), ev("ble", 0.9)])
    # Past FRESHNESS_S (30s) and short of STALE_S (90s), where trust decays
    # linearly. Inside the freshness window decay is exactly 1.0, and an earlier
    # version of this test picked 25s and proved nothing.
    aged = sources_for([ev("camera", 0.9, ts=now - timedelta(seconds=60)),
                        ev("ble", 0.9)])
    cam_fresh = next(r for r in fresh if r["modality"] == "camera")["share"]
    cam_aged = next(r for r in aged if r["modality"] == "camera")["share"]
    assert cam_aged < cam_fresh, (
        f"an aging camera kept its full share: {cam_aged} vs {cam_fresh}")


def test_a_sensor_measured_unreliable_contributes_less():
    """The other half. A frontend constant cannot know this at all: it is a
    measurement about one particular sensor in one particular room."""
    def unreliable(sensor_id, room):
        return (0.25, "missed you twice during a guided walk") \
            if sensor_id == "ble" else (1.0, "")

    rows = sources_for([ev("camera", 0.9), ev("ble", 0.9)],
                       reliability_fn=unreliable)
    by = {r["modality"]: r for r in rows}
    assert by["ble"]["share"] < by["camera"]["share"]
    # And the reason travels with it, because a number that moved without a
    # reason is a number nobody can check.
    assert by["ble"]["reliability"] == 0.25
    assert "guided walk" in by["ble"]["reliability_reason"]


def test_a_source_that_contributed_nothing_reads_zero_rather_than_vanishing():
    """A sensor that voted on nothing is a fact worth seeing. Hiding the row
    would make "it is not helping" indistinguishable from "it is not there"."""
    rows = sources_for([ev("camera", 0.9), ev("ble", 0.0)])
    ble = next(r for r in rows if r["modality"] == "ble")
    assert ble["share"] == 0.0
    assert "share" in ble


def test_the_internal_mass_is_not_published():
    """Only the SHARE means anything to a reader; the raw magnitude is an
    implementation detail and publishing it invites somebody to compare two."""
    rows = sources_for([ev("camera", 0.9)])
    assert all("_mass" not in r for r in rows)


def test_the_frontend_no_longer_keeps_its_own_weight_table():
    """One producer. A second copy of the weights is a second answer, and the
    two diverge silently — the frontend's never learned about decay at all.

    Searched across index.html AND every module it loads, because the frontend
    stopped being one file. This read index.html only, and 783 KB of logic
    moved out of it into `frontend/js/`: the ratchet went on passing while
    guarding a shell that no longer contains any logic to guard.
    """
    from tests.frontend_source import ALL, module_holding
    assert "FUSION_WEIGHTS = {" not in ALL, (
        f"the frontend has its own fusion weight table again, in "
        f"{module_holding('FUSION_WEIGHTS = {')}")
