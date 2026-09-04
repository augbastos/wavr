"""Apple spatial interoperability — the Core side of it.

Everything here runs on Wavr's Core, in Python, on whatever the household owns.
The iOS half is Swift and needs Xcode to compile and a real iPhone to validate;
what Wavr can build and test without either is the CONTRACT — the message shapes,
the rules about what may be stored, and the arithmetic that turns a Nearby
Interaction reading into evidence.

## Nearby Interaction, and the rule that shapes the whole integration

`NIDiscoveryToken` is generated per SESSION and is valid only for that session's
lifetime. It is not a device identity, and treating it as one is the mistake this
module exists to prevent: an integration that stored a token as "this is
Augusto's phone" would be holding a dead reference by the next session, and
between sessions it would confidently attribute a stranger's phone to Augusto if
a token were ever reused.

So a token is accepted for the duration of a session, mapped onto a device Wavr
ALREADY knows through its own pairing, and never persisted.

## What a UWB reading actually is

Distance in metres, and — only sometimes — direction. Direction requires the
devices to be roughly facing each other and the app to be in the foreground; it
is absent far more often than a demo suggests. So `direction` is optional here
and its absence is a first-class case rather than a zero vector.

A distance without a direction is still useful: it says a phone is 2.1 m from a
known anchor, which narrows a room. It does not say WHERE in the room, and this
module will not produce a coordinate from it.

## ARKit anchors

Same shape as OpenXR: a stable per-anchor UUID, and a world origin that is
wherever the session started. So identity is free and coordinates need an
alignment, solved by `spatial_align` — the same code path, because it is the
same problem and two copies would drift.

## What is deliberately absent

No claim of iOS support. Nothing in this repository has run on an iPhone, and
`describe()` says so in the notes rather than in a footnote somewhere else.
"""
from __future__ import annotations

import math
import re

from wavr.contracts import version
from wavr.spatial_align import Alignment, AlignmentError
from wavr.spatial_align import apply as _apply_alignment
from wavr.spatial_align import solve as _solve_alignment

# The contract an iOS client speaks. An iOS client states it, and a mismatch is
# reported rather than guessed at.
APPLE_PROTOCOL_VERSION = version("apple_spatial")

# ARKit and Nearby Interaction both work in metres, and ARKit is right-handed
# with +Y up — the same convention OpenXR uses, so the floor plane is its XZ and
# the conversion below is identical to `openxr.xr_to_floor`. Written as a comment
# rather than a constant: an earlier version had it as a string nothing read, and
# a constant with no consumer is documentation pretending to be code.
#
# A `NIDiscoveryToken` is an opaque Data blob; clients hand it over base64. Length
# is bounded because it reaches a store and a log.
MAX_TOKEN_CHARS = 512
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]{16,%d}$" % MAX_TOKEN_CHARS)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class AppleError(ValueError):
    """A claim from an Apple device that Wavr cannot use honestly."""


def check_token(value: str) -> str:
    """A session's discovery token, shape-checked.

    Returned for use WITHIN the session and never for storage. See the module
    docstring: this identifies a session, not a device, and the difference is
    the whole integration.
    """
    text = str(value or "").strip()
    if not _B64_RE.match(text):
        raise AppleError("a discovery token must be base64 and 16-512 characters")
    return text


def check_anchor_id(value: str) -> str:
    text = str(value or "").strip()
    if not _UUID_RE.match(text):
        raise AppleError(f"not an ARAnchor identifier: {value!r}")
    return text.lower()


def provider_id(bundle_id: str) -> str:
    """The namespace an ARKit anchor UUID belongs to.

    Per application, because an `ARWorldMap` and the anchors in it belong to the
    app that built them. The same UUID from two apps is two different places,
    and storing both under a bare "arkit" would make them collide — the same
    trap `openxr.provider_id` exists to close.
    """
    app = " ".join(str(bundle_id or "").split())
    if not app:
        raise AppleError(
            "an ARKit anchor mapping must name the app bundle it came from — "
            "anchor UUIDs are namespaced per application")
    return f"arkit:app:{app}"


