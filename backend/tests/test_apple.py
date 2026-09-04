"""Apple spatial interop, Core side.

The two tests that carry this file: a discovery token is not a device identity,
and a distance without a direction does not become a position. Both are places
where the convenient thing to do produces a confident wrong answer about
somebody's home.
"""
import math

import pytest

from wavr.apple import (
    APPLE_PROTOCOL_VERSION, AppleError, apply_alignment, check_anchor_id,
    check_token, describe, offset_from, parse_nearby_object, provider_id,
    solve_alignment,
)

TOKEN = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
UUID = "1f2e3d4c-5b6a-7089-9a8b-0c1d2e3f4a5b"


# -- A token is a session, not a device ----------------------------------------

def test_a_discovery_token_is_shape_checked_but_never_an_identity():
    """An integration that stored a token as "this is Augusto's phone" would
    hold a dead reference by the next session — and would confidently attribute
    a stranger's phone to Augusto if a token were ever reused."""
    assert check_token(TOKEN) == TOKEN
    out = parse_nearby_object({"discovery_token": TOKEN, "distance_m": 2.0})
    assert "device_id" not in out and "person" not in out
    assert "identifies a SESSION" in out["note"]


def test_a_malformed_token_is_refused():
    for bad in ("", "not base64!!", "short", None, "x" * 900):
        with pytest.raises(AppleError):
            check_token(bad)


# -- A distance is not a position ----------------------------------------------

def test_a_distance_without_a_direction_produces_no_offset():
    """A range constrains the peer to a SPHERE. Collapsing that to a point — by
    assuming "straight ahead", or by picking the room centre — would be
    inventing a position."""
    reading = parse_nearby_object({"discovery_token": TOKEN, "distance_m": 2.1})
    assert reading["has_direction"] is False
    assert offset_from(reading) is None


def test_the_distance_itself_survives_and_is_useful():
    """It tells you which anchor somebody is near, which is a real answer."""
    reading = parse_nearby_object({"discovery_token": TOKEN, "distance_m": 2.1})
    assert reading["distance_m"] == 2.1


def test_a_direction_and_a_distance_together_do_give_an_offset():
    reading = parse_nearby_object({"discovery_token": TOKEN, "distance_m": 2.0,
                                   "direction": [1.0, 0.0, 0.0]})
    assert offset_from(reading) == pytest.approx((2.0, 0.0, 0.0))


def test_a_direction_is_normalised_rather_than_trusted_as_a_unit_vector():
    reading = parse_nearby_object({"discovery_token": TOKEN, "distance_m": 3.0,
                                   "direction": [0.0, 0.0, 7.0]})
    assert math.isclose(sum(v * v for v in reading["direction"]), 1.0)
    # ARKit's floor plane is XZ, so a +Z direction lands on the plan's y.
    assert offset_from(reading) == pytest.approx((0.0, 3.0, 0.0))


def test_a_zero_vector_is_refused_rather_than_normalised():
    """Normalising it would invent a direction out of nothing."""
    with pytest.raises(AppleError, match="no magnitude"):
        parse_nearby_object({"discovery_token": TOKEN, "distance_m": 1.0,
                             "direction": [0.0, 0.0, 0.0]})


def test_a_reading_with_neither_says_nothing_and_is_refused():
    with pytest.raises(AppleError, match="says nothing"):
        parse_nearby_object({"discovery_token": TOKEN})


def test_a_units_mistake_in_the_distance_is_refused():
    """The same class of check anchors apply: Nearby Interaction works to about
    nine metres, so a four-digit value is millimetres or garbage."""
    with pytest.raises(AppleError, match="out of range"):
        parse_nearby_object({"discovery_token": TOKEN, "distance_m": 2100.0})


def test_a_non_numeric_distance_is_refused():
    with pytest.raises(AppleError):
        parse_nearby_object({"discovery_token": TOKEN, "distance_m": "close"})


def test_a_malformed_direction_is_refused():
    for bad in ("north", [1.0], {"x": 1}):
        with pytest.raises(AppleError):
            parse_nearby_object({"discovery_token": TOKEN, "distance_m": 1.0,
                                 "direction": bad})


# -- ARKit anchors: identity is free, coordinates are not ----------------------

def test_anchor_uuids_are_namespaced_per_app_bundle():
    """An ARWorldMap and its anchors belong to the app that built them. The same
    UUID from two apps is two different places."""
    assert provider_id("com.example.tour") != provider_id("com.other.app")


def test_an_anchor_mapping_without_a_bundle_is_refused():
    with pytest.raises(AppleError, match="name the app bundle"):
        provider_id("  ")


def test_an_anchor_identifier_is_checked():
    assert check_anchor_id(UUID.upper()) == UUID
    with pytest.raises(AppleError):
        check_anchor_id("not-a-uuid")


def test_an_arkit_world_is_aligned_by_the_shared_solver():
    """ARKit's problem is OpenXR's problem. Giving it its own implementation
    would produce two subtly different answers to one question."""
    a = solve_alignment("kitchen", [
        ((0.0, 0.0, 0.0), (1.0, 1.0)),
        ((2.0, 0.0, 0.0), (3.0, 1.0)),
    ])
    assert a.frame == "arkit-world"
    assert a.trustworthy
    assert apply_alignment(a, 1.0, 0.0, 0.0) == pytest.approx((2.0, 1.0, 0.0))


def test_one_correspondence_is_refused_here_too():
    with pytest.raises(AppleError, match="at least 2"):
        solve_alignment("kitchen", [((0.0, 0.0, 0.0), (1.0, 1.0))])


def test_malformed_correspondences_are_refused():
    with pytest.raises(AppleError, match="malformed"):
        solve_alignment("kitchen", [((0, 0, 0), (0, 0)), "nope"])


# -- The declaration says what has not been done -------------------------------

def test_the_provider_does_not_claim_position():
    """A UWB range is accurate to centimetres and a range is still a sphere.
    Declaring `position` would promise the rare best case as though it were
    normal."""
    d = describe()
    assert d.precision_ceiling == "room"
    assert d.confidence_semantics == "quality", "a range is not a probability"


def test_the_declaration_admits_nothing_has_run_on_an_iphone():
    """In the notes, where somebody evaluating the integration will read it —
    not in a footnote somewhere else."""
    assert "has run on an iPhone" in describe().notes


def test_the_contract_is_versioned():
    assert APPLE_PROTOCOL_VERSION == 1
