"""What an experience says it needs, and whether this Space can support it.

## The two documents

An **experience manifest** is what a developer writes:

    id: kitchen-timer
    name: Kitchen Timer
    requires: [presence]
    optional: [count, display]
    scopes:   [room.presence]

A **verdict** is what Wavr answers:

    FULLY_SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED
    plus the reason for each capability that is missing, and what the experience
    can fall back to.

## A manifest is a request, never a grant

This is the security line, and it is worth stating in the module that would be
the natural place to blur it. A manifest says "I would like to know the headcount
in this room". The evaluator answers "this room can produce one". Neither of
those is permission. Whether that experience may actually READ the headcount is a
separate decision, made by a person, recorded in `experience_grants` and applied
by `experience.redact` before any context leaves — and an evaluator returning
FULLY_SUPPORTED must never be mistaken for one.

⚠️ That sentence was true of the DESIGN and false of the code for a while: the
scopes below drove validation warnings and nothing else, so every caller holding
`presence:read` received the full room census whatever its manifest claimed to
need. A stated guarantee nothing implements is worse than no guarantee. The
enforcement is real now, and `test_experience_grants.py` is where it is held to.

The practical consequence: `evaluate()` takes no credential and reads no grant
table. It cannot accidentally authorise anything, because it has nothing to
authorise with.

## Deterministic, and fail-closed on the unknown

No scores, no probabilities, no "83% compatible". The same manifest against the
same Space gives the same verdict every time, which is what makes it something a
developer can test against.

A required capability Wavr does not recognise makes the experience UNSUPPORTED.
Not ignored — Wavr cannot verify a capability it has never heard of, and treating
an unverifiable requirement as satisfied is how an experience gets told it will
work and then does not.

## Deliberately not an app store

No ratings, no ranking, no distribution, no notion of "installed". This evaluates
one document against one Space. Everything an app store adds is a product
decision nobody has made.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from wavr.experience import SPATIAL_CAPABILITIES

# The verdicts. Three, and no fourth: a middle ground that meant "probably" would
# force every developer to invent their own threshold for acting on it.
FULLY_SUPPORTED = "FULLY_SUPPORTED"
PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"

# Spatial permission scopes: what an experience may READ, as opposed to what the
# room can PRODUCE. Separate vocabulary from `auth.SCOPES` on purpose — those
# gate a credential's reach into Wavr's own HTTP surface, these gate a
# third-party experience's reach into spatial facts, and collapsing them would
# mean granting an experience the ability to see a room also granted it whatever
# else that credential tier could reach.
SCOPE_PRESENCE = "room.presence"      # is anybody in this room
SCOPE_COUNT = "room.count"            # how many
SCOPE_POSITION = "room.position"      # whereabouts within the room
SCOPE_ANCHORS = "anchors.read"        # the named places
SCOPE_DEVICES = "devices.read"        # what screens and speakers are here
SCOPE_EVENTS = "events.subscribe"     # the live semantic stream

SPATIAL_SCOPES: frozenset[str] = frozenset({
    SCOPE_PRESENCE, SCOPE_COUNT, SCOPE_POSITION, SCOPE_ANCHORS, SCOPE_DEVICES,
    SCOPE_EVENTS})

# Which scope each capability needs. An experience that requires `count` and asks
# only for `room.presence` has a manifest that contradicts itself, and saying so
# at validation time is much kinder than a 403 in the field.
CAPABILITY_SCOPE: dict[str, str] = {
    "presence": SCOPE_PRESENCE,
    "count": SCOPE_COUNT,
    "position": SCOPE_POSITION,
    "anchors": SCOPE_ANCHORS,
    "display": SCOPE_DEVICES,
    "audio": SCOPE_DEVICES,
}

# There is no scope that grants identity, and there is no capability that offers
# it. Stated as a constant so a future capability cannot be added without
# somebody meeting this line.
NO_IDENTITY_SCOPE = ("An experience can learn that a room is occupied. It can "
                     "never learn who is in it — there is no scope for that, "
                     "and adding one would be a different product.")

MAX_ID = 64
MAX_NAME = 80
MAX_LIST = 32
MAX_TEXT = 400


class ManifestError(ValueError):
    """A manifest that cannot be evaluated honestly."""


@dataclass(frozen=True)
class ExperienceManifest:
    experience_id: str
    name: str
    version: str = "1"
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    # An experience that only makes sense in particular rooms. Empty = anywhere.
    rooms: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict:
        out = {"experience_id": self.experience_id, "name": self.name,
               "version": self.version, "requires": list(self.requires),
               "optional": list(self.optional), "scopes": list(self.scopes)}
        if self.rooms:
            out["rooms"] = list(self.rooms)
        if self.description:
            out["description"] = self.description
        return out


def _slug(value, what: str, limit: int) -> str:
    s = " ".join(str(value or "").split())
    if not s:
        raise ManifestError(f"{what} is required")
    if len(s) > limit:
        raise ManifestError(f"{what} must be {limit} characters or fewer")
    return s


def _string_list(value, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise ManifestError(f"{what} must be a list")
    items = list(value)
    if len(items) > MAX_LIST:
        raise ManifestError(f"{what} may have at most {MAX_LIST} entries")
    out = []
    for v in items:
        if not isinstance(v, str) or not v.strip():
            raise ManifestError(f"every entry in {what} must be a non-empty string")
        s = v.strip()
        if len(s) > MAX_ID:
            raise ManifestError(f"{what} entries must be {MAX_ID} characters or fewer")
        if s not in out:
            out.append(s)
    return tuple(out)


def parse(raw: dict) -> ExperienceManifest:
    """Read an untrusted manifest.

    Untrusted is the operative word: a manifest arrives from a third party, so
    every field is bounded and type-checked before anything looks at it. The
    validation is strict rather than forgiving — a typo in `requires` becomes an
    error at load time instead of a capability silently never checked, which
    would leave a developer debugging a Space that is working perfectly.
    """
    if not isinstance(raw, dict):
        raise ManifestError("a manifest must be an object")
    unknown = set(raw) - {"experience_id", "id", "name", "version", "requires",
                          "optional", "scopes", "rooms", "description"}
    if unknown:
        raise ManifestError(f"unknown manifest fields: {sorted(unknown)}")

    experience_id = _slug(raw.get("experience_id") or raw.get("id"),
                          "experience_id", MAX_ID)
    manifest = ExperienceManifest(
        experience_id=experience_id,
        name=_slug(raw.get("name") or experience_id, "name", MAX_NAME),
        version=_slug(raw.get("version") or "1", "version", 32),
        requires=_string_list(raw.get("requires"), "requires"),
        optional=_string_list(raw.get("optional"), "optional"),
        scopes=_string_list(raw.get("scopes"), "scopes"),
        rooms=_string_list(raw.get("rooms"), "rooms"),
        description=" ".join(str(raw.get("description") or "").split())[:MAX_TEXT])

    bad_scopes = [s for s in manifest.scopes if s not in SPATIAL_SCOPES]
    if bad_scopes:
        raise ManifestError(
            f"unknown scopes {bad_scopes}; Wavr grants only {sorted(SPATIAL_SCOPES)}")
    both = set(manifest.requires) & set(manifest.optional)
    if both:
        # Not a harmless duplicate: one of the two lists is what the developer
        # meant, and guessing which would change whether a Space that lacks it
        # reads as UNSUPPORTED or as PARTIAL.
        raise ManifestError(
            f"{sorted(both)} are listed as both required and optional")
    return manifest


def validate(manifest: ExperienceManifest) -> list[str]:
    """Problems that do not stop evaluation but will bite in the field.

    Returned rather than raised: a manifest asking for a capability without the
    scope to read it is still evaluable, and telling the developer at validation
    time is much kinder than a 403 after they ship.
    """
    warnings: list[str] = []
    for cap in manifest.requires + manifest.optional:
        if cap not in SPATIAL_CAPABILITIES:
            warnings.append(
                f"'{cap}' is not a capability Wavr knows about. Required ones "
                f"make an experience UNSUPPORTED; optional ones can never be "
                f"satisfied. Known: {sorted(SPATIAL_CAPABILITIES)}.")
            continue
        needed = CAPABILITY_SCOPE.get(cap)
        if needed and needed not in manifest.scopes:
            warnings.append(
                f"'{cap}' needs the '{needed}' scope, which this manifest does "
                f"not request. Wavr will report the capability as available and "
                f"then refuse to hand over the data.")
    for scope in manifest.scopes:
        if scope in (SCOPE_PRESENCE, SCOPE_COUNT, SCOPE_POSITION, SCOPE_ANCHORS,
                     SCOPE_DEVICES) and not any(
                CAPABILITY_SCOPE.get(c) == scope
                for c in manifest.requires + manifest.optional):
            warnings.append(
                f"the '{scope}' scope is requested but no capability needs it. "
                f"Asking for more than you use is what makes people refuse the "
                f"whole request.")
    return warnings


@dataclass(frozen=True)
class Verdict:
    status: str
    room: str = ""
    satisfied: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status != UNSUPPORTED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "room": self.room,
            "usable": self.usable,
            "satisfied": list(self.satisfied),
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "reasons": list(self.reasons),
            "fallbacks": list(self.fallbacks),
            "note": ("A verdict says what this room can PRODUCE. It is not "
                     "permission to read it — scopes are, and a person grants "
                     "those."),
        }


def evaluate(manifest: ExperienceManifest, context) -> Verdict:
    """Whether one room can support one experience. Deterministic.

    `context` is an `ExperienceContext` or its dict — both, because the SDKs hand
    back the dict and the in-process callers hold the object, and making one of
    them convert would guarantee the two paths eventually disagree.
    """
    ctx = context.to_dict() if hasattr(context, "to_dict") else dict(context or {})
    room = str(ctx.get("room") or "")
    available = set(ctx.get("capabilities") or ())
    limitations = list(ctx.get("limitations") or ())

    if manifest.rooms and room not in manifest.rooms:
        return Verdict(
            status=UNSUPPORTED, room=room,
            reasons=(f"{manifest.name} is meant for "
                     f"{', '.join(manifest.rooms)}, not {room}.",))

    missing_required, missing_optional, satisfied = [], [], []
    reasons: list[str] = []

    for cap in manifest.requires:
        if cap not in SPATIAL_CAPABILITIES:
            # Fail closed. Wavr cannot verify a capability it has never heard
            # of, and calling an unverifiable requirement satisfied is how an
            # experience gets told it will work and then does not.
            missing_required.append(cap)
            reasons.append(f"'{cap}' is not a capability Wavr can provide or "
                           f"verify, so this cannot be supported here.")
        elif cap in available:
            satisfied.append(cap)
        else:
            missing_required.append(cap)
            reasons.append(_why(cap, room, limitations))

    for cap in manifest.optional:
        if cap in available and cap in SPATIAL_CAPABILITIES:
            satisfied.append(cap)
        else:
            missing_optional.append(cap)

    if missing_required:
        status = UNSUPPORTED
    elif missing_optional:
        status = PARTIALLY_SUPPORTED
    else:
        status = FULLY_SUPPORTED

    return Verdict(
        status=status, room=room,
        satisfied=tuple(satisfied),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
        reasons=tuple(reasons),
        fallbacks=tuple(_fallbacks(missing_optional, room)))


def _why(cap: str, room: str, limitations) -> str:
    """Why one capability is missing, in the room's own words where possible.

    Reuses the context's limitation sentences rather than writing a second set:
    two explanations of the same gap drift, and the developer would find the
    disagreement before we did.
    """
    hint = ""
    for line in limitations:
        if cap == "count" and "not how many" in line:
            hint = line
        elif cap == "position" and "not place them" in line:
            hint = line
        elif cap == "presence" and "cannot tell whether anybody" in line:
            hint = line
    if hint:
        return hint
    return {
        "presence": f"Nothing in {room} can tell whether anybody is there.",
        "count": f"Nothing in {room} can count people.",
        "position": f"Nothing in {room} can place a person within the room.",
        "anchors": f"No anchors have been created in {room}.",
        "display": f"No screen is available in {room}.",
        "audio": f"Nothing in {room} can capture or play audio.",
    }.get(cap, f"'{cap}' is not available in {room}.")


def _fallbacks(missing_optional, room: str) -> list[str]:
    """What an experience can still do without each optional capability.

    Concrete rather than generic. "Degrade gracefully" tells a developer nothing;
    "you have presence but not a count, so show 'someone is here' instead of a
    number" tells them what to write.
    """
    out = []
    for cap in missing_optional:
        out.append({
            "count": (f"No headcount in {room} — show 'someone is here' rather "
                      f"than a number."),
            "position": (f"No position in {room} — treat the whole room as one "
                         f"place."),
            "anchors": (f"No anchors in {room} — refer to the room itself "
                        f"instead of places within it."),
            "display": f"No screen in {room} — fall back to audio or a phone.",
            "audio": f"No audio in {room} — fall back to the screen.",
            "presence": (f"No presence in {room} — the experience cannot react "
                         f"to people, only to time and input."),
        }.get(cap, f"'{cap}' is unavailable in {room}."))
    return out


def evaluate_space(manifest: ExperienceManifest, space_context: dict) -> dict:
    """Every room's verdict at once, plus where the experience works best.

    `best_rooms` is the FULLY_SUPPORTED list rather than a ranking. Ranking would
    require weighing capabilities against each other, and Wavr has no basis for
    saying a screen matters more than a headcount to somebody else's application.
    """
    rooms = space_context.get("rooms") or []
    verdicts = [evaluate(manifest, r).to_dict() for r in rooms]
    full = [v["room"] for v in verdicts if v["status"] == FULLY_SUPPORTED]
    partial = [v["room"] for v in verdicts if v["status"] == PARTIALLY_SUPPORTED]
    return {
        "experience": manifest.to_dict(),
        "verdicts": verdicts,
        "fully_supported": full,
        "partially_supported": partial,
        "supported_anywhere": bool(full or partial),
        "note": ("Rooms are listed, not ranked. Wavr has no basis for saying a "
                 "screen matters more than a headcount to your application."),
    }
