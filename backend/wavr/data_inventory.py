"""Everything Wavr keeps, said plainly — and everything it deliberately does not.

## The objection this answers

"This is too invasive." Local-first does not, by itself, make anybody
comfortable. A system that can see presence, count, movement, position and
device identity is uncomfortable in proportion to how little you can find out
about what it retains.

So: one screen, one API, one truthful answer to *what does this thing actually
store about my home?* Not a developer diagnostic — a trust feature, and the
difference is that it must be complete and it must be legible.

## Why the absences matter as much as the contents

Half of this file is a list of things Wavr does NOT store, and that half is
load-bearing. "Camera frames: never written to disk" is a stronger statement
than any list of tables, and it is unfalsifiable unless it is stated somewhere a
person can find it and a test can pin it.

Three retention classes, and every category declares one:

  * `persistent` — survives a restart, in the database.
  * `session`    — held in memory while the Core runs; a restart clears it.
  * `never`      — passes through RAM and is discarded; nothing is written
                   anywhere, ever.

## The rule for this module

**It must never be the thing that leaks.** It reports categories, row counts and
descriptions. It never returns a token, a password, a certificate key or an RTSP
URL, because a "show me my data" screen that exposes a credential is a worse
privacy failure than not having one.
"""
from __future__ import annotations

import sqlite3

PERSISTENT = "persistent"
SESSION = "session"
NEVER = "never"


class Category:
    """One kind of thing Wavr holds.

    `table` is None for anything not in the database — which is how a memory-only
    or never-stored category can appear in the same list as a stored one without
    pretending to be a row count.
    """

    __slots__ = ("key", "label", "what", "retention", "table", "sensitive",
                 "where", "counts")

    def __init__(self, key: str, label: str, what: str, retention: str,
                 table: str | None = None, sensitive: bool = False,
                 where: str = "", counts: str = ""):
        self.key = key
        self.label = label
        self.what = what
        self.retention = retention
        self.table = table
        self.sensitive = sensitive
        # A row count is not always the honest number. `connectors` holds a row
        # per KNOWN integration whether or not it is switched on, and reporting
        # the total on a screen that answers "is anything leaving my network"
        # reads as seven things leaving when the answer is none. `where` narrows
        # the count; `counts` names what the number then means, because "7" and
        # "7 enabled" are different claims and the UI must not have to guess.
        self.where = where
        self.counts = counts


