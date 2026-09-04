"""How wrong an answer might be, and the number Wavr refuses to invent.

Every competing product prints a metres figure, and one here would be easy: pick
a plausible radius per modality and nobody could tell. A camera's positional
error depends on its calibration, its mounting height and the room's geometry; a
radar's on its aim and the furniture. Wavr knows none of those.

So most of this file is about the difference between "we measured it" and "we
have not checked", which is the confusion the whole module exists to prevent.
"""
from dataclasses import dataclass

from wavr.spatial_uncertainty import (
    BASIS_MEASURED, BASIS_NONE, BASIS_UNMEASURED, GRANULARITY, assess,
)


@dataclass
class FakeProfile:
    measured: bool = True
    accuracy: float | None = 0.9


def cov(sensor_id="cam1", room="kitchen", health="ok"):
    return {"sensor_id": sensor_id, "room": room, "health": health}


# -- The number that is never invented -----------------------------------------

def test_no_error_figure_appears_without_a_measurement():
    out = assess("kitchen", precision="position",
                 coverage_rows=[cov()]).to_dict()
    assert "residual_m" not in out
    assert "will not print an error figure it did not measure" in out["note"]


def test_a_real_measured_residual_is_carried_through():
    """The one place a genuine metres figure exists: the worst per-point error
    after fitting an XR runtime's frame onto a room, from correspondences a
    person supplied."""
    out = assess("kitchen", precision="position", coverage_rows=[cov()],
                 residual_m=0.14).to_dict()
    assert out["residual_m"] == 0.14


def test_the_module_holds_no_per_modality_error_table():
    """A table of plausible radii would be decoration shaped like rigour, and
    every application built on it would inherit a confidence nobody earned."""
    import inspect

    from wavr import spatial_uncertainty
    src = inspect.getsource(spatial_uncertainty)
    for suspicious in ("_ERROR_M", "RADIUS", "ERROR_BY_MODALITY", "TYPICAL_"):
        assert suspicious not in src, suspicious


# -- Measured versus not measured ----------------------------------------------

def test_an_unchecked_room_says_so_rather_than_looking_fine():
    """"We have not checked" must never read as "it is fine". That confusion is
    the whole reason a basis is reported at all."""
    out = assess("kitchen", precision="room", coverage_rows=[cov()]).to_dict()
    assert out["basis"] == BASIS_UNMEASURED
    assert "has not been measured here" in out["note"]
    assert out["worst_accuracy"] is None


def test_a_checked_room_reports_what_was_measured():
    out = assess("kitchen", precision="count", coverage_rows=[cov()],
                 profile_fn=lambda *_a: FakeProfile(True, 0.87)).to_dict()
    assert out["basis"] == BASIS_MEASURED
    assert out["measured_sensors"] == 1 and out["sensors"] == 1
    assert out["worst_accuracy"] == 0.87
    assert "87% of the time" in out["note"]


def test_the_worst_sensor_sets_the_figure():
    """A room is as trustworthy as its least trustworthy contributor, and an
    average would hide the one that is wrong."""
    profiles = {"cam1": FakeProfile(True, 0.95), "radar1": FakeProfile(True, 0.6)}
    out = assess("kitchen", precision="count",
                 coverage_rows=[cov("cam1"), cov("radar1")],
                 profile_fn=lambda sid, *_a: profiles[sid]).to_dict()
    assert out["worst_accuracy"] == 0.6


def test_a_sensor_with_too_few_samples_counts_as_unmeasured_not_as_a_low_score():
    """`reliability` treats an unmeasured sensor at full trust because it has
    not earned a penalty. Turning that into an accuracy figure here would make
    "we have not checked" into a number."""
    out = assess("kitchen", precision="room", coverage_rows=[cov()],
                 profile_fn=lambda *_a: FakeProfile(measured=False,
                                                    accuracy=None)).to_dict()
    assert out["basis"] == BASIS_UNMEASURED
    assert out["measured_sensors"] == 0


def test_a_mixed_room_reports_how_many_were_checked():
    profiles = {"cam1": FakeProfile(True, 0.9),
                "radar1": FakeProfile(measured=False, accuracy=None)}
    out = assess("kitchen", precision="count",
                 coverage_rows=[cov("cam1"), cov("radar1")],
                 profile_fn=lambda sid, *_a: profiles[sid]).to_dict()
    assert out["measured_sensors"] == 1 and out["sensors"] == 2
    assert "1 of 2 sensors" in out["note"]


def test_a_broken_reliability_read_does_not_decide_the_answer():
    def boom(*_a):
        raise OSError("store is gone")
    out = assess("kitchen", precision="room", coverage_rows=[cov()],
                 profile_fn=boom).to_dict()
    assert out["basis"] == BASIS_UNMEASURED


# -- Granularity, which is always knowable -------------------------------------

def test_each_rung_states_what_the_answer_is_about():
    for rung in ("house", "room", "count", "position"):
        out = assess("kitchen", precision=rung, coverage_rows=[cov()]).to_dict()
        assert out["granularity"] == GRANULARITY[rung]


def test_a_house_scope_answer_says_it_cannot_tell_rooms_apart():
    """A fact about the technology, not a guess: one antenna localises to the
    house however confident it is."""
    out = assess("kitchen", precision="house", coverage_rows=[cov()]).to_dict()
    assert "cannot tell rooms apart" in out["granularity"]


# -- Nothing reporting ----------------------------------------------------------

def test_a_room_with_nothing_working_has_no_answer_to_be_uncertain_about():
    out = assess("kitchen", precision="room",
                 coverage_rows=[cov(health="offline")]).to_dict()
    assert out["basis"] == BASIS_NONE
    assert "no answer to be uncertain about" in out["note"]


def test_a_room_with_no_sensors_at_all_is_the_same():
    assert assess("attic", precision="none").to_dict()["basis"] == BASIS_NONE


def test_sensors_in_other_rooms_do_not_count():
    out = assess("kitchen", precision="room",
                 coverage_rows=[cov(room="hall")]).to_dict()
    assert out["basis"] == BASIS_NONE


def test_confidence_is_not_uncertainty_and_this_module_never_reads_it():
    """`confidence` is about PRESENCE — how much fused mass says somebody is
    there. Reusing it as a precision figure is the conflation this module was
    written to separate."""
    import inspect

    from wavr import spatial_uncertainty
    assert "confidence" not in inspect.getsource(spatial_uncertainty.assess)
