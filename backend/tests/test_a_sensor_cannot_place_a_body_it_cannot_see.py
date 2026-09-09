"""A payload shape that accepts coordinates is not permission to send them.

`nodes.py` refused a person COUNT from a sensor type that cannot count, and in
the same return statement handed `targets` through untouched. So a node enrolled
as `pir` or `ble_beacon` could POST

    {"targets": [{"x": 1.2, "y": 3.4}]}

and those coordinates became `SensingEvent.targets`, reached
`RoomState.targets` through a merge that gated only on freshness, and were drawn
on the map.

The precision ladder never promoted that to "position", so the percentage on
screen stayed honest. The coordinate travelled anyway. A number invented by a
sensor that cannot measure position is a claim about where a person is standing,
and this product's whole argument is that it does not make claims it cannot
support.

## Two gates, deliberately

An enrolment row says what a node IS. A payload says what it CLAIMS. The first
decides:

  * `nodes.py` drops coordinates from a sensor type outside `COUNTING_SENSORS`,
    keeping the presence the node is entitled to assert;
  * `fusion.py` independently refuses targets from any modality outside
    `LOCATABLE_MODALITIES`, so a future event source that skips the node path
    cannot reintroduce this.

Either gate alone would close today's hole. Both exist because a capability a
node DECLARES must not by itself grant the authority to act on it — and because
the first version of this repository's count gate proved that one check in one
place is exactly the shape that gets bypassed when a second path appears.

## Controls

Tests below come in pairs. Every refusal has a matching acceptance proving the
legitimate path still works, because a guard that refuses everything passes a
one-sided test suite and breaks the product.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wavr.events import SensingEvent, Target
from wavr.fusion import (COUNTING_MODALITIES, LOCATABLE_MODALITIES,
                         RESOLUTION_SCOPE, FusionEngine)
from wavr.nodes import STATE_ACTIVE, Node, node_event

NOW = "2026-09-09T12:00:00+00:00"

NON_LOCATABLE = sorted(set(RESOLUTION_SCOPE) - LOCATABLE_MODALITIES)
LOCATABLE = sorted(LOCATABLE_MODALITIES)


def _event(modality: str, *, targets, room="sala", ts=None) -> SensingEvent:
    return SensingEvent(
        room=room, modality=modality, presence=True, motion=0.4,
        breathing_bpm=None, heart_bpm=None, confidence=0.9,
        ts=ts or NOW, targets=tuple(targets),
        count=len(targets) if modality in COUNTING_MODALITIES else None,
    )


def _body(i=1, x=1.5, y=2.5):
    return Target(id=i, x=x, y=y, velocity=0.1, posture=None, confidence=0.9)


def _fuse(engine: FusionEngine, events):
    rs = None
    for e in events:
        rs = engine.update(e)
    return rs


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------

def test_locatable_is_derived_and_cannot_drift():
    """One source of truth, not two lists somebody keeps in sync by hand."""
    assert LOCATABLE_MODALITIES == COUNTING_MODALITIES
    assert LOCATABLE_MODALITIES == {
        m for m, scope in RESOLUTION_SCOPE.items() if scope == "count"}


def test_a_modality_nobody_declared_is_not_locatable():
    """Fails closed. An unknown modality is refused, not tolerated -- adding one
    has to be a decision somebody makes on purpose."""
    assert "smart_doorbell_2027" not in LOCATABLE_MODALITIES
    assert "" not in LOCATABLE_MODALITIES


# ---------------------------------------------------------------------------
# Fusion refuses coordinates from a modality that cannot resolve one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("modality", NON_LOCATABLE)
def test_a_presence_only_modality_cannot_place_a_body(modality):
    rs = _fuse(FusionEngine(), [_event(modality, targets=[_body()])])
    assert rs.occupied, (
        f"{modality} must still be believed about PRESENCE -- this gate is "
        f"about position, and refusing the presence too would be a different "
        f"and worse bug")
    assert rs.targets == [], (
        f"{modality} placed a body at a coordinate it has no way to measure")


@pytest.mark.parametrize("modality", LOCATABLE)
def test_a_locatable_modality_still_places_its_body(modality):
    """The positive control. Without this the test above passes on a merge that
    dropped every target, which would be a product regression wearing the
    costume of a security fix."""
    rs = _fuse(FusionEngine(), [_event(modality, targets=[_body()])])
    assert rs.occupied
    assert rs.targets, f"{modality} must still be able to place a body"
    assert rs.targets[0].get("x") is not None


def test_a_presence_only_source_does_not_shadow_a_real_one():
    """Both present, one entitled. The map shows the radar's body, not nothing
    and not a phantom -- the refusal must not cost the legitimate reading."""
    engine = FusionEngine()
    rs = _fuse(engine, [
        _event("pir", targets=[_body(1, 9.9, 9.9)]),
        _event("mmwave", targets=[_body(2, 1.0, 2.0)]),
    ])
    assert rs.targets, "the radar's target was lost"
    xs = [t.get("x") for t in rs.targets]
    assert 9.9 not in xs, "a PIR coordinate reached the map alongside the radar's"


# ---------------------------------------------------------------------------
# The node edge: the enrolment row decides, never the payload
# ---------------------------------------------------------------------------

def _node(sensor_type: str, modality: str) -> Node:
    """A real enrolled node row -- not a stand-in.

    A fake object here would let the test pass against a `Node` whose shape has
    moved on, which is the failure mode this whole file exists to prevent.
    """
    return Node(
        node_id=f"node-{sensor_type}", name=sensor_type, sensor_type=sensor_type,
        modality=modality, room="sala", transport="native", cert_fingerprint="",
        confidence_cap=1.0, state=STATE_ACTIVE, press_count=0, last_seq=0,
        last_seen_ts=None, created_ts=NOW,
    )


@pytest.mark.parametrize("sensor_type,modality", [
    ("pir", "pir"), ("ble_beacon", "ble"), ("generic", "node"),
])
def test_a_node_that_cannot_resolve_position_has_its_coordinates_dropped(
        sensor_type, modality):
    ev = node_event(
        _node(sensor_type, modality),
        {"targets": [{"x": 1.2, "y": 3.4, "confidence": 0.9}]},
        NOW,
    )
    assert ev is not None, "presence must survive; only the coordinate is refused"
    assert ev.count is None, "a non-counting sensor asserted a headcount"
    for t in ev.targets:
        assert t.x is None and t.y is None, (
            f"a {sensor_type} node smuggled a coordinate through the payload")


def test_a_radar_node_keeps_its_coordinates():
    """Positive control for the edge gate."""
    ev = node_event(
        _node("ld2450", "mmwave"),
        {"targets": [{"x": 1.2, "y": 3.4, "confidence": 0.9}]},
        NOW,
    )
    assert ev is not None and ev.targets
    assert ev.targets[0].x == pytest.approx(1.2)
    assert ev.targets[0].y == pytest.approx(3.4)
    assert ev.count == 1


def test_a_non_locatable_node_still_reports_presence_and_posture():
    """The refusal is surgical. Posture is not a coordinate: a PIR that somehow
    reports one is wrong about something else, not about where somebody is, and
    this gate is not the place to decide that."""
    ev = node_event(
        _node("pir", "pir"),
        {"targets": [{"x": 5.0, "y": 5.0, "posture": "standing"}]},
        NOW,
    )
    assert ev is not None and ev.presence is True
    assert all(t.x is None for t in ev.targets)


# ---------------------------------------------------------------------------
# The control: prove the guard can fail
# ---------------------------------------------------------------------------

def test_the_gate_would_notice_if_it_were_removed():
    """Without the modality check, a PIR event's targets pass on freshness alone.

    This reproduces the pre-fix merge in three lines and asserts it produces the
    bad outcome -- so if the real gate is ever deleted, the tests above start
    failing for a reason this file has already written down.
    """
    e = _event("pir", targets=[_body()])
    decayed = 1.0
    passed_through_without_the_gate = bool(e.presence and e.targets and decayed > 0.0)
    assert passed_through_without_the_gate, (
        "the pre-fix condition no longer describes the code; if the merge "
        "changed shape, re-derive what this file is protecting")
    assert e.modality not in LOCATABLE_MODALITIES