# Ordered the way a worried person reads: the things that sound most invasive
# first, so the answer to "does it keep video?" is not buried under settings.
CATEGORIES: tuple[Category, ...] = (
    Category("camera_frames", "Camera images",
             "Never written anywhere. Frames exist in memory long enough to ask "
             "'is there a person in this picture?' and are then discarded. Wavr "
             "has no video storage of any kind.",
             NEVER),
    Category("positions", "Where people are standing",
             "Live only. Coordinates are streamed to your own screen and never "
             "stored, so there is no history of where anyone stood.",
             NEVER),
    Category("vitals", "Breathing and heart rate",
             "Live only, research-grade, and never stored. Wavr is not a medical "
             "device.",
             NEVER),
    Category("occupancy_log", "Room occupancy over time",
             "Whether each room was occupied, how many people when a sensor "
             "could honestly count, and how sure Wavr was. No names, no "
             "coordinates. This is what the routines and history screens read.",
             PERSISTENT, "occupancy_log"),
    Category("people", "People you added",
             "The names you typed and the role you gave each person. Wavr never "
             "learns a name from a camera or a microphone — every one of these "
             "was typed by you.",
             PERSISTENT, "people", sensitive=True),
    Category("person_devices", "Which device belongs to whom",
             "The links you confirmed between a person and a device. Used to "
             "decide what each device is allowed to do.",
             PERSISTENT, "person_devices", sensitive=True),
    Category("devices", "Paired devices",
             "One row per device you paired, with the role it holds. Tokens are "
             "stored HASHED and are not recoverable — not by you, not by Wavr.",
             PERSISTENT, "devices"),
    Category("identity_devices", "Device-to-person labels",
             "Optional labels associating a network or Bluetooth address with a "
             "person, so Wavr can say who is home. Off by default.",
             PERSISTENT, "identity_devices", sensitive=True),
    Category("known_devices", "Devices seen on your network",
             "Addresses, vendor names and types of the devices Wavr has seen. "
             "The same information your router already has.",
             PERSISTENT, "known_devices"),
    Category("cameras", "Cameras you added",
             "Each camera's name, room and stream address. The password is part "
             "of that address and is never shown back to you or sent anywhere.",
             PERSISTENT, "cameras", sensitive=True),
    Category("nodes", "Sensor nodes",
             "The small sensors you enrolled, with the room and type YOU set. "
             "What a node says about itself is kept separately and never "
             "trusted.",
             PERSISTENT, "nodes"),
    Category("sensor_reliability", "How well each sensor performs",
             "Counts of how often each sensor agreed with you during a guided "
             "walk. Numbers about sensors, never about people.",
             PERSISTENT, "sensor_reliability"),
    Category("validation_sessions", "Guided walks you ran",
             "What you declared during a walk and how each sensor scored. No "
             "record of who was walking.",
             PERSISTENT, "validation_sessions"),
    Category("camera_calib", "Camera calibration",
             "The geometry that lets a camera place a person on your floor plan: "
             "reference points and a mounting pose. No images.",
             PERSISTENT, "camera_calib"),
    Category("space", "Your Space",
             "The name you gave this place and what kind of place it is. One "
             "row. Never broadcast on your network — other devices see an "
             "opaque id, never the name.",
             PERSISTENT, "space"),
    Category("cores", "Cores in this Space",
             "The machines running Wavr for you, and which one is in charge.",
             PERSISTENT, "cores"),
    Category("peers", "Paired Cores",
             "Certificate fingerprints of Cores this one trusts.",
             PERSISTENT, "peers"),
    Category("room_links", "Room connections you corrected",
             "Where you told Wavr the floor plan was wrong about which rooms "
             "connect.",
             PERSISTENT, "room_links"),
    Category("settings", "Settings you changed",
             "Only the switches you moved away from their default. No secrets "
             "can be stored here — the settings store refuses them.",
             PERSISTENT, "settings"),
    Category("connectors", "External connections",
             "Integrations that reach outside this machine. The number is how "
             "many are switched ON — zero means nothing leaves your network, "
             "however many Wavr knows how to talk to.",
             PERSISTENT, "connectors", sensitive=True,
             where="enabled = 1", counts="enabled"),
    Category("routines", "Routines",
             "The 'when this, do that' rules you wrote.",
             PERSISTENT, "routines"),
    Category("discoveries", "Things Wavr noticed",
             "The cards in your Discovery inbox and what you decided about "
             "them.",
             PERSISTENT, "discoveries"),
    Category("room_states", "Recent room readings",
             "A short history of fused room state, used by the history and "
             "narration screens.",
             PERSISTENT, "room_states"),
    Category("trace", "Sensor recordings",
             "Only while you are recording one, and only in memory. Stopping "
             "Wavr discards it. Nothing is written unless you download it "
             "yourself.",
             SESSION),
)


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int | None:
    """Rows in a table, or None when Wavr genuinely cannot tell.

    None rather than 0: a table that does not exist yet and a table that is
    empty are different facts, and rendering both as "0 rows" would make a
    missing feature look like an empty one.

    `where` comes from this module's own CATEGORIES table and never from a
    request — there is no path by which a caller can put a clause in here.
    """
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else None


def inventory(db_path: str) -> dict:
    """Every category, with a live row count where one applies.

    Opened read-only-ish and per call rather than holding a handle: this runs
    when somebody presses a button, not in a loop, and a long-lived connection
    for a rarely-used screen is a lock waiting to happen.
    """
    out = []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        for cat in CATEGORIES:
            row = {
                "key": cat.key,
                "label": cat.label,
                "what": cat.what,
                "retention": cat.retention,
                "sensitive": cat.sensitive,
            }
            if cat.table is not None:
                row["rows"] = _count(conn, cat.table, cat.where)
                if cat.counts:
                    # Say what the number means when it is not "all of them".
                    row["counts"] = cat.counts
            out.append(row)
    except sqlite3.Error:
        # The categories and their descriptions are still true even when the
        # database cannot be opened. Returning nothing would be less honest than
        # returning the list without counts.
        out = [{"key": c.key, "label": c.label, "what": c.what,
                "retention": c.retention, "sensitive": c.sensitive}
               for c in CATEGORIES]
    finally:
        if conn is not None:
            conn.close()

    return {
        "categories": out,
        "counts": {
            "persistent": sum(1 for c in CATEGORIES if c.retention == PERSISTENT),
            "session": sum(1 for c in CATEGORIES if c.retention == SESSION),
            "never": sum(1 for c in CATEGORIES if c.retention == NEVER),
        },
        "note": ("Everything here lives on this machine. Nothing is uploaded "
                 "unless you enable an external connection, and none of it "
                 "includes an image, a recording, or a position history."),
        "secrets_note": ("Credentials are never listed here. Device tokens are "
                         "stored hashed and cannot be recovered; camera "
                         "passwords are part of a stream address that is never "
                         "shown back or sent anywhere."),
    }
