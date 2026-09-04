"""OpenXR interoperability: what a headset and Wavr can honestly say to each
other.

## The two halves, and why only one of them is free

**Identity is free.** OpenXR persists a spatial entity as an `XrUuid`
(`XR_EXT_spatial_persistence`), stable across sessions and device reboots. A
headset that recognises its own anchor can ask Wavr what that place is CALLED and
what Wavr knows about the room around it — no geometry involved, no alignment
required, works on the first run. That is most of the value and it costs nothing
but a lookup.

**Coordinates are not free, and pretending otherwise is the failure mode.**
OpenXR's `LOCAL` reference space has its origin wherever the session started and
its yaw pointing wherever the user happened to face. There is NO fixed transform
from OpenXR coordinates into a Wavr room, and any code that hard-codes one is
producing plausible numbers about somebody's home with nothing behind them.

So a transform must be SOLVED, from anchors both systems can see:

    one corresponded anchor   -> translation only; yaw is unknown
    two or more               -> translation and yaw
    zero                      -> nothing, and Wavr says so

## What is never solved

**Scale.** OpenXR distances are metres by specification and so are Wavr's, so a
scale factor is not an unknown — it is 1. Solving for it would let a single bad
correspondence quietly resize somebody's house, and the resulting map would look
fine.

**Pitch and roll.** A floor plan is a plan. A headset tilted while its anchors
were captured produces a fit that is worse in a way this module can measure and
report, rather than one it absorbs into a rotation that makes the walls lean.

## Axis conventions, which do not match and must not be assumed

    OpenXR   right-handed, metres, +X right, +Y UP,   -Z forward
    Wavr     metres, +X right, +Y DOWN on the floor plan, +Z up

So the floor plane is OpenXR's XZ and Wavr's XY, and Wavr's `y` runs opposite to
OpenXR's `-z` handedness on that plane. The conversion below is the only place
that knowledge lives.

## The scoping rule that stops UUIDs from colliding in meaning

`XR_SPATIAL_PERSISTENCE_SCOPE_LOCAL_ANCHORS_EXT` persists an anchor for the same
device, the same user AND the same application. A UUID from one app is not the
same place as the identical UUID from another — it is simply a different
namespace. So a mapping stored in Wavr must carry the scope it came from, or two
headsets will eventually agree on a UUID and disagree about where it is.

Only `SYSTEM_MANAGED` entities are shared across applications on a device, and
even those are per-device.
"""
from __future__ import annotations

import re

from wavr.spatial_align import Alignment, AlignmentError
from wavr.spatial_align import apply as _apply_alignment
from wavr.spatial_align import solve as _solve_alignment

# Reference spaces an application may be working in. Named here so a payload can
# state which one it means; Wavr treats them all the same way, because the
# problem is identical for each — the origin is not Wavr's.
SPACE_LOCAL = "LOCAL"           # world-locked, origin where the session started
SPACE_STAGE = "STAGE"           # a defined play area, origin at its centre
SPACE_UNBOUNDED = "UNBOUNDED"   # large-scale tracking, runtime-defined origin
SPACE_VIEW = "VIEW"             # the headset itself — never a place

REFERENCE_SPACES: frozenset[str] = frozenset({
    SPACE_LOCAL, SPACE_STAGE, SPACE_UNBOUNDED, SPACE_VIEW})

# Persistence scopes, from XR_EXT_spatial_persistence.
SCOPE_SYSTEM_MANAGED = "system_managed"   # read-only, shared across apps, per device
SCOPE_LOCAL_ANCHORS = "local_anchors"     # read/write, per device + user + APP

PERSISTENCE_SCOPES: frozenset[str] = frozenset({
    SCOPE_SYSTEM_MANAGED, SCOPE_LOCAL_ANCHORS})

# What the runtime says about a UUID it was asked to load.
STATE_LOADED = "loaded"
STATE_NOT_FOUND = "not_found"

# `XrUuid` is 16 bytes; runtimes hand it over as the usual 8-4-4-4-12 hex form.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class OpenXRError(ValueError):
    """A claim from an XR runtime that Wavr cannot use honestly."""


def provider_id(scope: str, application: str = "") -> str:
    """The `provider_id` a mapping from this runtime must be stored under.

    Encodes the SCOPE, and for `local_anchors` the application too, because that
    scope's UUIDs are namespaced per application: the same UUID from two apps
    names two different places. Storing both under a bare "openxr" would make
    them collide, and the collision would look like an anchor that had moved.
    """
    if scope not in PERSISTENCE_SCOPES:
        raise OpenXRError(f"scope must be one of {sorted(PERSISTENCE_SCOPES)}")
    if scope == SCOPE_SYSTEM_MANAGED:
        return "openxr:system"
    app = " ".join(str(application or "").split())
    if not app:
        raise OpenXRError(
            "a local_anchors mapping must name the application it came from — "
            "that scope's UUIDs mean different places in different apps")
    return f"openxr:app:{app}"


