"""Erasing what Wavr learned, without erasing the Wavr somebody set up.

`data_inventory.py` answers "what does this thing store about my home?" and is
read-only by construction — its own docstring says a route that could also
change things would turn an audit screen into an attack surface, and that is
right. So the acting half lives here, behind its own gate, and CONSUMES that
inventory rather than keeping a second list of tables. One producer: the thing
that says what is stored is the thing that knows what to delete, and the two
cannot drift into disagreeing about what exists.

## The distinction the whole feature turns on

"Delete what you know about me" and "delete my installation" are different
requests, and a button that confuses them destroys an afternoon of somebody's
work.

  * **observation** — what Wavr learned by watching. Occupancy history, room
    readings, things it noticed, how well each sensor has been performing.
    Deleting this costs accuracy that rebuilds itself, and nothing else.
  * **setup** — what a person told Wavr. The Space, the people, the rooms, the
    cameras, the paired phones, the routines — and every "that's mine" they
    pressed on a network device. Deleting this is not a privacy action; it is
    starting over.

The line between them is not always where the label suggests, which is why the
classification is written out per category with the borderline calls argued.
`known_devices` reads like an observation and is not: the table holds
`(mac, known)`, the decisions, while the sightings live in the live inventory.

Everything is classified below, by hand, as data rather than as branching —
because this is the part a reviewer must be able to check at a glance, and
`test_data_erasure.py` fails if a category added later is not on this list.
Silence must not default to erasable.

## What this module will not do

  * It never touches a category whose retention is `never` or `session`: there
    is nothing on disk to delete, and pretending otherwise would report a
    deletion that did not happen.
  * It never drops a table or a column. Rows only. A schema this deleted would
    have to be rebuilt by a migration nobody ran.
  * It never erases `setup` unless the caller names those categories
    explicitly. There is no "everything" shorthand, on purpose: the shorthand
    is what somebody clicks at speed.
"""
from __future__ import annotations

import sqlite3

from wavr.data_inventory import CATEGORIES, PERSISTENT

OBSERVATION = "observation"
SETUP = "setup"

# Every persistent category, classified. See the module docstring for what the
# two words mean and why the list is data rather than a rule.
#
# The borderline calls, stated so they can be argued with:
#   * `known_devices` LOOKS like an observation and is setup — see the note
#     beside it. `device_meta` is setup for the plainer reason: those are
#     names a person typed.
#   * `identity_devices` is setup for the same reason: a label somebody
#     applied, not something Wavr worked out.
#   * `assistant_log` is an observation of the person rather than of the house,
#     and it is the single most personal table here. It goes first.
#   * `discoveries` is an observation whose table also holds decisions. It is
#     erased through a ROW FILTER rather than reclassified — see `ROW_FILTER`.
#   * `ha_devices` LOOKS like setup and is not: see the note beside it.
#   * `gateway_binding` is an observation (a fingerprint Wavr took), but
#     erasing it makes the next boot report a changed router until it re-learns
#     — which is honest, and is why it is not in the default set below.
#   * `core_pin` is setup: erasing it would silently unpair nothing and lock
#     nobody out, but it is a credential, and credentials are not privacy
#     housekeeping.
CLASSIFICATION: dict[str, str] = {
    # -- what Wavr learned by watching -------------------------------------
    "assistant_log": OBSERVATION,
    "occupancy_log": OBSERVATION,
    "room_states": OBSERVATION,
    "sensor_reliability": OBSERVATION,
    "validation_sessions": OBSERVATION,
    "discoveries": OBSERVATION,
    "gateway_binding": OBSERVATION,
    # Filed as setup at first because it sits among the Home Assistant
    # screens — the same "read the neighbourhood, not the store" mistake that
    # put `known_devices` on the wrong side. Every column (mac, device_type,
    # make, model, os) is copied out of HA's own device registry by
    # `map_device()`; nobody types, chooses or corrects anything. A person
    # presses Import and a copy happens. Deleting it costs device identity on
    # the Network screen until the next import, which is accuracy that
    # rebuilds — the definition of an observation. Kept OUT of the default set
    # for `gateway_binding`'s reason: the consequence is visible.
    "ha_devices": OBSERVATION,
    # -- what a person set up ----------------------------------------------
    "assistant_setup": SETUP,
    # Named "Devices seen on your network", which reads like an observation and
    # is not what the TABLE holds: `known_devices` is `(mac, known)` — the
    # "that's mine" decisions a person pressed. The sightings live in the live
    # inventory. Erasing it under "delete what Wavr learned" would undo every
    # one of those clicks, re-flood New devices and make Discoveries re-raise
    # the whole network. Caught by reading the store instead of the label.
    "known_devices": SETUP,
    "anchors": SETUP,
    "people": SETUP,
    "person_devices": SETUP,
    "devices": SETUP,
    "identity_devices": SETUP,
    "device_meta": SETUP,
    "device_roles": SETUP,
    "cameras": SETUP,
    "nodes": SETUP,
    "camera_calib": SETUP,
    "space": SETUP,
    "cores": SETUP,
    "peers": SETUP,
    "room_links": SETUP,
    "settings": SETUP,
    "connectors": SETUP,
    "experience_grants": SETUP,
    "external_providers": SETUP,
    "ha_presence_map": SETUP,
    "core_pin": SETUP,
    "routines": SETUP,
}

