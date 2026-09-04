"""OpenXR interop — mostly a set of refusals.

The tests that matter are the ones where Wavr declines to produce a number: one
corresponded anchor is not enough to know which way a room faces, and an
alignment that fits badly is not an alignment. Every one of those refusals is a
place where the easy alternative is a plausible wrong answer about somebody's
home.
"""
import math

import pytest

from wavr.openxr import (
    SCOPE_LOCAL_ANCHORS, SCOPE_SYSTEM_MANAGED, SPACE_LOCAL, SPACE_VIEW,
    STATE_NOT_FOUND, OpenXRError, apply_alignment, check_uuid, describe,
    parse_entity, provider_id, solve_alignment, xr_to_floor,
)
# The alignment maths is vendor-neutral and shared — a second copy per vendor
# would drift, and the drift would be a room rotated in one integration and not
# the other.
from wavr.spatial_align import MAX_RESIDUAL_M, Alignment

UUID_A = "1f2e3d4c-5b6a-7089-9a8b-0c1d2e3f4a5b"
UUID_B = "aabbccdd-eeff-0011-2233-445566778899"


# -- Axes ----------------------------------------------------------------------

def test_the_floor_plane_is_openxrs_xz_and_wavrs_xy():
    """OpenXR is +Y up and -Z forward; a floor plan is +Y down the page with +Z
    as height. This is the only place that knowledge lives."""
    assert xr_to_floor(1.0, 2.0, 3.0) == (1.0, 3.0, 2.0)


def test_height_survives_the_conversion():
    assert xr_to_floor(0.0, 1.7, 0.0)[2] == 1.7


# -- Solving an alignment ------------------------------------------------------

def test_two_anchors_solve_yaw_and_translation():
    a = solve_alignment("kitchen", [
        ((0.0, 0.0, 0.0), (1.0, 1.0)),
        ((2.0, 0.0, 0.0), (3.0, 1.0)),
    ])
    assert a.samples == 2
    assert a.yaw_deg == pytest.approx(0.0, abs=1e-6)
    assert (a.dx, a.dy) == pytest.approx((1.0, 1.0))
    assert a.trustworthy


def test_a_rotated_room_is_recovered():
    """The whole reason yaw has to be solved: the headset's session started
    facing wherever the user happened to be facing."""
    theta = math.radians(37.0)
    pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 1.5)]
    corr = []
    for fx, fy in pts:
        rx = math.cos(theta) * fx - math.sin(theta) * fy + 4.0
        ry = math.sin(theta) * fx + math.cos(theta) * fy - 2.0
        # xr_to_floor maps (x, y, z) -> (x, z, y), so the floor point goes in
        # as x and z.
        corr.append(((fx, 0.0, fy), (rx, ry)))
    a = solve_alignment("kitchen", corr)
    assert a.yaw_deg == pytest.approx(37.0, abs=1e-6)
    assert a.residual_m < 1e-9


def test_one_anchor_is_refused_with_the_reason():
    """Translation without yaw would render the room at an arbitrary angle, and
    the application would have no way to tell."""
    with pytest.raises(OpenXRError, match="at least 2"):
        solve_alignment("kitchen", [((0.0, 0.0, 0.0), (1.0, 1.0))])


def test_none_at_all_is_refused():
    with pytest.raises(OpenXRError):
        solve_alignment("kitchen", [])


def test_anchors_all_in_one_place_give_no_baseline():
    """Two correspondences at the same point is one correspondence."""
    with pytest.raises(OpenXRError, match="same spot"):
        solve_alignment("kitchen", [
            ((1.0, 0.0, 1.0), (2.0, 2.0)),
            ((1.0, 0.0, 1.0), (2.0, 2.0)),
        ])


def test_scale_is_never_solved():
    """Both systems are metres by specification. Solving for scale could only
    let one bad correspondence resize somebody's house — and the result would
    look entirely reasonable."""
    a = solve_alignment("kitchen", [
        ((0.0, 0.0, 0.0), (0.0, 0.0)),
        ((1.0, 0.0, 0.0), (2.0, 0.0)),      # twice the distance
    ])
    # A rigid fit cannot absorb this, so it shows up as residual rather than
    # disappearing into a scale factor.
    assert a.residual_m > 0.4
    assert "scale" in a.to_dict()["note"].lower()


def test_a_bad_fit_is_reported_rather_than_absorbed():
    corr = [((0.0, 0.0, 0.0), (0.0, 0.0)),
            ((2.0, 0.0, 0.0), (2.0, 0.0)),
            ((0.0, 0.0, 2.0), (5.0, 9.0))]     # wrong correspondence
    a = solve_alignment("kitchen", corr)
    assert a.residual_m > MAX_RESIDUAL_M
    assert a.trustworthy is False


def test_the_residual_is_published_because_two_anchors_always_fit():
    """Two fit perfectly and say nothing about accuracy; three that fit to 4 cm
    say a great deal."""
    d = solve_alignment("kitchen", [
        ((0.0, 0.0, 0.0), (0.0, 0.0)),
        ((2.0, 0.0, 0.0), (2.0, 0.0)),
    ]).to_dict()
    assert "residual_m" in d and d["samples"] == 2