def check_uuid(value: str) -> str:
    """A normalised XrUuid, or a refusal.

    Refused rather than accepted-and-stored: an id that is not a UUID did not
    come from the extension this integration is built on, and letting it through
    means the eventual failure surfaces as "anchor not found" somewhere much
    further away.
    """
    text = str(value or "").strip()
    if not _UUID_RE.match(text):
        raise OpenXRError(f"not an XrUuid: {value!r}")
    return text.lower()


def xr_to_floor(x: float, y: float, z: float) -> tuple[float, float, float]:
    """One OpenXR point into Wavr's floor-plan axes, before any alignment.

    OpenXR is +Y up and -Z forward; a Wavr floor plan is +Y down the page with
    +Z as height. So the floor plane is OpenXR's XZ, and OpenXR's `z` maps
    straight onto Wavr's `y` — the sign works out because OpenXR's forward is
    NEGATIVE z, which is the direction a plan's +y runs when looking down at it.

    Axes only. It does NOT place the point in a room: without an alignment the
    origin is still wherever the headset's session began.
    """
    return (float(x), float(z), float(y))


def solve_alignment(room: str, correspondences, *,
                    reference_space: str = SPACE_LOCAL) -> Alignment:
    """Fit yaw and translation from anchors both systems can see.

    `correspondences` is a sequence of `((xr_x, xr_y, xr_z), (room_x, room_y))`.
    The maths is `spatial_align.solve`, shared with every other vendor that has
    the same problem; what belongs here is the axis conversion and the
    reference-space rules, which are OpenXR's alone.
    """
    if reference_space not in REFERENCE_SPACES:
        raise OpenXRError(f"unknown reference space {reference_space!r}")
    if reference_space == SPACE_VIEW:
        raise OpenXRError("VIEW is the headset, not a place — it moves with the "
                          "user, so nothing can be aligned to it")
    flat = []
    for item in correspondences or ():
        try:
            (xr, room_pt) = item
            fx, fy, _ = xr_to_floor(*xr)
            flat.append(((fx, fy), room_pt))
        except (TypeError, ValueError, IndexError) as exc:
            raise OpenXRError(f"malformed correspondence: {item!r}") from exc
    try:
        return _solve_alignment(room, flat, frame=reference_space)
    except AlignmentError as exc:
        raise OpenXRError(str(exc)) from exc


def apply_alignment(alignment: Alignment, x: float, y: float, z: float
                    ) -> tuple[float, float, float] | None:
    """One XR point placed in a Wavr room, or None when it cannot be."""
    fx, fy, fz = xr_to_floor(x, y, z)
    placed = _apply_alignment(alignment, fx, fy)
    return None if placed is None else (placed[0], placed[1], fz)


def parse_entity(payload: dict) -> dict:
    """One spatial entity as an XR runtime reports it, checked.

    Untrusted input: it arrives over the network from an application Wavr did not
    write. Every field is validated, and the whole thing is refused rather than
    partially accepted — a half-read entity would be stored with a UUID and no
    scope, which is precisely the state that makes two runtimes' anchors collide.
    """
    if not isinstance(payload, dict):
        raise OpenXRError("a spatial entity must be an object")
    uuid = check_uuid(payload.get("uuid"))
    scope = str(payload.get("scope") or "").strip().lower()
    if scope not in PERSISTENCE_SCOPES:
        raise OpenXRError(f"scope must be one of {sorted(PERSISTENCE_SCOPES)}")
    state = str(payload.get("state") or STATE_LOADED).strip().lower()
    if state not in (STATE_LOADED, STATE_NOT_FOUND):
        raise OpenXRError("state must be 'loaded' or 'not_found'")
    space = str(payload.get("reference_space") or SPACE_LOCAL).strip().upper()
    if space not in REFERENCE_SPACES:
        raise OpenXRError(f"unknown reference space {space!r}")
    return {
        "uuid": uuid,
        "scope": scope,
        "state": state,
        "reference_space": space,
        "application": " ".join(str(payload.get("application") or "").split()),
        "provider_id": provider_id(scope, payload.get("application") or ""),
        # A runtime's own transient handle. Recorded when offered and NEVER
        # stored as an identity: `XrSpatialEntityIdEXT` is valid only inside one
        # spatial context, so persisting it would create a reference that is
        # dead by the next session and looks alive.
        "entity_id": str(payload.get("entity_id") or "")[:64],
    }


def describe():
    """The provider declaration for an XR runtime.

    `precision_ceiling: "room"` and NOT `position`, which is the honest answer
    and probably a surprising one. A headset knows where it is to centimetres —
    in ITS OWN frame. What it can tell WAVR depends entirely on whether an
    alignment has been solved, and a descriptor is a static claim that cannot
    depend on runtime state. Declaring `position` would promise the best case as
    if it were the normal one.
    """
    from wavr.providers import CONF_NONE, KIND_SPATIAL, REACH_LAN, describe as _d
    return _d(
        "openxr", "OpenXR headset", KIND_SPATIAL, REACH_LAN,
        observes=("anchor", "pose"),
        precision_ceiling="room",
        confidence_semantics=CONF_NONE,
        coordinate_frame="sensor",
        requires=("an application that shares its anchor UUIDs",),
        notes=("Recognises places by anchor UUID. Coordinates need an alignment "
               "solved from at least two anchors both systems can see."))