def parse_nearby_object(payload: dict) -> dict:
    """One `NINearbyObject` as an iOS client reports it.

    Untrusted: it arrives over the LAN from an app Wavr did not write.

    `direction` is optional and its ABSENCE is preserved rather than filled in.
    Nearby Interaction only produces a direction when the devices are roughly
    facing each other with the app in the foreground, which is much rarer than a
    demo video implies. A zero vector would be indistinguishable from "directly
    ahead".
    """
    if not isinstance(payload, dict):
        raise AppleError("a nearby object must be an object")
    token = check_token(payload.get("discovery_token"))

    distance = payload.get("distance_m")
    if distance is not None:
        try:
            distance = float(distance)
        except (TypeError, ValueError) as exc:
            raise AppleError("distance_m must be a number of metres") from exc
        if not (0.0 <= distance <= 100.0) or distance != distance:
            # Nearby Interaction works to about nine metres; a hundred is a
            # generous bound that still rejects a units mistake or a garbage
            # float, and it is the same class of check anchors apply.
            raise AppleError(f"distance_m out of range: {distance}")

    direction = payload.get("direction")
    if direction is not None:
        try:
            vx, vy, vz = (float(v) for v in direction)
        except (TypeError, ValueError) as exc:
            raise AppleError("direction must be [x, y, z]") from exc
        norm = math.sqrt(vx * vx + vy * vy + vz * vz)
        if norm < 1e-6:
            # A zero vector is not a direction. Refused rather than normalised,
            # because normalising it would invent one.
            raise AppleError("direction has no magnitude, so it is not a direction")
        direction = (vx / norm, vy / norm, vz / norm)

    if distance is None and direction is None:
        raise AppleError("a nearby object with neither distance nor direction "
                         "says nothing")
    return {
        "discovery_token": token,
        "distance_m": distance,
        "direction": direction,
        "has_direction": direction is not None,
        # Stated so a consumer does not have to infer it from a missing field.
        "note": ("A discovery token identifies a SESSION, not a device. Direction "
                 "is often absent — Nearby Interaction only produces one when "
                 "the devices roughly face each other with the app in front."),
    }


def offset_from(reading: dict) -> tuple[float, float, float] | None:
    """The peer's offset from the measuring device, or None.

    `None` whenever direction is missing, which is most of the time. A distance
    alone constrains the peer to a SPHERE, and collapsing that to a point — by
    assuming "straight ahead", or by picking the room centre — would be inventing
    a position, which is the one thing this codebase will not do.

    The caller still has the distance, and a distance is genuinely useful: it
    tells you which anchor somebody is near.
    """
    if not reading.get("has_direction") or reading.get("distance_m") is None:
        return None
    dx, dy, dz = reading["direction"]
    d = reading["distance_m"]
    # ARKit axes: the floor plane is XZ, +Y is up. Same as OpenXR.
    return (dx * d, dz * d, dy * d)


def solve_alignment(room: str, correspondences) -> Alignment:
    """Fit an ARKit world frame onto a Wavr room.

    `correspondences` is `((ar_x, ar_y, ar_z), (room_x, room_y))`. Delegates to
    the shared solver: ARKit's problem is OpenXR's problem, and giving it its own
    implementation would produce two subtly different answers to one question.
    """
    flat = []
    for item in correspondences or ():
        try:
            (ar, room_pt) = item
            flat.append(((float(ar[0]), float(ar[2])), room_pt))
        except (TypeError, ValueError, IndexError) as exc:
            raise AppleError(f"malformed correspondence: {item!r}") from exc
    try:
        return _solve_alignment(room, flat, frame="arkit-world")
    except AlignmentError as exc:
        raise AppleError(str(exc)) from exc


def apply_alignment(alignment: Alignment, x: float, y: float, z: float
                    ) -> tuple[float, float, float] | None:
    placed = _apply_alignment(alignment, float(x), float(z))
    return None if placed is None else (placed[0], placed[1], float(y))


def describe():
    """The provider declaration for an Apple device.

    `precision_ceiling: "room"`. A UWB range is accurate to centimetres and that
    is genuinely impressive, but a range is a sphere — without a direction, and
    without an alignment, it places a phone in a ROOM and no more. Declaring
    `position` would promise the rare best case as though it were normal.
    """
    from wavr.providers import CONF_QUALITY, KIND_SPATIAL, REACH_LAN, describe as _d
    return _d(
        "apple_spatial", "Apple device (UWB / ARKit)", KIND_SPATIAL, REACH_LAN,
        observes=("range", "anchor"),
        precision_ceiling="room",
        # Apple reports a distance with an accuracy characteristic, not a
        # probability that somebody is present. Calling it a probability would
        # let fusion weigh it as one.
        confidence_semantics=CONF_QUALITY,
        coordinate_frame="sensor",
        requires=("an iOS app that shares its readings with this Core",),
        notes=("Contract and Core-side arithmetic only. Nothing in this "
               "repository has run on an iPhone — the iOS half needs Xcode to "
               "build and a device to validate."))
