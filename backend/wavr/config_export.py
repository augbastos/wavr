"""Handing somebody their configuration, and a diagnostic bundle, without
handing them a secret.

## Two things a person eventually needs

**Their configuration**, because the Pi dies, or they move house, or they want to
try the Core on a better machine. A Space is hours of work — a floor plan drawn
by hand, rooms named, cameras placed, sensors mapped, anchors created — and
losing it to a failed SD card is the difference between an appliance and a
project.

**A diagnostic bundle**, because when something is wrong the useful thing to send
is what Wavr actually sees, and the alternative is a fortnight of "what does the
dashboard say now?".

Both are the same problem: package up state, and do not leak.

## The rule, and why it is an ALLOWLIST

**A field is exported only if it is named here.** Not "everything except the
secrets" — that is a blocklist, and a blocklist leaks by default the day somebody
adds a column. The first version of a feature like this always remembers the
camera password; the fourth one forgets the new field somebody added last month.

So every ROW exporter below lists its fields explicitly — cameras, nodes,
anchors, HA mappings, providers — and a field nobody thought about is absent.

### Three things travel wholesale, and the reason is not the same for each

An audit flagged these as a blocklist hiding inside an allowlist, and it was
right to look. What each actually rests on:

  * **`settings`** — on the settings STORE being an allowlist of its own. It
    accepts only the keys in `SETTING_SPECS` and raises on anything else, so a
    secret is not storable there at all, whatever this file does.
    `test_config_export` pins that refusal, because this export now depends on
    it: if the store ever starts accepting arbitrary keys, the export leaks and
    the test is where that shows up. `SECRET_SETTINGS` below is a second belt
    for a key the store might one day accept — today it matches none of them,
    which is the correct state for a belt and not evidence it works.
  * **`house`** — a floor plan: room polygons and their names. Geometry an
    operator drew. There is no credential surface in it, and it is exactly what
    somebody rebuilding a Space must not have to draw again.
  * **`topology`** — which rooms connect to which. Same, derived from the above.

If any of those three ever gains a field carrying a credential, it stops
travelling wholesale and gets a `_FIELDS` tuple like everything else.

## What is deliberately never in either file

  * **Camera URLs.** An RTSP URL carries a username and a password, and a camera
    reachable from outside is a camera anybody can watch. The camera's NAME,
    ROOM and CALIBRATION are exported; the URL is not, and the import asks for it
    again.
  * **Any token.** Device tokens, node tokens, peer tokens, the Home Assistant
    token, the local token. A credential in a file people email each other is a
    credential that has been published.
  * **Anything about a person.** Names, device associations, presence history.
    A configuration describes a BUILDING; who lives in it is a different thing
    with a different consent conversation, and it does not travel in a file.
  * **Room state.** A bundle carries what Wavr can SEE, not where anybody was.

## Versioned, and refused rather than guessed

An import states its version. A file from a future Wavr is refused with the
reason, because a field whose meaning changed would be read with the old meaning
and applied silently — and the operator would have no way to tell.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from wavr.contracts import version

# From `contracts`, which owns every published shape's version in one place.
CONFIG_VERSION = version("config_export")
BUNDLE_VERSION = version("diagnostic_bundle")

# The exact fields each kind of row may carry OUT. Default-deny: see the module
# docstring on why this is an allowlist and not a list of things to strip.
CAMERA_FIELDS = ("name", "room", "level", "confidence")
NODE_FIELDS = ("node_id", "label", "room", "sensor_type", "modality", "state",
               "transport")
ANCHOR_FIELDS = ("name", "room", "level", "kind", "x", "y", "z", "note")
HA_MAPPING_FIELDS = ("entity_id", "room", "modality", "enabled", "label")
PROVIDER_FIELDS = ("provider_id", "label", "kind", "reach", "modality",
                   "ceiling", "confidence", "enabled", "notes")

# Settings whose VALUE would be a secret, filtered out by name.
#
# None of these is currently in `settings_store.SETTING_SPECS`, so this list
# matches nothing today — the store refuses to hold any of them, which is the
# real protection. It stays as a second belt for the day somebody adds a key
# like these, and `test_config_export` asserts that the two lists have not
# quietly started to overlap without the value being stripped.
SECRET_SETTINGS = ("ha_token", "mqtt_password", "diag_endpoint", "local_token")

MAX_BUNDLE_ROWS = 500


class TransferError(ValueError):
    """A configuration file Wavr will not read."""


def _pick(row, fields) -> dict:
    """A row reduced to named fields. Absent ones are simply absent."""
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in fields if k in row and row[k] is not None}


def export_config(*, space=None, house=None, cameras=(), nodes=(), anchors=(),
                  ha_mappings=(), providers=(), settings=(), topology=None,
                  now=None) -> dict:
    """Everything needed to rebuild this Space on another machine.

    Takes plain data rather than stores, for the same reason `collect_coverage`
    does: it makes the redaction testable without an app, and there is exactly
    one place to read when somebody asks what leaves.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    secrets_needed = [
        s.get("key") for s in (settings or ())
        if isinstance(s, dict) and s.get("key") in SECRET_SETTINGS
        and s.get("value")]

    return {
        "wavr_config_version": CONFIG_VERSION,
        "exported_at": stamp,
        # The Space's NAME and KIND. Not its `space_id`: importing this into a
        # second Core should create a new Space, not a second Core claiming to
        # be the first one, which is the split-brain `core_registry` exists to
        # detect and would be a strange thing to cause deliberately.
        "space": {"name": (space or {}).get("name", ""),
                  "kind": (space or {}).get("kind", "")},
        "house": house or {},
        "cameras": [_pick(c, CAMERA_FIELDS) for c in cameras or ()],
        "nodes": [_pick(n, NODE_FIELDS) for n in nodes or ()],
        "anchors": [_pick(a, ANCHOR_FIELDS) for a in anchors or ()],
        "ha_mappings": [_pick(m, HA_MAPPING_FIELDS) for m in ha_mappings or ()],
        "external_providers": [_pick(p, PROVIDER_FIELDS) for p in providers or ()],
        "topology": topology or {},
        "settings": {s["key"]: s.get("value") for s in (settings or ())
                     if isinstance(s, dict) and s.get("key")
                     and s["key"] not in SECRET_SETTINGS},
        # Named, never valued. An operator importing this sees what they still
        # have to supply, rather than finding out when a camera stays dark.
        "secrets_you_must_supply_again": sorted(x for x in secrets_needed if x),
        "note": ("No password, token or camera URL is in this file, and no "
                 "person is either. It describes a building. Re-enter the "
                 "camera URLs and any tokens after importing."),
    }