def test_the_view_space_cannot_be_aligned_to():
    """It is the headset. It moves with the user."""
    with pytest.raises(OpenXRError, match="VIEW"):
        solve_alignment("kitchen", [((0, 0, 0), (0, 0)), ((1, 0, 0), (1, 0))],
                        reference_space=SPACE_VIEW)


def test_an_unknown_reference_space_is_refused():
    with pytest.raises(OpenXRError):
        solve_alignment("kitchen", [((0, 0, 0), (0, 0)), ((1, 0, 0), (1, 0))],
                        reference_space="WHEREVER")


def test_malformed_correspondences_are_refused():
    with pytest.raises(OpenXRError, match="malformed"):
        solve_alignment("kitchen", [((0, 0, 0), (0, 0)), "nonsense"])


# -- Applying one -------------------------------------------------------------

def test_a_point_is_placed_once_an_alignment_is_trustworthy():
    a = solve_alignment("kitchen", [
        ((0.0, 0.0, 0.0), (1.0, 1.0)),
        ((2.0, 0.0, 0.0), (3.0, 1.0)),
    ])
    assert apply_alignment(a, 1.0, 0.0, 0.0) == pytest.approx((2.0, 1.0, 0.0))


def test_an_untrustworthy_alignment_places_nothing():
    """The same rule spatial_frames applies to a sensor target whose frame
    cannot be resolved: a coordinate without one is not a position."""
    bad = Alignment(room="kitchen", frame=SPACE_LOCAL, yaw_deg=0.0,
                    dx=0.0, dy=0.0, residual_m=9.0, samples=3)
    assert apply_alignment(bad, 1.0, 0.0, 0.0) is None


def test_a_single_sample_alignment_places_nothing_either():
    lonely = Alignment(room="kitchen", frame=SPACE_LOCAL, yaw_deg=0.0,
                       dx=0.0, dy=0.0, residual_m=0.0, samples=1)
    assert apply_alignment(lonely, 1.0, 0.0, 0.0) is None


# -- The scoping rule that stops UUIDs colliding -------------------------------

def test_local_anchor_uuids_are_namespaced_per_application():
    """That scope persists per device, per user AND per app. The same UUID from
    two apps names two different places."""
    a = provider_id(SCOPE_LOCAL_ANCHORS, "com.example.tour")
    b = provider_id(SCOPE_LOCAL_ANCHORS, "com.other.game")
    assert a != b


def test_a_local_anchor_without_an_application_is_refused():
    with pytest.raises(OpenXRError, match="name the application"):
        provider_id(SCOPE_LOCAL_ANCHORS, "")


def test_system_managed_anchors_share_one_namespace():
    """Those ARE shared across applications on a device."""
    assert provider_id(SCOPE_SYSTEM_MANAGED) == "openxr:system"
    assert provider_id(SCOPE_SYSTEM_MANAGED, "anything") == "openxr:system"


def test_an_unknown_scope_is_refused():
    with pytest.raises(OpenXRError):
        provider_id("whatever", "app")


# -- Untrusted input -----------------------------------------------------------

def test_a_uuid_is_checked_rather_than_stored_as_given():
    assert check_uuid(UUID_A.upper()) == UUID_A
    for bad in ("", "not-a-uuid", "1f2e3d4c5b6a70899a8b0c1d2e3f4a5b", None):
        with pytest.raises(OpenXRError):
            check_uuid(bad)


def test_an_entity_is_parsed_whole_or_refused():
    """A half-read entity would be stored with a UUID and no scope, which is
    exactly the state that makes two runtimes' anchors collide."""
    out = parse_entity({"uuid": UUID_A, "scope": SCOPE_SYSTEM_MANAGED})
    assert out["provider_id"] == "openxr:system"
    assert out["reference_space"] == SPACE_LOCAL
    for bad in ({"uuid": UUID_A}, {"scope": SCOPE_SYSTEM_MANAGED},
                {"uuid": UUID_A, "scope": "made_up"}, "not an object"):
        with pytest.raises(OpenXRError):
            parse_entity(bad)


def test_a_transient_entity_id_is_recorded_but_never_an_identity():
    """`XrSpatialEntityIdEXT` is valid only inside one spatial context, so
    persisting it would create a reference that is dead by the next session and
    still looks alive."""
    out = parse_entity({"uuid": UUID_A, "scope": SCOPE_SYSTEM_MANAGED,
                        "entity_id": "0x7f2a"})
    assert out["entity_id"] == "0x7f2a"
    assert out["uuid"] == UUID_A, "the UUID is what identifies it"


def test_not_found_is_a_state_a_runtime_may_report():
    """A persisted UUID can come back missing, and an integration that treated
    that as an error would break on a perfectly ordinary reinstall."""
    out = parse_entity({"uuid": UUID_A, "scope": SCOPE_LOCAL_ANCHORS,
                        "application": "com.example.tour",
                        "state": STATE_NOT_FOUND})
    assert out["state"] == STATE_NOT_FOUND


# -- The declaration -----------------------------------------------------------

def test_the_provider_does_not_claim_position():
    """A headset knows where it is to centimetres — in ITS OWN frame. What it
    can tell Wavr depends on an alignment, and a descriptor is a static claim
    that cannot depend on runtime state."""
    d = describe()
    assert d.precision_ceiling == "room"
    assert d.coordinate_frame == "sensor"
    assert d.reach == "lan" and d.leaves_the_home is False
