"""Named places inside a Space — the vocabulary applications point at.

## What an anchor is for

A room is the coarsest useful unit and often too coarse. "Somebody is in the
kitchen" is a different fact from "somebody is at the kitchen counter", and an
application that wants to say the second needs a name for the counter. An anchor
is that name: a stable identifier for a place, owned by the Space rather than by
whichever app or headset happened to create it.

## The rule that shapes everything here

**A logical anchor is a first-class anchor, not a degraded one.**

    Kitchen counter
    Room: kitchen
    (no coordinates at all)

That is complete. It is enough to write "when somebody is near the kitchen
counter" against, enough to show in a list, enough to attach an experience to.
Most homes will never own an AR headset, and a model that treated an anchor
without a pose as an unfinished anchor would make the entire feature dead weight
for almost everybody who installs Wavr.

Coordinates are an *upgrade*, added when a Space genuinely has the geometry.

## The rules that keep coordinates honest

**A coordinate without a frame is not a position** — the same rule
`spatial_frames` applies to a sensor target, and for the same reason. An anchor
stores room-local metres and says so.

**A coordinate far outside its room is refused, not stored.** An anchor at
`(2400, 1500)` in a four-metre kitchen is not a mistake about geometry, it is
millimetres arriving where metres were expected — exactly the units confusion
that put radar targets in the wrong place and let the precision ladder promote
them to Wavr's highest confidence. Catching it at the door is much cheaper than
finding it on a map later.

**An external anchor id is an IDENTITY link, never a position claim.** ARKit,
OpenXR and every other spatial runtime give you an opaque id and a transform in
*their* world frame — an origin that is wherever that particular session started
and means nothing in the next one. So a mapping binds `their id` to `our anchor`
and stops there. Importing their transform as room coordinates would be the D-006
bug again, with a vendor's name on it.

**An anchor whose room disappeared is shown, not hidden.** A renamed or deleted
room leaves anchors pointing at nothing. Silently dropping them destroys an
operator's work; silently keeping them makes an application ask about a room that
does not exist. So they are kept, marked `orphaned`, and surfaced.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wavr.contracts import version
from wavr.spatial_frames import FRAME_ROOM

# The shape an anchor is published in. Carried on the summary rather than on
# every row: anchors arrive as a list from one endpoint, so one stamp is enough
# and thirty copies of it would be noise.
ANCHORS_VERSION = version("anchors")

# What shape of place this is.
KIND_LOGICAL = "logical"   # a named place in a room; no coordinates. The default.
KIND_POINT = "point"       # one spot, in room-local metres
KIND_AREA = "area"         # a polygon, in room-local metres

KINDS: frozenset[str] = frozenset({KIND_LOGICAL, KIND_POINT, KIND_AREA})

# How an external system's id got attached to a Wavr anchor. The distinction
# matters when two runtimes claim the same anchor and disagree: one of them was
# placed by a person and the other was matched by software.
ORIGIN_DECLARED = "declared"   # a person bound these two together
ORIGIN_IMPORTED = "imported"   # an adapter reported the pairing

MAX_NAME = 80
MAX_NOTE = 400
MAX_POLYGON = 64
# How far outside its room's bounding box a coordinate may sit before it is
# refused. Generous enough for an anchor ON a wall (a window, a door frame,
# a TV mounted flush) and nowhere near large enough to let a millimetre value
# through: half a metre versus the ~1000x error a units mix-up produces.
ROOM_SLACK_M = 0.5


class AnchorError(ValueError):
    """An anchor that would claim something the Space cannot support."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS anchors (
    anchor_id  TEXT PRIMARY KEY,
    name       TEXT    NOT NULL,
    room       TEXT    NOT NULL,
    level      INTEGER NOT NULL DEFAULT 0,
    kind       TEXT    NOT NULL DEFAULT 'logical',
    -- Room-local metres, NULL for a logical anchor. Frame is implicit and
    -- singular on purpose: there is exactly one frame an anchor may be stored
    -- in, so there is no way to store one in the wrong frame by omission.
    x          REAL,
    y          REAL,
    z          REAL,
    polygon    TEXT    NOT NULL DEFAULT '',   -- "x,y;x,y;..." for an area
    note       TEXT    NOT NULL DEFAULT '',
    created_ts TEXT    NOT NULL,
    updated_ts TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS anchor_mappings (
    anchor_id   TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'declared',
    created_ts  TEXT NOT NULL,
    PRIMARY KEY (anchor_id, provider_id, external_id)
);