def check_import(payload) -> dict:
    """Read a configuration file, or refuse it with the reason.

    Refuses a FUTURE version rather than reading what it recognises: a field
    whose meaning changed would be applied with the old meaning, silently, and
    the operator would have no way to tell. Refusing costs them an upgrade;
    guessing costs them a house map that is subtly wrong.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise TransferError("that is not a Wavr configuration file") from exc
    if not isinstance(payload, dict):
        raise TransferError("a configuration must be an object")

    # `found`, not `version`: `version` is the imported contracts helper, and a
    # local of the same name shadows it. That exact shadowing has cost this
    # codebase a bug before, in a file where a variable `t` hid a function `t()`.
    found = payload.get("wavr_config_version")
    if found is None:
        raise TransferError(
            "this file does not say which Wavr version wrote it, so Wavr "
            "cannot know how to read it")
    try:
        found = int(found)
    except (TypeError, ValueError) as exc:
        raise TransferError("the version must be a number") from exc
    if found > CONFIG_VERSION:
        raise TransferError(
            f"this file was written by a newer Wavr (format {found}; this "
            f"Core reads {CONFIG_VERSION}). Update this Core rather than "
            f"importing part of it.")

    # A file that HAS travelled through somebody's inbox may have picked up
    # anything. Every list is checked for shape before a caller walks it.
    for key in ("cameras", "nodes", "anchors", "ha_mappings",
                "external_providers"):
        rows = payload.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise TransferError(f"{key} must be a list")
        if len(rows) > MAX_BUNDLE_ROWS:
            raise TransferError(f"{key} has more than {MAX_BUNDLE_ROWS} entries")
    return payload


def preview_import(payload, *, existing_rooms=(), existing_anchors=(),
                   validate_house=None) -> dict:
    """What importing this file would change, before anything is written.

    Shown rather than applied, because an import is destructive in a way an
    operator cannot easily reverse: a floor plan is hours of work, and "it
    replaced my rooms" is the complaint this avoids.
    """
    config = check_import(payload)
    house = config.get("house") or {}
    if validate_house is not None and house.get("floors"):
        try:
            validate_house(house)
        except Exception as exc:            # noqa: BLE001
            raise TransferError(
                f"the floor plan in this file cannot be read: {exc}") from None
    incoming_rooms = [r.get("name") for f in house.get("floors", [])
                      for r in f.get("rooms", []) if r.get("name")]
    known = set(existing_rooms or ())
    anchors = config.get("anchors") or []
    return {
        # Returned so the apply can refuse a different file than the one whose
        # consequences were shown. See `config_digest`.
        "digest": config_digest(config),
        "space": config.get("space", {}),
        "rooms_incoming": incoming_rooms,
        "rooms_replaced": sorted(known & set(incoming_rooms)),
        "rooms_lost": sorted(known - set(incoming_rooms)),
        "anchors_incoming": len(anchors),
        "anchors_existing": len(list(existing_anchors or ())),
        "cameras_incoming": len(config.get("cameras") or []),
        "secrets_needed": config.get("secrets_you_must_supply_again", []),
        "note": ("Nothing has been written. Importing REPLACES the floor plan; "
                 "anything under `rooms_lost` exists here and not in the file."),
    }


def config_digest(payload) -> str:
    """A stable fingerprint of the configuration a person was shown.

    An import is previewed and then applied, and those are two requests. Without
    this, an operator can be shown the consequences of file A and apply file B —
    by accident, with two browser tabs, or because a script rebuilt the file
    between the two calls. The apply refuses unless the digest matches what the
    preview returned.

    Not a security control: anybody who can call apply can call preview first.
    It defends against a mistake, which is the failure that actually happens
    here, and it costs one hash.
    """
    canonical = json.dumps(check_import(payload), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def plan_import(payload, *, existing_rooms=(), existing_anchors=(),
                validate_house=None) -> dict:
    """The exact operations an import would perform, decided before any writing.

    Separate from `preview_import`, which answers "what would change" for a
    person. This answers "what would be done" for the code that does it — the
    two are different questions and one function trying to be both ends up
    truthful to neither.

    Nothing here touches a store. The plan is a value: it can be tested, shown,
    diffed, and refused, and the route that executes it has no decisions left to
    make. That matters because an import is the one operation in Wavr that
    destroys work an operator did by hand.

    ## What is refused rather than guessed

    **An anchor whose room does not survive the import.** The file may carry an
    anchor in "study" while the incoming floor plan has no "study" — because the
    export came from a Space that has since been re-drawn. Writing it would
    create an anchor pointing at nothing; skipping it silently would lose
    something the operator made. It is listed, by name, with the reason.

    **A camera, always, in the sense that matters.** The URL carries a password
    and is deliberately absent from every export. The camera's name, room and
    calibration come back; the stream address must be typed again. Imported
    cameras are reported as needing one rather than created half-working, so
    "why is this camera dark" is answered before it is asked.
    """
    config = check_import(payload)
    house = config.get("house") or {}
    # Checked HERE, not at write time. The preview's whole promise is "nothing
    # has been written"; if the apply then fails on the floor plan, an operator
    # has seen a green preview and a red apply for the same file, and learns
    # that the preview means nothing. Injected rather than imported so this
    # module stays pure and testable without a house map.
    if validate_house is not None and house.get("floors"):
        try:
            validate_house(house)
        except Exception as exc:            # noqa: BLE001
            raise TransferError(
                f"the floor plan in this file cannot be read: {exc}") from None
    floors = house.get("floors") or []
    incoming_rooms = [r.get("name") for f in floors
                      for r in (f.get("rooms") or []) if r.get("name")]
    incoming = set(incoming_rooms)
    known = set(existing_rooms or ())

    anchors_ok, anchors_orphaned = [], []
    for a in config.get("anchors") or []:
        room = str(a.get("room") or "")
        (anchors_ok if room in incoming else anchors_orphaned).append(
            {"name": a.get("name", ""), "room": room})

    return {
        "digest": config_digest(config),
        "space": config.get("space", {}),
        "house": config.get("house") or {},
        "rooms": {
            "incoming": incoming_rooms,
            "replaced": sorted(known & incoming),
            "lost": sorted(known - incoming),
        },
        "anchors": {
            "write": anchors_ok,
            # Named, never dropped in silence.
            "skipped_no_such_room": anchors_orphaned,
        },
        "cameras": [{"name": c.get("name", ""), "room": c.get("room", ""),
                     "needs_url": True} for c in config.get("cameras") or ()],
        "nodes": list(config.get("nodes") or ()),
        "ha_mappings": list(config.get("ha_mappings") or ()),
        "external_providers": list(config.get("external_providers") or ()),
        "settings": dict(config.get("settings") or {}),
        "secrets_needed": config.get("secrets_you_must_supply_again", []),
    }


def _config_shape(config: dict) -> dict:
    """How much of what, with none of the operator's own words.

    Counts and kinds. A supporter needs to know there are four cameras and nine
    rooms; they do not need to know a room is called "Sam's bedroom", and the
    person emailing the file has no way to notice that it was in there.
    """
    floors = (config.get("house") or {}).get("floors") or []
    rooms = [r for f in floors for r in (f.get("rooms") or [])]
    return {
        "space_kind": (config.get("space") or {}).get("kind", ""),
        "floors": len(floors),
        "rooms": len(rooms),
        "rooms_with_geometry": sum(1 for r in rooms if r.get("polygon")),
        "cameras": len(config.get("cameras") or ()),
        "nodes": len(config.get("nodes") or ()),
        "anchors": len(config.get("anchors") or ()),
        "anchors_positioned": sum(
            1 for a in config.get("anchors") or () if a.get("x") is not None),
        "ha_mappings": len(config.get("ha_mappings") or ()),
        "external_providers": len(config.get("external_providers") or ()),
        "settings_changed": len(config.get("settings") or {}),
    }


def _space_fingerprint(space) -> "str | None":
    """A stable, one-way fingerprint of the Space — never the Space itself.

    `space_id` is not a label: `api_space.join_space` accepts it as the exact
    credential a second Core presents to join this Space (see
    `backend/wavr/api_space.py`), so it belongs on the same list as a token or
    a password, not on the same list as a room name. A supporter comparing two
    diagnostic bundles from the same household still needs SOME way to tell
    they are looking at the same install; a one-way hash gives them that
    without handing back the value the hash was made from.

    Returns `None` — never an empty string a caller could mistake for a real,
    absent fingerprint — when there is no Space to fingerprint at all.
    """
    sid = str((space or {}).get("space_id") or "")
    if not sid:
        return None
    return "sp_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def _node_health(nodes) -> dict:
    """Node counts, by state and by modality — never a node's name.

    The same reasoning as `_config_shape`, applied to nodes specifically: a
    node's operator-given name is exactly the kind of free text that already
    carried a person's name once, in an anchor, before this module's own
    "shape, not content" rule existed (see `diagnostic_bundle`'s docstring). A
    supporter diagnosing "half my sensors dropped off" needs the counts, not
    which one is called "Sam's room".
    """
    rows = list(nodes or ())
    by_state: dict[str, int] = {}
    by_modality: dict[str, int] = {}
    for n in rows:
        if not isinstance(n, dict):
            continue
        state = str(n.get("state") or "unknown")
        modality = str(n.get("modality") or "unknown")
        by_state[state] = by_state.get(state, 0) + 1
        by_modality[modality] = by_modality.get(modality, 0) + 1
    return {"count": len(rows), "by_state": by_state, "by_modality": by_modality}


def diagnostic_bundle(*, config=None, coverage=(), providers=None,
                      source_health=(), clocks=None, recent_events=(),
                      version="", platform="", space=None, now=None) -> dict:
    """What Wavr can see, packaged for somebody trying to help.

    The health of every sensor and provider, and the recent SEMANTIC events —
    which carry a room, a boolean and a sensor id, and never a person or a
    coordinate.

    Deliberately not the room state history. "The kitchen was occupied at 23:40"
    is a fact about somebody's evening, and a support bundle is a file that ends
    up in a ticket system.

    ## Why the configuration is SHAPE, not content

    This used to embed the whole export. Its own note promised "no coordinates,
    nobody's name" and the file carried anchor x/y/z, anchor names, and every
    room polygon — with `data_inventory` marking anchors sensitive on the exact
    grounds that "a name can carry a person's name if you put one there".

    `audit()` did not catch it and could not: it looks for credential SHAPES,
    and a person's name in an anchor label is not a credential.

    What a supporter actually needs from the configuration is its shape — how
    many cameras, how many rooms, which providers — not the household's floor
    plan. So the shape travels and the content does not, and the note is now
    true of the file it is printed inside.

    `space` is the RAW space row (the one `export_config` is also handed, and
    the one it deliberately does not forward — see its own docstring), passed
    here separately so this function can turn `space_id` into a fingerprint
    without ever putting the id itself in the output. See
    `_space_fingerprint`.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    rows = list(recent_events or ())[-MAX_BUNDLE_ROWS:]
    return {
        "wavr_bundle_version": BUNDLE_VERSION,
        "generated_at": stamp,
        "wavr_version": version,
        "platform": platform,
        # A one-way hash, never the Space's own join credential. See
        # `_space_fingerprint`.
        "space_fingerprint": _space_fingerprint(space),
        # Shape, never content. See the docstring: the promise on this file is
        # "no coordinates, nobody's name", and it has to be true of the file
        # rather than true of an intention.
        "config_shape": _config_shape(config or {}),
        # Counts, never a node's own name — same rule as `config_shape`.
        "nodes": _node_health((config or {}).get("nodes")),
        "coverage": [dict(c) for c in coverage or ()],
        "providers": providers or {},
        "source_health": [dict(h) for h in source_health or ()],
        "clocks": clocks or {},
        "recent_events": [dict(e) for e in rows],
        "note": ("Sensor health, coverage and the semantic event stream — a "
                 "room, a boolean, a sensor id. No credentials, no camera URLs, "
                 "no coordinates, nobody's name, and no history of who was "
                 "where."),
    }


def audit(document) -> list[str]:
    """Anything in a finished export or bundle that looks like a secret.

    A belt-and-braces pass over the SERIALISED document, because the allowlists
    above are only as good as the last person who added a field. Returns the
    paths of anything suspicious, so the caller can refuse to hand the file over
    rather than discovering the leak in somebody's inbox.
    """
    suspicious = ("password", "passwd", "token", "secret", "rtsp://",
                  "api_key", "apikey", "authorization", "bearer ",
                  "private_key", "-----begin")
    found: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower()
                # A KEY that names a secret is suspicious whatever its value —
                # except the two fields whose whole job is to name secrets
                # without carrying them.
                if (any(s in key for s in suspicious)
                        and k not in ("secrets_you_must_supply_again",
                                      "secrets_needed")):
                    found.append(f"{path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            low = node.lower()
            # No exemption for `note` / `notes`, and the old one was a real hole.
            #
            # Its reasoning was that those fields carry Wavr's own "no secrets
            # here" prose, so the WORDS appear there legitimately. True -- but
            # PROVIDER_FIELDS and ANCHOR_FIELDS both export an OPERATOR's own
            # free-text note, and somebody documenting a partner integration is
            # exactly the person who pastes an RTSP URL or a PEM block into one.
            # The single gate designed to catch a credential in a file that has
            # already been emailed was switched off for the field most likely to
            # hold one.
            #
            # Nothing is lost by removing it: these three patterns match a
            # credential's SHAPE, not the words "password" or "token", and
            # Wavr's own prose contains none of them. A test pins that.
            if any(s in low for s in ("rtsp://", "-----begin", "bearer ")):
                found.append(path)
    walk(document, "$")
    return sorted(set(found))
