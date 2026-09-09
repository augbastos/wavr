"""What an application is allowed to know about where it is running.

## The problem an application actually has

An app that wants to behave differently in the kitchen than in the hall cannot
use Wavr's internals. It does not know what a modality is, it should not learn
what `RESOLUTION_SCOPE` means, and it must not have to decide for itself whether
a Wi-Fi count is good enough evidence to dim the lights. It needs one answer:

    here is the room, here is how well Wavr can see it, here is what is in it,
    and here is what Wavr CANNOT tell you.

That last clause is the one everything else in this module is arranged around.

## Limitations are a field, not a footnote

Every context carries `limitations`: plain sentences naming what this room, right
now, cannot answer. They are DERIVED from real state — an offline sensor, a
missing count capability, a radar-only room — never written by hand, so they
cannot go stale the way a documented caveat does.

Without them, an application infers capability from absence, and absence is
ambiguous in exactly the wrong direction. `person_count: null` could mean "nobody
is counting" or "nobody is here", and an app that guesses will guess "empty" —
because empty is the value that makes its code simpler. So Wavr says it outright.

This also makes something else true for the first time: fusion documents an
honest cost — a very still person vanishes from a radar, so a radar-only room
keeps a bounded phantom — in a module docstring, where no application can read
it. Here it becomes a sentence in an API response.

## What a context deliberately does not contain

**Nobody's name, and nobody's position.** Occupancy is a count or `null`.
Identities never appear, whatever the caller's credential says, because an
experience that can name the people in a room is a different product with a
different consent conversation attached. Anchors describe furniture; targets
describe people, and only the dashboard's own authenticated stream carries those.

**No fabricated zero.** `occupancy: null` means Wavr cannot count here. It is
never rendered as 0, which is the single most tempting lie in this whole system.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from wavr.contracts import version as _contract_version
from wavr.fusion import RESOLUTION_SCOPE, TRUSTED_ABSENCE_MODALITIES
from wavr.spatial_uncertainty import assess as assess_uncertainty

# What an application can ask Wavr for in a given room. Named for what an app
# WANTS, not for the technology behind it: an app asks for `count`, and whether
# that comes from a camera or a radar is Wavr's problem.
CAP_PRESENCE = "presence"      # is anybody here
CAP_COUNT = "count"            # how many
CAP_POSITION = "position"      # whereabouts in the room
CAP_ANCHORS = "anchors"        # named places exist here
CAP_DISPLAY = "display"        # a screen is available in this room
CAP_AUDIO = "audio"            # something here can play or capture sound

SPATIAL_CAPABILITIES: frozenset[str] = frozenset({
    CAP_PRESENCE, CAP_COUNT, CAP_POSITION, CAP_ANCHORS, CAP_DISPLAY, CAP_AUDIO})

# Coverage health values that mean a sensor is contributing right now. Imported
# by value rather than by reference to `sensor_coverage` so this module can be
# tested with plain dicts, which is what its callers hand it anyway.
_HEALTH_OK = "ok"


@dataclass(frozen=True)
class ExperienceContext:
    """One room, as an application sees it."""

    space_id: str = ""
    space_name: str = ""
    room: str = ""
    # Where this room sits on fusion's precision ladder right now.
    precision: str = "none"
    confidence: float = 0.0
    occupied: bool | None = None
    # None means "nobody is counting here", never zero. See the module docstring.
    occupancy: int | None = None
    capabilities: tuple[str, ...] = ()
    anchors: tuple[dict, ...] = ()
    devices: tuple[dict, ...] = ()
    limitations: tuple[str, ...] = ()
    sensors: tuple[dict, ...] = ()
    # How wrong this room's answer might be, and whether that was MEASURED.
    # Distinct from `confidence`, which is about presence, and from `precision`,
    # which bounds granularity and says nothing about how often it is right.
    uncertainty: dict = field(default_factory=dict)
    protocol_version: int = field(
        default_factory=lambda: _contract_version("experience_context"))

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "space": {"space_id": self.space_id, "name": self.space_name},
            "room": self.room,
            "precision": self.precision,
            "confidence": round(float(self.confidence), 3),
            "occupied": self.occupied,
            "occupancy": self.occupancy,
            "occupancy_known": self.occupancy is not None,
            "capabilities": list(self.capabilities),
            "anchors": [dict(a) for a in self.anchors],
            "devices": [dict(d) for d in self.devices],
            "sensors": [dict(s) for s in self.sensors],
            "uncertainty": dict(self.uncertainty),
            "limitations": list(self.limitations),
            "note": ("`limitations` says what this room cannot answer right now. "
                     "An absent value is not a negative one — `occupancy: null` "
                     "means nobody is counting here, never that the room is "
                     "empty."),
        }


# Everything a caller sees regardless of what it was granted. Presence is the
# floor: an application that cannot tell whether anybody is in the room has
# nothing to run on, and the honest way to express "this experience gets
# nothing" is to unpair the device.
#
# Adding a key here is the decision that it is safe for the LEAST trusted paired
# caller. That is a deliberately uncomfortable sentence to have to agree with,
# which is the point.
_ALWAYS_VISIBLE = (
    "protocol_version", "space", "room",
    "precision", "confidence", "occupied",
    "capabilities", "limitations", "uncertainty", "note",
)

# The one limitation sentence that enumerates sensors. Matched on its opening
# clause rather than rebuilt, because rebuilding would need the coverage rows
# and `redact` deliberately runs on the SERIALISED body — one place to read when
# somebody asks what an experience can see.
_NAMES_SENSORS = "Not everything here is reporting"


def _limitations_without_names(body: dict) -> list[str]:
    """The same warning, minus the inventory.

    "Not everything here is reporting: mmwave 1, camera 2" tells an application
    its answer rests on fewer sensors than it should — which it needs — and also
    tells it how many sensors exist and which are dark, which it was not
    granted. The count keeps the first and drops the second.
    """
    out = []
    for line in body.get("limitations") or []:
        if isinstance(line, str) and line.startswith(_NAMES_SENSORS):
            quiet = len(body.get("sensors") or ()) and line.count(",") + 1
            out.append(
                f"Not everything in this room is reporting"
                + (f" ({quiet} sensor{'s' if quiet != 1 else ''} quiet)"
                   if quiet else "")
                + ". What Wavr says here rests on fewer sensors than it should.")
        else:
            out.append(line)
    return out


def redact(body: dict, scopes) -> dict:
    """One context, reduced to what this caller was granted.

    Applied to the SERIALISED context rather than to the builder, so there is
    exactly one place to read when somebody asks what an experience can see —
    and so a field added to the context later is absent from a restricted view
    by default rather than present until somebody remembers.

    `None` means unscoped: loopback root, the Core's own screens, the developer
    tools and the MCP surface. Those already hold every authority in the
    product, and a second weaker gate in front of them would only be a second
    thing to keep correct.

    Presence is never removed. An application that cannot tell whether anybody
    is in the room has nothing to run on, and the honest way to express "this
    experience gets nothing" is to unpair the device.
    """
    from wavr.experience_manifest import (
        SCOPE_ANCHORS, SCOPE_COUNT, SCOPE_DEVICES, SCOPE_POSITION,
    )
    if scopes is None:
        return body
    allowed = set(scopes)

    # An ALLOWLIST, and it has to be. This was `dict(body)` minus four known
    # keys, which is a blocklist however the docstring above described it -- and
    # a blocklist leaks by default the day somebody adds a field. That day
    # arrived: `sensors` was added to the context and to nothing here, so every
    # caller holding the minimum credential received the room's full sensing
    # inventory, live health included, while `withheld` told it the response was
    # narrowed.
    #
    # Unknown keys are DROPPED rather than passed. The cost of that is a field
    # that goes missing until somebody adds it below; the cost of the other
    # choice is a field that leaks until somebody notices. Only one of those two
    # gets noticed by the person it affects.
    out = {k: body[k] for k in _ALWAYS_VISIBLE if k in body}
    removed: list[str] = []

    if SCOPE_COUNT in allowed:
        out["occupancy"] = body.get("occupancy")
        out["occupancy_known"] = body.get("occupancy_known", False)
    else:
        # `None`, not absent — an application reading `occupancy` gets the same
        # "nobody is counting here" it gets from a room with no counting sensor,
        # which is a case it already had to handle.
        out["occupancy"] = None
        out["occupancy_known"] = False
        removed.append("count")
    if SCOPE_POSITION not in allowed:
        removed.append("position")

    out["anchors"] = body.get("anchors", []) if SCOPE_ANCHORS in allowed else []
    if SCOPE_ANCHORS not in allowed:
        removed.append("anchors")

    # `sensors` travels with `devices`: both answer "what hardware is in this
    # room", and one of them was gated while the other, on the adjacent line of
    # the same `to_dict`, was not. A caller without the scope should not be able
    # to enumerate a household's sensors and see which are currently dark.
    if SCOPE_DEVICES in allowed:
        out["devices"] = body.get("devices", [])
        out["sensors"] = body.get("sensors", [])
    else:
        out["devices"] = []
        out["sensors"] = []
        removed.append("devices")
        # `limitations` stays — an application must know its answer is weak —
        # but the sentence that NAMES the quiet sensors is the inventory again,
        # in prose. Replaced with the count, which carries the warning without
        # the enumeration.
        out["limitations"] = _limitations_without_names(body)

    if removed:
        out["capabilities"] = [c for c in out.get("capabilities", [])
                               if c not in set(removed)]
        # Said out loud. An application that cannot see a headcount should know
        # whether the room cannot produce one or whether it was not granted it —
        # those need completely different responses, and a silent removal makes
        # them identical.
        out["withheld"] = removed
        out["note"] = (out.get("note", "") + " Some of this room's answers are "
                       "not in this response because this experience was not "
                       "granted them: " + ", ".join(removed) + ".")
    return out


# A live source's freshness vocabulary is not the coverage census's. `fresh`
# and `stale` both still carry weight in fusion; `dead` carries none, and
# neither do the error labels.
_LIVE_OK = frozenset({"fresh", "stale"})


def _capability_from(level: str, caps: set) -> None:
    if level in ("room", "count", "position"):
        caps.add(CAP_PRESENCE)
    if level in ("count", "position"):
        caps.add(CAP_COUNT)
    if level == "position":
        caps.add(CAP_POSITION)


def _room_capabilities(coverage_rows, anchors, devices,
                       room_state=None) -> tuple[str, ...]:
    """What this room can currently support, from what is actually working.

    Built from sensors that are OBSERVING, not from sensors that exist. A camera
    that is switched off makes the room watchable, not watched, and an
    application told it has `count` in a room whose only counting sensor is dark
    will show a headcount that never changes.

    ## Two censuses, and why one of them was not enough

    `coverage_rows` is a HARDWARE inventory — the cameras and nodes this Core
    owns. It is silent about every other way evidence arrives: Home Assistant
    motion sensors, an external provider posting to
    `POST /api/providers/{id}/observations`, the simulator.

    Reading only that census made one response contradict itself. It carried
    `occupied: true, confidence: 0.665, precision: "room"` beside
    `capabilities: []`, `sensors: []` and "No sensor covers sala. Wavr cannot
    tell whether anybody is here." Every SDK's documented guard —
    `room.can("presence")` — was therefore false for a room Wavr was actively
    sensing, so a correctly written application refused to act on it, and an
    agent reading the MCP coverage tool reported that nothing was watching a
    kitchen Home Assistant was watching.

    So the live `room_state` is unioned in: its `sources[]` are what is feeding
    fusion right now, whatever the hardware census knows about them. A
    capability claimed here still means OBSERVED — a `dead` source contributes
    nothing here, exactly as it contributes nothing to the confidence.
    """
    caps: set[str] = set()
    for row in coverage_rows or []:
        if row.get("health") != _HEALTH_OK:
            continue
        _capability_from(
            row.get("precision_level")
            or RESOLUTION_SCOPE.get(row.get("modality", ""), "house"), caps)
    for src in (room_state or {}).get("sources") or []:
        if src.get("health") not in _LIVE_OK:
            continue
        _capability_from(
            src.get("precision_level")
            or RESOLUTION_SCOPE.get(src.get("modality", ""), "house"), caps)
    if anchors:
        caps.add(CAP_ANCHORS)
    for d in devices or []:
        if d.get("display") is True:
            caps.add(CAP_DISPLAY)
        if d.get("audio") is True:
            caps.add(CAP_AUDIO)
    return tuple(sorted(caps))


def _limitations(room, room_state, coverage_rows, capabilities) -> tuple[str, ...]:
    """The honest sentences, derived from state rather than written down.

    Ordered worst-first: an application that renders only the first one should
    get the one that most changes what it can do.
    """
    out: list[str] = []
    rows = list(coverage_rows or [])
    working = [r for r in rows if r.get("health") == _HEALTH_OK]
    # Evidence arriving from somewhere the hardware census cannot see: Home
    # Assistant, an external provider, the simulator. Its presence is what
    # makes "no sensor covers this room" a false sentence to print beside a
    # confidence, in the same payload, about the same room.
    live = [s for s in ((room_state or {}).get("sources") or [])
            if s.get("health") in _LIVE_OK]

    if not rows and not live:
        out.append(f"No sensor covers {room}. Wavr cannot tell whether anybody "
                   f"is here.")
        return tuple(out)

    if not rows:
        # Sensed, but by nothing this Core owns. Say that, rather than either
        # of the two lies available: "no sensor covers this room" (it is
        # covered) or silence (a person cannot tell where the reading came
        # from, and cannot go and fix it if it stops).
        what = ", ".join(sorted({str(s.get("modality") or "?") for s in live}))
        out.append(f"{room} is sensed by an integration rather than by a "
                   f"sensor Wavr manages ({what}). If it stops reporting, the "
                   f"fix is over there, not here.")
        return tuple(out)

    if not working and not live:
        broken = len(rows)
        out.append(
            f"{'The sensor' if broken == 1 else f'All {broken} sensors'} in "
            f"{room} {'is' if broken == 1 else 'are'} not reporting. Nothing "
            f"here is being observed right now.")
        return tuple(out)

    labels = {id(row): label for row, label, _ in _labelled(rows)}
    silent = [r for r in rows if r.get("health") not in (_HEALTH_OK, "disabled")]
    if silent:
        # The derived label, never the id. See `_labelled`.
        names = ", ".join(sorted(labels.get(id(r), "a sensor") for r in silent))
        out.append(f"Not everything here is reporting: {names}. What Wavr says "
                   f"about {room} rests on fewer sensors than it should.")

    if CAP_COUNT not in capabilities:
        out.append(f"Wavr can tell whether somebody is in {room}, not how many.")
    elif CAP_POSITION not in capabilities:
        out.append(f"Wavr can count people in {room}, not place them within it.")

    # The bounded phantom, documented in fusion and until now readable only
    # there: a very still person disappears from a radar, so a room whose only
    # counting sensor is a radar can hold a stale count for the freshness window.
    counting = [r for r in working
                if (r.get("precision_level") in ("count", "position"))]
    if counting and not any(r.get("modality") in TRUSTED_ABSENCE_MODALITIES
                            for r in counting):
        out.append(
            f"The counting sensors in {room} detect movement, so somebody who "
            f"stays very still can disappear from them. A count here can lag "
            f"reality by up to the freshness window.")

    if room_state and room_state.get("person_count") is None and CAP_COUNT in capabilities:
        out.append(f"Nothing in {room} has produced a headcount yet.")

    return tuple(out)


def _visible_devices(devices, room: str) -> tuple[dict, ...]:
    """Authorized devices in this room, described by what they can DO.

    **No pairing name.** That name is typed by whoever paired the device and is
    very often "Alex's iPhone" -- which is an identity, and the docstring at
    the top of this module says identities never appear here. An earlier version
    returned it anyway, and the contradiction between the promise and the code
    was caught in review rather than by the household it would have leaked to.

    The label is DERIVED instead: what the device can do, where it is, and a
    number when two would otherwise read the same. "living room screen 2" is
    enough for an application to ask "Continue on which?", and it tells an
    experience nothing about whose phone it is.

    No credential, no address, no person link either.
    """
    out = []
    seen: dict[str, int] = {}
    for d in devices or []:
        if str(d.get("room") or "") != room:
            continue
        kind = ("screen" if d.get("display") is True
                else "speaker" if d.get("audio") is True else "device")
        seen[kind] = seen.get(kind, 0) + 1
        label = f"{room} {kind}".strip() if room else kind
        if seen[kind] > 1:
            label = f"{label} {seen[kind]}"
        out.append({
            "device_id": d.get("device_id"),
            "label": label,
            "functions": list(d.get("functions") or ()),
            # Tristate throughout: `None` means the device never told us, which
            # is different from telling us no. An application choosing a screen
            # should be able to tell "no display" from "did not say".
            "display": d.get("display"),
            "audio": d.get("audio"),
            "uwb": d.get("uwb"),
        })
    return tuple(out)


# Provenance a third-party application is entitled to know, without any of the
# text an operator typed. Ordered most-specific first: a simulated HA-derived
# sensor is simulated, and that is the answer that matters.
_SENSOR_SOURCE = (
    ("sim:", "simulated"),
    ("ha:", "home_assistant"),
    ("ext:", "external"),
)


def _labelled(rows):
    """Every row with the ONE name an experience is allowed to hear.

    Shared, because it was not: `_visible_sensors` derived a safe label and
    `_limitations` two hundred lines away joined the RAW ids into a sentence —
    "Not everything here is reporting: office cam, baby monitor." One function
    deriving a label while another prints the thing it was derived to hide is
    not a partial fix, it is no fix; the prose is the more readable of the two
    places for a name to leak.

    Deterministic in the order given, so the same sensor gets the same label in
    the sensor list and in the limitation sentence about it.
    """
    counts: dict[str, int] = {}
    for row in rows:
        raw = str(row.get("sensor_id") or "")
        modality = str(row.get("modality") or "sensor")
        counts[modality] = counts.get(modality, 0) + 1
        source = "wavr"
        for prefix, name in _SENSOR_SOURCE:
            if raw.startswith(prefix):
                source = name
                break
        yield row, f"{modality} {counts[modality]}", source


def _visible_sensors(rows) -> tuple[dict, ...]:
    """The room's sensing hardware, described but not named.

    A `sensor_id` in this codebase is very often the NAME an operator typed:
    `sensor_coverage` uses the camera's name for a camera and the node's name
    for a node. Those read "hall-cam" in a test fixture and "<person>'s office
    cam" in a real house — which is the identity this module's own docstring
    says never appears, in the field directly beside the one an appsec pass
    already had to fix for exactly this reason.

    So the id is derived, the way `_visible_devices` derives a device label: an
    application gets "camera 1" and "mmwave 2", stable within a response and
    enough to correlate two readings, and learns nothing about whose room it is
    or what the household calls its equipment.

    What an application legitimately needs IS kept — modality, health and the
    precision each sensor supports, because "the camera here is offline" is the
    difference between a stale answer and a wrong one.

    `simulated` is a boolean rather than a prefix on a string. The guarantee
    that simulated evidence is never indistinguishable from real evidence is
    only worth something if a client cannot fail to notice it, and a client that
    has to remember to check `startswith("sim:")` can.
    """
    return tuple(
        {"label": label,
         "modality": str(row.get("modality") or "sensor"),
         "health": row.get("health"),
         "precision_level": row.get("precision_level"),
         "source": source,
         "simulated": source == "simulated"}
        for row, label, source in _labelled(rows))


def build_context(*, room: str, space=None, room_state=None, coverage_rows=(),
                  anchors=(), devices=(), profile_fn=None,
                  residual_m: float | None = None,
                  protocol_version: int | None = None) -> ExperienceContext:
    """Assemble one room's context from state the Core already holds.

    Deliberately takes plain data rather than stores: this is the shape three
    different callers need (HTTP, MCP, and the SDKs' own endpoint), and building
    it from injected values is what keeps those three from drifting into three
    slightly different answers.
    """
    rs = dict(room_state or {})
    rows = [dict(r) for r in (coverage_rows or [])
            if str(r.get("room") or "") == room]
    room_anchors = [dict(a) for a in (anchors or [])
                    if str(a.get("room") or "") == room]
    dev = _visible_devices(devices, room)
    # `rs` as well as `rows`: the hardware census does not know about evidence
    # arriving through Home Assistant, an external provider or the simulator,
    # and a capability list built from it alone told every SDK that a room Wavr
    # was actively sensing had no sensing at all.
    caps = _room_capabilities(rows, room_anchors, dev, rs)
    return ExperienceContext(
        space_id=(space or {}).get("space_id", ""),
        space_name=(space or {}).get("name", ""),
        room=room,
        precision=str(rs.get("precision_level") or "none"),
        confidence=float(rs.get("confidence") or 0.0),
        occupied=rs.get("occupied") if "occupied" in rs else None,
        occupancy=rs.get("person_count"),
        capabilities=caps,
        anchors=tuple(room_anchors),
        devices=dev,
        sensors=_visible_sensors(rows),
        limitations=_limitations(room, rs, rows, caps),
        uncertainty=assess_uncertainty(
            room, precision=str(rs.get("precision_level") or "none"),
            coverage_rows=rows, profile_fn=profile_fn,
            residual_m=residual_m).to_dict(),
        protocol_version=(protocol_version
                          if protocol_version is not None
                          else _contract_version("experience_context")))


def build_space_context(*, rooms, space=None, states=None, coverage_rows=(),
                        anchors=(), devices=(), profile_fn=None,
                        protocol_version: int | None = None) -> dict:
    """Every room at once, for an application that has not been told where it is.

    The common case for a first call: an app connects, asks what this Space
    looks like, and then subscribes to the room it cares about.
    """
    states = states or {}
    out = [build_context(room=r, space=space, room_state=states.get(r),
                         coverage_rows=coverage_rows, anchors=anchors,
                         devices=devices, profile_fn=profile_fn,
                         # None is resolved by build_context itself, in one
                         # place, so there is one answer to "what version is
                         # this" rather than two that can drift.
                         protocol_version=protocol_version).to_dict()
           for r in rooms or []]
    return {
        "protocol_version": protocol_version,
        "space": {"space_id": (space or {}).get("space_id", ""),
                  "name": (space or {}).get("name", "")},
        "rooms": out,
        # Rooms nothing can answer for, named once at the top so an application
        # does not have to scan every room to discover the house is half dark.
        "rooms_without_sensing": [c["room"] for c in out
                                  if CAP_PRESENCE not in c["capabilities"]],
        "note": ("Each room reports its own capabilities and its own "
                 "limitations. They differ — a house is not uniformly "
                 "observed, and treating it as one is how an application ends "
                 "up trusting the darkest room as much as the brightest."),
    }