# What "erase what Wavr knows about my home" means when nobody names anything.
# Deliberately NOT every observation: `gateway_binding` is left out because
# dropping it makes the next boot report the router as changed, which looks
# like an intrusion warning to somebody who just cleared their history.
DEFAULT_OBSERVATIONS: tuple[str, ...] = (
    "assistant_log", "occupancy_log", "room_states",
    "sensor_reliability", "validation_sessions", "discoveries",
)


# Categories whose table holds BOTH halves, and the SQL that isolates the
# observed one.
#
# The default is a whole-table delete, and for thirty of the thirty-one
# categories that is exactly right. `discoveries` is the exception, and the
# reason is worth reading before adding a second one: its rows are raised by
# the discovery sweep (observation) but carry a `status` column written by a
# person pressing Ignore or Accept — a decision stored nowhere else. Wiping the
# table destroys those decisions, and the next sweep re-raises every dismissed
# card within seconds, which `discovery_inbox`'s own docstring calls "how an
# inbox trains people to ignore it".
#
# Classifying it as setup would fail the other way: the cards ARE what Wavr
# learned, and putting them beyond the reach of "delete what Wavr learned" is
# the same mistake pointed the other direction.
#
# A filter here must isolate rows that carry NO human decision. If you cannot
# write that clause honestly, the category is setup.
ROW_FILTER: dict[str, str] = {
    # Cards nobody has answered. An accepted or dismissed row is an answer.
    "discoveries": "status = 'pending'",
}


class ErasureError(ValueError):
    """A request this module refuses to carry out."""


def erasable() -> dict[str, dict]:
    """Every category that CAN be erased, with the words a person needs.

    Anything not stored on disk is absent rather than listed as "0 deleted":
    offering to delete something that was never written teaches people the
    screen is theatre.
    """
    out = {}
    for c in CATEGORIES:
        if c.retention != PERSISTENT or not c.table:
            continue
        kind = CLASSIFICATION.get(c.key)
        if kind is None:
            continue
        out[c.key] = {
            "key": c.key,
            "label": c.label,
            "what": c.what,
            "kind": kind,
            "sensitive": bool(c.sensitive),
            "tables": (c.table,) + tuple(c.also or ()),
            # Present only where the table mixes observation with decision.
            "where": ROW_FILTER.get(c.key),
        }
    return out


def plan(keys) -> list[dict]:
    """Resolve requested keys into what would be deleted. Raises rather than
    quietly skipping: a partial erase reported as a complete one is the worst
    outcome this module can produce."""
    known = erasable()
    wanted = list(dict.fromkeys(keys or ()))      # de-dupe, keep order
    if not wanted:
        raise ErasureError("name at least one category to erase")
    unknown = [k for k in wanted if k not in known]
    if unknown:
        raise ErasureError(
            "not erasable: " + ", ".join(sorted(unknown))
            + ". Either it is not stored on disk, or it is not on the "
              "classification list in data_erasure.py.")
    return [known[k] for k in wanted]


def counts(db_path: str, keys) -> dict[str, int]:
    """How many rows each requested category holds right now.

    Shown BEFORE the confirm, so "this deletes 41,208 occupancy rows" is a
    number somebody saw rather than discovered.
    """
    entries = plan(keys)
    out: dict[str, int] = {}
    # `with sqlite3.connect(...)` commits, it does NOT close — the connection
    # and its file handle survive the block. On Windows that handle keeps the
    # database locked, which is how a read-only count ends up blocking the
    # erase that follows it and breaking tmp-dir cleanup in tests.
    conn = sqlite3.connect(db_path)
    try:
        for e in entries:
            total = 0
            for table in e["tables"]:
                # The count must match what the erase will actually remove,
                # or the number shown before the confirm is a different
                # promise from the one kept.
                clause = (" WHERE " + e["where"]) if e.get("where") else ""
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table}{clause}").fetchone()  # noqa: S608
                except sqlite3.Error:
                    continue          # table absent in this build: nothing there
                total += int(row[0] or 0)
            out[e["key"]] = total
    finally:
        conn.close()
    return out


def erase(db_path: str, keys) -> dict:
    """Delete the rows, and report exactly what went.

    One transaction: a half-finished erase would leave somebody believing their
    history is gone when part of it is not, and that is a worse state than
    having refused.

    Rows only — never a DROP. A schema deleted here would need a migration
    nobody is going to run.
    """
    entries = plan(keys)
    before = counts(db_path, keys)
    deleted: dict[str, int] = {}
    missing: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        with conn:                                   # one transaction
            for e in entries:
                gone = 0
                clause = (" WHERE " + e["where"]) if e.get("where") else ""
                for table in e["tables"]:
                    try:
                        cur = conn.execute(
                            f"DELETE FROM {table}{clause}")  # noqa: S608
                    except sqlite3.Error:
                        missing.append(table)
                        continue
                    gone += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                deleted[e["key"]] = gone
    finally:
        conn.close()
    out = {
        "deleted": deleted,
        "before": before,
        "total": sum(deleted.values()),
    }
    if missing:
        # Never silently. A table this build does not have is fine; a table it
        # was supposed to have and could not read is not, and only the operator
        # can tell those apart.
        out["tables_not_present"] = sorted(set(missing))
    return out
