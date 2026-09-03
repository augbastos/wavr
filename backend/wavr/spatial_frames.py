"""Which frame a coordinate is in, and how to move between them.

## Why this exists

Two sources already fill `events.Target.x/y`, in two different frames, and
nothing said so. The camera path runs PIXEL → FLOOR → ROOM-LOCAL through
`localize.to_room_local()`. The mmWave path emits millimetres from the RADAR, in
the radar's own axes, and never transforms them. Both land in a field whose
docstring promises room-local metres, and the map draws them as if that were
true.

A radar's dot is therefore in the wrong place unless the radar happens to sit at
the room polygon's minimum corner facing +x. Worse, the precision ladder sees a
target with coordinates and promotes the room to `position` — so Wavr reports its
highest confidence about the answer it is least able to give.

## The rule

**A coordinate without a frame is not a position.** A source declares the frame
it speaks in; a consumer that cannot transform it must treat it as absent rather
than assume. That is the same rule the rest of this codebase applies to unknown
capabilities and unknown counts: silence beats a confident guess.

## The frames

`ROOM`   — metres, offset from the room polygon's minimum corner, x right,
           y down. What `Target.x/y` has always claimed to be, and what the
           renderer expects.
`SENSOR` — the emitting sensor's own origin and axes. Physically meaningful to
           that sensor and meaningless anywhere else without its mount.

FLOOR/HOUSE exists too (see `localize`'s module docstring) but no source emits a
Target in it — the camera path converts before building one — so it is not in
this vocabulary. Adding a frame nothing emits would be the same mistake as a
constant nothing produces.
"""
from __future__ import annotations

import math

# The frame a coordinate is expressed in. Small and closed on purpose: every
# member has a producer today.
FRAME_ROOM = "room"
FRAME_SENSOR = "sensor"

FRAMES: frozenset[str] = frozenset({FRAME_ROOM, FRAME_SENSOR})


class FrameError(ValueError):
    """A transform that cannot be performed honestly."""


def sensor_to_room(x: float, y: float, *, mount_x: float, mount_y: float,
                   yaw_deg: float, room_min_x: float = 0.0,
                   room_min_y: float = 0.0) -> tuple[float, float]:
    """One sensor-local point into the room frame, given where the sensor is.

    The sensor sits at `(mount_x, mount_y)` on the FLOOR plan, facing `yaw_deg`
    (0 = +x, turning toward +y — the same convention `localize.MountPose`
    already uses for cameras, so an operator who has placed a camera has already
    learned this one).

    A sensor reports a point ahead of and beside itself. Rotating by the mount's
    heading puts that into floor axes; adding the mount position puts it at the
    right place on the plan; subtracting the room's minimum corner expresses it
    the way `Target` promises.

    Deliberately 2D. The LD2450 reports a plane, and inventing a z from a
    2D sensor would be the fabrication this module exists to stop.
    """
    if not all(map(math.isfinite, (x, y, mount_x, mount_y, yaw_deg))):
        raise FrameError("a non-finite coordinate cannot be transformed")
    rad = math.radians(yaw_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    floor_x = mount_x + (x * cos_a - y * sin_a)
    floor_y = mount_y + (x * sin_a + y * cos_a)
    return (floor_x - room_min_x, floor_y - room_min_y)


def transform_target(target, *, mount=None, room_min=(0.0, 0.0)):
    """A Target in whatever frame it declared, returned in the room frame — or
    with its position REMOVED when that cannot be done honestly.

    Returns `(target, transformed)`. `transformed` is False when the position was
    dropped, so a caller can tell "already correct" from "could not be made
    correct" — which are different things and must not both look like success.

    The dropping is the important half. A sensor-frame target with no mount has
    a real presence, a real count and a meaningless coordinate; keeping the
    coordinate is what puts a person in the wrong corner of a floor plan and
    then calls it position-level precision.
    """
    frame = getattr(target, "frame", FRAME_ROOM)
    if frame == FRAME_ROOM:
        return target, False
    if frame not in FRAMES:
        # An unrecognised frame is not a licence to guess. Same direction as
        # every other unknown here: drop the claim, keep the observation.
        return _without_position(target), False
    if mount is None or target.x is None or target.y is None:
        return _without_position(target), False
    try:
        rx, ry = sensor_to_room(
            target.x, target.y,
            mount_x=mount.pos_x, mount_y=mount.pos_y, yaw_deg=mount.yaw_deg,
            room_min_x=room_min[0], room_min_y=room_min[1])
    except (FrameError, AttributeError, TypeError):
        # A malformed mount is a configuration problem, not evidence that the
        # person is at the origin.
        return _without_position(target), False
    return _replace(target, x=rx, y=ry, frame=FRAME_ROOM), True


def _without_position(target):
    """Keep everything the sensor genuinely knows; drop the coordinate.

    Posture, velocity, confidence and the target's existence all survive — a
    radar that cannot be placed still knows somebody is moving.
    """
    if target.x is None and target.y is None:
        return target
    return _replace(target, x=None, y=None, frame=FRAME_ROOM)


def _replace(target, **changes):
    """`dataclasses.replace` without importing dataclasses into the hot path,
    and tolerant of a Target-shaped object that is not a dataclass (the tests
    and the node path both build these by hand)."""
    from dataclasses import replace as dc_replace
    try:
        return dc_replace(target, **changes)
    except TypeError:
        for k, v in changes.items():
            setattr(target, k, v)
        return target