CREATE INDEX IF NOT EXISTS anchor_mappings_by_external
    ON anchor_mappings (provider_id, external_id);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text, what: str, limit: int) -> str:
    s = " ".join(str(text or "").split())
    if not s:
        raise AnchorError(f"{what} is required")
    if len(s) > limit:
        raise AnchorError(f"{what} must be {limit} characters or fewer")
    return s


def _finite(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


@dataclass(frozen=True)
class Anchor:
    """One named place. Frozen: an anchor that could rewrite itself in flight
    would let a read and a later write disagree about what was authorised."""

    anchor_id: str
    name: str
    room: str
    level: int = 0
    kind: str = KIND_LOGICAL
    x: float | None = None
    y: float | None = None
    z: float | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    note: str = ""
    created_ts: str = ""
    updated_ts: str = ""
    mappings: tuple[dict, ...] = ()

    @property
    def positioned(self) -> bool:
        return self.kind in (KIND_POINT, KIND_AREA)

    def to_dict(self, *, known_rooms=None) -> dict:
        out = {
            "anchor_id": self.anchor_id,
            "name": self.name,
            "room": self.room,
            "level": self.level,
            "kind": self.kind,
            "positioned": self.positioned,
        }
        if self.kind == KIND_POINT:
            # The frame is stated even though only one is storable. A consumer
            # reading `x`/`y` off an anchor and off a Target should not have to
            # remember which of them carries a frame and which does not.
            out.update({"x": self.x, "y": self.y, "frame": FRAME_ROOM})
            if self.z is not None:
                out["z"] = self.z
        elif self.kind == KIND_AREA:
            out.update({"polygon": [list(p) for p in self.polygon],
                        "frame": FRAME_ROOM})
        if self.note:
            out["note"] = self.note
        if self.mappings:
            out["mappings"] = [dict(m) for m in self.mappings]
        if known_rooms is not None:
            # An anchor whose room has been renamed or deleted. Kept and MARKED:
            # dropping it destroys an operator's work, and keeping it silently
            # makes applications ask about a room that no longer exists.
            out["orphaned"] = self.room not in set(known_rooms)
        out["created_ts"] = self.created_ts
        out["updated_ts"] = self.updated_ts
        return out


def _validate_in_room(room_polygon, points) -> None:
    """Refuse coordinates that cannot be in this room.

    Bounding box plus slack rather than a strict point-in-polygon test: an anchor
    genuinely belongs ON a wall much of the time (a window, a door, a wall-mounted
    TV), and a strict test would reject the most common real case. What this
    catches is the error worth catching -- a value three orders of magnitude too
    large, which is millimetres arriving where metres were expected.
    """
    if not room_polygon or len(room_polygon) < 3:
        # No geometry to check against. A Space without a floor plan can still
        # have positioned anchors; there is simply nothing to validate them
        # against, and refusing them would punish the operator for a drawing
        # they have not made yet.
        return
    xs = [p[0] for p in room_polygon]
    ys = [p[1] for p in room_polygon]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    for (x, y) in points:
        if not (-ROOM_SLACK_M <= x <= w + ROOM_SLACK_M
                and -ROOM_SLACK_M <= y <= h + ROOM_SLACK_M):
            raise AnchorError(
                f"({x:g}, {y:g}) is outside a {w:.1f}x{h:.1f}m room. Anchor "
                f"coordinates are METRES from the room's own corner — a value "
                f"this far out is usually millimetres.")


class AnchorStore:
    """Anchors and their external id mappings. Shares `wavr.db`, owns two tables.

    Configuration only: a name, a room, optionally a coordinate. No presence, no
    history, nobody's position. An anchor says where the counter is, never who is
    standing at it.
    """

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes --------------------------------------------------------------

    def create(self, name: str, room: str, *, level: int = 0,
               kind: str = KIND_LOGICAL, x=None, y=None, z=None,
               polygon=None, note: str = "", room_polygon=None) -> Anchor:
        name = _clean(name, "anchor name", MAX_NAME)
        room = _clean(room, "room", MAX_NAME)
        if kind not in KINDS:
            raise AnchorError(f"kind must be one of {sorted(KINDS)}")
        note = " ".join(str(note or "").split())[:MAX_NOTE]
        poly: tuple[tuple[float, float], ...] = ()

        if kind == KIND_POINT:
            if not (_finite(x) and _finite(y)):
                raise AnchorError("a point anchor needs finite x and y in metres")
            x, y = float(x), float(y)
            if z is not None:
                if not _finite(z):
                    raise AnchorError("z must be a finite number of metres")
                z = float(z)
            _validate_in_room(room_polygon, [(x, y)])
        elif kind == KIND_AREA:
            pts = list(polygon or ())
            if len(pts) < 3:
                raise AnchorError("an area anchor needs at least 3 points")
            if len(pts) > MAX_POLYGON:
                raise AnchorError(f"an area may have at most {MAX_POLYGON} points")
            for p in pts:
                if not (isinstance(p, (list, tuple)) and len(p) == 2
                        and _finite(p[0]) and _finite(p[1])):
                    raise AnchorError("each area point must be [x, y] in metres")
            poly = tuple((float(a), float(b)) for a, b in pts)
            _validate_in_room(room_polygon, poly)
            x = y = z = None
        else:
            # A logical anchor carries no geometry, and silently keeping a
            # coordinate somebody passed by mistake would make `positioned`
            # disagree with what is stored.
            x = y = z = None

        anchor_id = f"anc_{secrets.token_hex(8)}"
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO anchors (anchor_id, name, room, level, kind, x, y, z,"
                " polygon, note, created_ts, updated_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (anchor_id, name, room, int(level), kind, x, y, z,
                 _dump_polygon(poly), note, ts, ts))
            self._conn.commit()
        return Anchor(anchor_id, name, room, int(level), kind, x, y, z, poly,
                      note, ts, ts)

    def rename(self, anchor_id: str, name: str) -> Anchor:
        name = _clean(name, "anchor name", MAX_NAME)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE anchors SET name = ?, updated_ts = ? WHERE anchor_id = ?",
                (name, self._now(), anchor_id))
            self._conn.commit()
        if cur.rowcount == 0:
            raise AnchorError("no such anchor")
        return self.get(anchor_id)

    def move(self, anchor_id: str, room: str, *, level: int | None = None) -> Anchor:
        """Put an anchor in a different room.

        Coordinates are CLEARED, and the anchor becomes logical. They were metres
        from the old room's corner, and carrying them across would place the
        counter at whatever happens to be that far into the new room — a silent
        wrong answer rather than a visible missing one.
        """
        room = _clean(room, "room", MAX_NAME)
        existing = self.get(anchor_id)
        if existing is None:
            raise AnchorError("no such anchor")
        with self._lock:
            self._conn.execute(
                "UPDATE anchors SET room = ?, level = ?, kind = ?, x = NULL,"
                " y = NULL, z = NULL, polygon = '', updated_ts = ?"
                " WHERE anchor_id = ?",
                (room, int(existing.level if level is None else level),
                 KIND_LOGICAL, self._now(), anchor_id))
            self._conn.commit()
        return self.get(anchor_id)

    def place(self, anchor_id: str, x: float, y: float, *, z=None,
              room_polygon=None) -> Anchor:
        """Give a logical anchor a coordinate — the upgrade path.

        Where an AR session, a guided walk or a floor-plan click lands. The room
        is NOT changed: an anchor is placed within the room it already belongs
        to, so a bad coordinate can never also relocate it.
        """
        existing = self.get(anchor_id)
        if existing is None:
            raise AnchorError("no such anchor")
        if not (_finite(x) and _finite(y)):
            raise AnchorError("x and y must be finite metres")
        x, y = float(x), float(y)
        if z is not None:
            if not _finite(z):
                raise AnchorError("z must be a finite number of metres")
            z = float(z)
        _validate_in_room(room_polygon, [(x, y)])
        with self._lock:
            self._conn.execute(
                "UPDATE anchors SET kind = ?, x = ?, y = ?, z = ?, polygon = '',"
                " updated_ts = ? WHERE anchor_id = ?",
                (KIND_POINT, x, y, z, self._now(), anchor_id))
            self._conn.commit()
        return self.get(anchor_id)

    def delete(self, anchor_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM anchors WHERE anchor_id = ?", (anchor_id,))
            # Mappings go with it. An external id pointing at a deleted anchor is
            # a dangling reference that would resolve to nothing, and leaving it
            # behind means the next anchor to reuse that id inherits somebody
            # else's headset session.
            self._conn.execute(
                "DELETE FROM anchor_mappings WHERE anchor_id = ?", (anchor_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # -- external id mappings ------------------------------------------------

    def bind(self, anchor_id: str, provider_id: str, external_id: str,
             origin: str = ORIGIN_DECLARED) -> dict:
        """Bind an external system's anchor id to this one.

        An IDENTITY link only. Whatever pose the other system holds stays over
        there: its world origin is wherever that session started, and importing
        its transform as room metres would put the counter somewhere arbitrary
        with a vendor's name attached to the error.
        """
        if self.get(anchor_id) is None:
            raise AnchorError("no such anchor")
        provider_id = _clean(provider_id, "provider_id", MAX_NAME)
        external_id = _clean(external_id, "external_id", 200)
        if origin not in (ORIGIN_DECLARED, ORIGIN_IMPORTED):
            raise AnchorError("origin must be 'declared' or 'imported'")
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO anchor_mappings"
                " (anchor_id, provider_id, external_id, origin, created_ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (anchor_id, provider_id, external_id, origin, ts))
            self._conn.commit()
        return {"anchor_id": anchor_id, "provider_id": provider_id,
                "external_id": external_id, "origin": origin}

    def unbind(self, anchor_id: str, provider_id: str, external_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM anchor_mappings WHERE anchor_id = ? AND"
                " provider_id = ? AND external_id = ?",
                (anchor_id, provider_id, external_id))
            self._conn.commit()
        return cur.rowcount > 0

    def resolve(self, provider_id: str, external_id: str) -> list[Anchor]:
        """Which Wavr anchors an external id refers to.

        A LIST, not one anchor. Two headsets can each bind their own id to the
        same counter, and one id can be bound to more than one anchor by mistake
        — returning the first would hide the mistake behind a plausible answer.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT anchor_id FROM anchor_mappings"
                " WHERE provider_id = ? AND external_id = ? ORDER BY created_ts",
                (provider_id, external_id)).fetchall()
        out = [self.get(r["anchor_id"]) for r in rows]
        return [a for a in out if a is not None]

    # -- reads ---------------------------------------------------------------

    def get(self, anchor_id: str) -> Anchor | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM anchors WHERE anchor_id = ?", (anchor_id,)).fetchone()
        return self._hydrate(row) if row is not None else None

    def list(self, *, room: str | None = None) -> list[Anchor]:
        sql = "SELECT * FROM anchors"
        args: tuple = ()
        if room is not None:
            sql += " WHERE room = ?"
            args = (room,)
        sql += " ORDER BY room, name"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._hydrate(r) for r in rows]

    def by_room(self) -> dict[str, list[Anchor]]:
        out: dict[str, list[Anchor]] = {}
        for a in self.list():
            out.setdefault(a.room, []).append(a)
        return out

    def _hydrate(self, row) -> Anchor:
        with self._lock:
            maps = self._conn.execute(
                "SELECT provider_id, external_id, origin FROM anchor_mappings"
                " WHERE anchor_id = ? ORDER BY provider_id, external_id",
                (row["anchor_id"],)).fetchall()
        return Anchor(
            anchor_id=row["anchor_id"], name=row["name"], room=row["room"],
            level=int(row["level"]), kind=row["kind"],
            x=row["x"], y=row["y"], z=row["z"],
            polygon=_load_polygon(row["polygon"]),
            note=row["note"], created_ts=row["created_ts"],
            updated_ts=row["updated_ts"],
            mappings=tuple(dict(m) for m in maps))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _dump_polygon(poly) -> str:
    return ";".join(f"{x:.4f},{y:.4f}" for x, y in poly)


def _load_polygon(text) -> tuple[tuple[float, float], ...]:
    out = []
    for part in str(text or "").split(";"):
        if not part:
            continue
        try:
            a, b = part.split(",")
            out.append((float(a), float(b)))
        except (ValueError, TypeError):
            continue      # a malformed stored point is dropped, never guessed
    return tuple(out)


def summarize(anchors, known_rooms) -> dict:
    """The whole anchor picture, in the terms a person reads it in."""
    known = set(known_rooms or ())
    rows = [a.to_dict(known_rooms=known) for a in anchors]
    orphaned = [r for r in rows if r.get("orphaned")]
    return {
        "v": ANCHORS_VERSION,
        "anchors": rows,
        "count": len(rows),
        "positioned": sum(1 for r in rows if r["positioned"]),
        "orphaned": [r["anchor_id"] for r in orphaned],
        "rooms_with_anchors": sorted({r["room"] for r in rows} & known),
        "note": ("An anchor names a place. One with no coordinates is complete "
                 "— \"the kitchen counter\" is a usable answer without any "
                 "geometry behind it."
                 + (f" {len(orphaned)} point at a room that no longer exists."
                    if orphaned else "")),
    }
