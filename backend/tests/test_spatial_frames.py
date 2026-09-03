"""A coordinate without a frame is not a position.

The bug this file pins: `Target` promised room-local metres, the camera path
delivered them, and the mmWave path delivered millimetres-from-the-radar. Both
landed in the same field and the map drew them identically — so a radar's person
appeared wherever they happened to stand relative to the radar, and the precision
ladder called that `position`.
"""
import math
from datetime import datetime, timezone

import pytest

from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.localize import MountPose
from wavr.spatial_frames import (
    FRAME_ROOM, FRAME_SENSOR, FrameError, sensor_to_room, transform_target,
)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _mount(x=3.0, y=2.0, yaw=0.0):
    return MountPose(pos_x=x, pos_y=y, height=1.2, tilt_deg=10.0, yaw_deg=yaw,
                     hfov_deg=90.0)


# -- The transform itself -----------------------------------------------------

def test_a_sensor_facing_along_x_just_translates():
    """yaw 0 means the sensor's +x IS the floor's +x, so only the mount offset
    applies. The simplest case, and the one worth getting exactly right."""
    x, y = sensor_to_room(2.0, 1.0, mount_x=3.0, mount_y=2.0, yaw_deg=0.0)
    assert (round(x, 6), round(y, 6)) == (5.0, 3.0)


def test_a_rotated_sensor_rotates_its_readings():
    """A radar turned 90 degrees reports "2m ahead" for something that is 2m
    along the floor's +y. Getting this backwards mirrors the room."""
    x, y = sensor_to_room(2.0, 0.0, mount_x=0.0, mount_y=0.0, yaw_deg=90.0)
    assert round(x, 6) == 0.0
    assert round(y, 6) == 2.0


def test_the_room_origin_is_subtracted():
    """Targets are offset from the room polygon's min corner, not from the
    floor plan's origin — that is what `Target` has always promised."""
    x, y = sensor_to_room(1.0, 1.0, mount_x=5.0, mount_y=5.0, yaw_deg=0.0,
                          room_min_x=4.0, room_min_y=4.0)
    assert (round(x, 6), round(y, 6)) == (2.0, 2.0)


def test_a_non_finite_coordinate_is_refused():
    """NaN through a rotation produces NaN, which renders as a dot at nowhere."""
    with pytest.raises(FrameError):
        sensor_to_room(float("nan"), 0.0, mount_x=0.0, mount_y=0.0, yaw_deg=0.0)


# -- Placing, or honestly refusing to place -----------------------------------

def test_a_room_frame_target_passes_through_untouched():
    t = Target(id=1, x=1.5, y=2.5, frame=FRAME_ROOM)
    out, changed = transform_target(t, mount=_mount())
    assert out is t and changed is False


def test_a_sensor_frame_target_with_a_mount_is_placed():
    t = Target(id=1, x=2.0, y=1.0, frame=FRAME_SENSOR)
    out, changed = transform_target(t, mount=_mount(x=3.0, y=2.0, yaw=0.0))
    assert changed is True
    assert (round(out.x, 6), round(out.y, 6)) == (5.0, 3.0)
    assert out.frame == FRAME_ROOM, "it is a room coordinate now; say so"


def test_a_sensor_frame_target_with_no_mount_loses_its_position():
    """The heart of it. An unplaceable coordinate is removed, not rendered.

    Keeping it is what put a person in the wrong corner of the floor plan and
    then let the precision ladder call it position-level.
    """
    t = Target(id=1, x=2.0, y=1.0, posture="walking", velocity=0.8,
               confidence=0.9, frame=FRAME_SENSOR)
    out, changed = transform_target(t, mount=None)
    assert changed is False
    assert out.x is None and out.y is None


def test_dropping_the_position_keeps_everything_else_the_sensor_knows():
    """A radar that cannot be placed still knows somebody is moving, and how
    fast. Throwing that away to fix the coordinate would be an overcorrection."""
    t = Target(id=1, x=2.0, y=1.0, posture="walking", velocity=0.8,
               confidence=0.9, frame=FRAME_SENSOR)
    out, _ = transform_target(t, mount=None)
    assert out.posture == "walking"
    assert out.velocity == 0.8
    assert out.confidence == 0.9


def test_an_unrecognised_frame_is_not_a_licence_to_guess():
    t = Target(id=1, x=2.0, y=1.0, frame="martian")
    out, changed = transform_target(t, mount=_mount())
    assert changed is False and out.x is None


