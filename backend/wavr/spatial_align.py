"""Fitting somebody else's coordinate frame onto a Wavr room.

Vendor-neutral on purpose. OpenXR's `LOCAL` space, ARKit's world origin and every
enterprise positioning system share one property: their origin is wherever their
session happened to start, and their yaw points wherever the user happened to be
facing. None of them can be converted into a Wavr room without a transform
SOLVED from places both systems can see.

The maths lives here rather than in each adapter because a second copy would
drift, and the drift would be a room quietly rotated in one integration and not
the other — the kind of bug that gets attributed to the headset.

## What is solved, and what is deliberately not

**Yaw and translation.** Three numbers, from two or more corresponded points.

**Not scale.** Every system this serves reports metres. Scale is not an unknown,
it is 1, and solving for it would let a single bad correspondence quietly resize
somebody's house into a map that looks entirely reasonable.

**Not pitch or roll.** A floor plan is a plan. A device tilted while its anchors
were captured produces a fit that is worse in a way this module MEASURES and
reports, rather than one it absorbs into a rotation that makes the walls lean.

## Why one correspondence is refused

One point gives translation and leaves rotation completely unknown. An
application handed a one-point alignment would render the room at an arbitrary
angle with no way to tell. "I need one more" is a thirty-second fix; a silently
rotated house is a bug report about somebody else's product.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# How well a solved alignment has to fit before it may be used. A metre is
# generous for a room and far tighter than the error a wrong correspondence
# produces, which is usually the width of the house.
MAX_RESIDUAL_M = 1.0

# The minimum that can determine a rotation. Not a tunable.
MIN_CORRESPONDENCES = 2


class AlignmentError(ValueError):
    """A transform that cannot be solved honestly."""


@dataclass(frozen=True)
class Alignment:
    """How to get from an external frame into a Wavr room.

    Yaw and translation only — see the module docstring on why scale, pitch and
    roll are absent. Each of them would let a bad input produce a
    plausible-looking wrong answer instead of a measurable one.
    """

    room: str
    frame: str                  # what the external side calls its own space
    yaw_deg: float
    dx: float
    dy: float
    # Worst per-point error after the fit, in metres. Published because an
    # alignment is a MEASUREMENT: two points always fit perfectly and say
    # nothing about accuracy, and three that fit to 4 cm say a great deal.
    residual_m: float = 0.0
    samples: int = 0

    @property
    def trustworthy(self) -> bool:
        return (self.samples >= MIN_CORRESPONDENCES
                and self.residual_m <= MAX_RESIDUAL_M)

    def to_dict(self) -> dict:
        return {
            "room": self.room,
            "frame": self.frame,
            "yaw_deg": round(self.yaw_deg, 3),
            "offset_m": [round(self.dx, 4), round(self.dy, 4)],
            "residual_m": round(self.residual_m, 4),
            "samples": self.samples,
            "trustworthy": self.trustworthy,
            "note": ("Solved from places both systems can see, never assumed. "
                     "Scale is fixed at 1: every system this serves reports "
                     "metres, so solving for it could only let a bad "
                     "correspondence resize the room."),
        }


def solve(room: str, correspondences, *, frame: str = "") -> Alignment:
    """Fit yaw and translation from 2D floor-plane correspondences.

    `correspondences` is a sequence of `((ext_x, ext_y), (room_x, room_y))`, both
    in metres. Callers convert their own axes onto the floor plane before
    calling — that conversion is vendor-specific and lives with the vendor.
    """
    pairs = []
    for item in correspondences or ():
        try:
            (ext, room_pt) = item
            pairs.append(((float(ext[0]), float(ext[1])),
                          (float(room_pt[0]), float(room_pt[1]))))
        except (TypeError, ValueError, IndexError) as exc:
            raise AlignmentError(f"malformed correspondence: {item!r}") from exc

    if len(pairs) < MIN_CORRESPONDENCES:
        raise AlignmentError(
            f"need at least {MIN_CORRESPONDENCES} corresponded places to solve "
            f"yaw; got {len(pairs)}. With one, the rotation is unknown and the "
            f"room would be rendered at an arbitrary angle.")

    # Rigid 2D fit with scale pinned to 1 — Umeyama without the scale term.
    n = len(pairs)
    mx = sum(p[0][0] for p in pairs) / n
    my = sum(p[0][1] for p in pairs) / n
    rx = sum(p[1][0] for p in pairs) / n
    ry = sum(p[1][1] for p in pairs) / n

    num = den = 0.0
    for (ax, ay), (bx, by) in pairs:
        ax, ay = ax - mx, ay - my
        bx, by = bx - rx, by - ry
        num += ax * by - ay * bx
        den += ax * bx + ay * by
    if abs(num) < 1e-12 and abs(den) < 1e-12:
        # Every point sits on the same spot, so there is no baseline to rotate
        # about. Two correspondences at one place is one correspondence.
        raise AlignmentError(
            "the corresponded places are all in the same spot — they give no "
            "baseline, so yaw cannot be solved")
    theta = math.atan2(num, den)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    dx = rx - (cos_t * mx - sin_t * my)
    dy = ry - (sin_t * mx + cos_t * my)

    residual = 0.0
    for (ax, ay), (bx, by) in pairs:
        px = cos_t * ax - sin_t * ay + dx
        py = sin_t * ax + cos_t * ay + dy
        residual = max(residual, math.hypot(px - bx, py - by))

    return Alignment(room=room, frame=frame, yaw_deg=math.degrees(theta),
                     dx=dx, dy=dy, residual_m=residual, samples=n)


def apply(alignment: Alignment, x: float, y: float) -> tuple[float, float] | None:
    """One floor-plane point placed in a Wavr room, or None when it cannot be.

    `None` for an untrustworthy alignment rather than a best guess — the same
    rule `spatial_frames` applies to a sensor target whose frame cannot be
    resolved. A coordinate without a resolvable frame is not a position, and one
    from a two-metre-residual fit is not a position either.
    """
    if not alignment.trustworthy:
        return None
    cos_t = math.cos(math.radians(alignment.yaw_deg))
    sin_t = math.sin(math.radians(alignment.yaw_deg))
    return (cos_t * x - sin_t * y + alignment.dx,
            sin_t * x + cos_t * y + alignment.dy)