def test_a_malformed_mount_drops_the_position_rather_than_placing_it_at_zero():
    class Broken:
        pos_x = float("nan")
        pos_y = 0.0
        yaw_deg = 0.0

    t = Target(id=1, x=2.0, y=1.0, frame=FRAME_SENSOR)
    out, changed = transform_target(t, mount=Broken())
    assert changed is False and out.x is None


# -- What the radar actually emits --------------------------------------------

def test_the_mmwave_source_declares_the_sensor_frame():
    """Regression on the original defect: the LD2450 parser used to emit its
    own millimetres as though they were room-local."""
    import struct

    from wavr.sources.mmwave import parse_ld2450_frame

    def enc(v):                       # sign-magnitude int16
        return (0x8000 | v) if v >= 0 else (-v)

    slot = struct.pack("<HHHH", enc(1500), enc(2000), enc(0), 320)
    frame = b"\xaa\xff\x03\x00" + slot + b"\x00" * 16 + b"\x55\xcc"

    targets = parse_ld2450_frame(frame)
    assert targets, "the fixture should decode one target"
    assert all(t.frame == FRAME_SENSOR for t in targets)
    # Still the radar's own millimetres-turned-metres — the values did not
    # move, only the claim about what they mean.
    assert (targets[0].x, targets[0].y) == (1.5, 2.0)


# -- Fusion honours it ---------------------------------------------------------

def _radar_event(room="sala", sensor_id="kitchen-radar", x=2.0, y=1.0):
    return SensingEvent(
        room=room, modality="mmwave", presence=True, motion=0.5,
        breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=T0.isoformat(),
        count=1, sensor_id=sensor_id,
        targets=(Target(id=1, x=x, y=y, confidence=0.9, frame=FRAME_SENSOR),))


def test_fusion_places_a_radar_target_when_the_mount_is_known():
    f = FusionEngine(now_fn=lambda: T0,
                     mount_fn=lambda sid, room: _mount(x=3.0, y=2.0, yaw=0.0))
    rs = f.update(_radar_event())
    t = rs.targets[0]
    assert (round(t["x"], 6), round(t["y"], 6)) == (5.0, 3.0)


def test_fusion_unplaces_a_radar_target_with_no_mount():
    f = FusionEngine(now_fn=lambda: T0, mount_fn=lambda sid, room: None)
    rs = f.update(_radar_event())
    assert rs.targets[0]["x"] is None


def test_an_unplaceable_target_cannot_earn_the_position_rung():
    """The consequence that matters: without this the room reported `position`
    precision on the strength of a coordinate that meant nothing outside the
    radar — Wavr's highest confidence about its least reliable answer."""
    f = FusionEngine(now_fn=lambda: T0, mount_fn=lambda sid, room: None)
    rs = f.update(_radar_event())
    assert rs.occupied is True
    assert rs.person_count == 1, "the count is real and survives"
    assert rs.precision_level == "count", "but not position"


def test_a_placed_target_does_earn_the_position_rung():
    f = FusionEngine(now_fn=lambda: T0,
                     mount_fn=lambda sid, room: _mount())
    rs = f.update(_radar_event())
    assert rs.precision_level == "position"


def test_a_failing_mount_lookup_does_not_invent_a_place():
    def angry(sensor_id, room):
        raise RuntimeError("db locked")

    f = FusionEngine(now_fn=lambda: T0, mount_fn=angry)
    rs = f.update(_radar_event())
    assert rs.targets[0]["x"] is None
    assert rs.precision_level == "count"


def test_no_mount_fn_wired_still_unplaces_sensor_frame_targets():
    """A Core with no mount store must not fall back to the old behaviour of
    rendering radar-local coordinates as room-local."""
    f = FusionEngine(now_fn=lambda: T0)
    rs = f.update(_radar_event())
    assert rs.targets[0]["x"] is None


def test_a_camera_target_is_unaffected():
    """The camera already produces room-local coordinates. Nothing about this
    change may move them."""
    ev = SensingEvent(
        room="sala", modality="camera", presence=True, motion=0.0,
        breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=T0.isoformat(),
        count=1, sensor_id="hall-cam",
        targets=(Target(id=1, x=1.25, y=3.5, confidence=0.9),))
    f = FusionEngine(now_fn=lambda: T0, mount_fn=lambda sid, room: _mount())
    rs = f.update(ev)
    assert (rs.targets[0]["x"], rs.targets[0]["y"]) == (1.25, 3.5)
