"""Append-only per-room occupancy history (Build A4 -- "the house memory"): the timeline
``wavr.presence_report`` itself documents as missing. ``device_meta.py`` keeps only ONE
first/last_seen per MAC (overwritten on every sighting) so no real occupancy timeline is
reconstructible from it. This module IS that timeline: a compact, EDGE-TRIGGERED snapshot
``{room, occupied, person_count, confidence, ts}`` appended only when a room's fused state
actually changes (mirrors ``fusion.FusionEngine``'s own dwell-debounced ``occupied`` and the
``app.py`` ``_refuse_once`` change-detection heuristic -- never a row per re-fuse tick).

DERIVED ONLY, same discipline as ``wavr.storage`` (ADR-0002): the logged fields are exactly
what a ``RoomState`` already exposes as coarse/derived (occupied, confidence) plus
``person_count`` (A1, itself an honest derived count, never fabricated). Raw ``vitals``/
``targets``/``sources``/``identities`` are NEVER written here. Identity-in-history (e.g. "who
was home when") is a SEPARATE, opt-in feature gated on ``identity_enabled`` -- deliberately
not layered onto this module.

Mirrors ``wavr.device_meta``'s shape: a small sqlite store, injectable path, ``":memory:"``
for tests. Bounded growth by construction: ``retention_days`` caps how long a row lives (a
bulk DELETE runs after every insert) and edge-triggering keeps the insert rate low (a room's
occupancy flips a handful of times a day, not once per fusion tick).

On top of the raw log this derives what ``presence_report.py`` explicitly could not:
  * ``timeline()``    -- ordered per-room history over a time range,
  * ``routine()``      -- an hourly occupancy-PROBABILITY baseline per room, TIME-WEIGHTED
    across the trailing N weeks (each logged row is treated as holding until the next row
    for that room, or "now" for the most recent one, so an edge-triggered log with irregular
    gaps still yields an honest per-hour duty cycle instead of a naive per-row average),
  * ``is_unusual()``   -- "is the CURRENT reading unusual for this hour", by comparing it to
    the routine baseline, with an explicit ``None`` ("insufficient_data") when there isn't
    enough history yet -- never a fabricated verdict on day one.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DEFAULT_RETENTION_DAYS = 60
DEFAULT_ROUTINE_WEEKS = 4
DEFAULT_MIN_ROUTINE_SAMPLES = 3  # distinct days of coverage needed before an hour is "trusted"
DEFAULT_UNUSUAL_THRESHOLD = 0.5  # |observed(0/1) - baseline_probability| beyond this = unusual

# Edge-trigger gate: mirrors app.py's own on-change heuristic for room_states (occupied
# flipped OR confidence moved by >= 1%); person_count is an exact-match int so ANY change
# (including None <-> a number) counts.
_CONFIDENCE_EPS = 0.01

_SCHEMA = """
CREATE TABLE IF NOT EXISTS occupancy_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT    NOT NULL,
    occupied     INTEGER NOT NULL,
    person_count INTEGER,
    confidence   REAL    NOT NULL,
    ts           TEXT    NOT NULL
);
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_occupancy_log_room_ts ON occupancy_log (room, ts);"


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class OccupancyLog:
    """Persisted, edge-triggered per-room occupancy history. See module docstring."""

    def __init__(self, path: str = "wavr.db",
                 retention_days: float | None = DEFAULT_RETENTION_DAYS):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Guards every connection access. `append_if_changed` is driven off the fusion
        # publish path via `asyncio.to_thread` (app.py), which can run concurrently for
        # different rooms/events on the shared threadpool -- a bare sqlite3.Connection
        # (even with check_same_thread=False) is NOT safe under concurrent execute()
        # calls from multiple threads. Mirrors wavr.storage.Storage's own lock.
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(_INDEX)
            self._conn.commit()
        self.retention_days = retention_days
        # In-memory last-row-per-room cache (mirrors FusionEngine's own per-room state
        # dicts): avoids a SELECT on every single published RoomState just to decide
        # whether THIS one is a no-op repeat. Warmed once from the DB so a restart still
        # dedupes correctly against whatever was already logged.
        self._last: dict[str, dict] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT room, occupied, person_count, confidence, ts FROM occupancy_log"
                " WHERE id IN (SELECT MAX(id) FROM occupancy_log GROUP BY room)").fetchall()
        for r in rows:
            self._last[r["room"]] = {"occupied": bool(r["occupied"]),
                                      "person_count": r["person_count"],
                                      "confidence": r["confidence"], "ts": r["ts"]}

    # ---- write ----------------------------------------------------------------------

    def append_if_changed(self, room: str, occupied: bool, confidence: float,
                           person_count: int | None, ts: str) -> bool:
        """Append a snapshot ONLY if it differs from the last logged row for this room
        (occupied flipped, person_count changed, or confidence moved by >= 1%) -- so a
        room sitting steady at 82% occupied for hours logs exactly once, not once per
        fusion tick. Returns True iff a row was actually inserted. Safe to call on every
        published RoomState; the dedup is entirely internal (callers never need their own
        change-detection)."""
        prev = self._last.get(room)
        changed = (
            prev is None
            or prev["occupied"] != bool(occupied)
            or prev["person_count"] != person_count
            or abs(prev["confidence"] - confidence) >= _CONFIDENCE_EPS
        )
        if not changed:
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO occupancy_log (room, occupied, person_count, confidence, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (room, int(bool(occupied)), person_count, confidence, ts),
            )
            self._conn.commit()
        self._last[room] = {"occupied": bool(occupied), "person_count": person_count,
                             "confidence": confidence, "ts": ts}
        self._prune()
        return True

    def _prune(self) -> None:
        """Delete rows older than ``retention_days``. Runs after every insert -- cheap,
        because inserts themselves are edge-triggered (a handful a day per room, never
        one per re-fuse tick). ``retention_days`` <= 0 or None disables pruning."""
        if not self.retention_days or self.retention_days <= 0:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            self._conn.execute("DELETE FROM occupancy_log WHERE ts < ?", (cutoff,))
            self._conn.commit()

    # ---- read -------------------------------------------------------------------------

    def timeline(self, room: str | None = None, *, start: str | None = None,
                 end: str | None = None, limit: int = 1000) -> list[dict]:
        """Ordered (oldest -> newest) history, optionally filtered by ``room`` and/or an
        ISO-8601 half-open ``[start, end)`` time range. ``limit`` is clamped defensively
        (never an unbounded table dump off a read route)."""
        limit = max(1, min(int(limit), 5000))
        clauses, params = [], []
        if room is not None:
            clauses.append("room = ?")
            params.append(room)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT room, occupied, person_count, confidence, ts FROM occupancy_log"
                f"{where} ORDER BY ts DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    def rooms(self) -> list[str]:
        """Every room that has ever logged a row."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT room FROM occupancy_log ORDER BY room").fetchall()
        return [r["room"] for r in rows]

    def routine(self, room: str, *, weeks: float = DEFAULT_ROUTINE_WEEKS,
                now: datetime | None = None,
                min_samples: int = DEFAULT_MIN_ROUTINE_SAMPLES) -> dict:
        """Hourly occupancy-probability baseline for ``room`` over the trailing ``weeks``.
        TIME-WEIGHTED: each logged row is treated as holding until the next row for the
        same room (or ``now`` for the most recent row), so the irregular gaps an
        edge-triggered log produces still yield an honest per-hour duty cycle rather than a
        naive per-row average. ``hours[h]["samples"]`` counts distinct CALENDAR DAYS that
        contributed any coverage to hour ``h`` -- the trust signal consumed by
        ``is_unusual()`` (an hour needs ``min_samples`` distinct days before its baseline is
        used for anything)."""
        now = now or datetime.now(timezone.utc)
        # Defensive clamp (a caller-supplied `weeks` reaches here straight off the
        # /api/occupancy/routine query string): a zero/negative value would put
        # `window_start` AT or AFTER `now`, silently returning an all-"no data" result
        # instead of raising -- clamp to a sane [1 hour, 2 years] range so the query stays
        # honest without ever needing to 400 on a slightly-odd input.
        weeks = max(1 / 168, min(float(weeks), 104.0))
        window_start = now - timedelta(weeks=weeks)
        rows = self.timeline(room, start=window_start.isoformat(), end=now.isoformat(),
                              limit=5000)
        # Whatever was already true when the window opened (the last row strictly BEFORE
        # window_start) seeds the first interval -- otherwise a room that changed state
        # once, long ago, and never since would show "no data" for the whole window.
        with self._lock:
            prior = self._conn.execute(
                "SELECT room, occupied, person_count, confidence, ts FROM occupancy_log"
                " WHERE room = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (room, window_start.isoformat()),
            ).fetchone()
        segments: list[dict] = ([self._to_dict(prior)] if prior is not None else []) + rows
        if not segments:
            return {"room": room, "weeks": weeks, "hours": [
                {"hour": h, "probability": None, "samples": 0, "trusted": False}
                for h in range(24)
            ]}

        occ_seconds: dict[int, float] = defaultdict(float)
        total_seconds: dict[int, float] = defaultdict(float)
        day_hours_seen: dict[int, set] = defaultdict(set)

        for i, seg in enumerate(segments):
            seg_start = max(_parse(seg["ts"]), window_start)
            seg_end = _parse(segments[i + 1]["ts"]) if i + 1 < len(segments) else now
            seg_end = min(seg_end, now)
            if seg_end <= seg_start:
                continue
            self._accumulate_hours(seg_start, seg_end, bool(seg["occupied"]),
                                    occ_seconds, total_seconds, day_hours_seen)

        hours = []
        for h in range(24):
            samples = len(day_hours_seen.get(h, ()))
            tot = total_seconds.get(h, 0.0)
            prob = (occ_seconds.get(h, 0.0) / tot) if tot > 0 else None
            hours.append({
                "hour": h,
                "probability": round(prob, 3) if prob is not None else None,
                "samples": samples,
                "trusted": samples >= min_samples,
            })
        return {"room": room, "weeks": weeks, "hours": hours}

    @staticmethod
    def _accumulate_hours(seg_start: datetime, seg_end: datetime, occupied: bool,
                           occ_seconds: dict, total_seconds: dict,
                           day_hours_seen: dict) -> None:
        """Split ``[seg_start, seg_end)`` into per-calendar-hour slices and accumulate
        occupied/total seconds plus which (day, hour) pairs were covered. Walks hour-by-hour
        rather than assuming same-day, so a segment spanning midnight (or several days, for
        a room that hasn't changed state in a while) still splits correctly."""
        cur = seg_start
        while cur < seg_end:
            hour_start = cur.replace(minute=0, second=0, microsecond=0)
            next_hour = hour_start + timedelta(hours=1)
            slice_end = min(seg_end, next_hour)
            dur = (slice_end - cur).total_seconds()
            h = cur.hour
            total_seconds[h] = total_seconds.get(h, 0.0) + dur
            if occupied:
                occ_seconds[h] = occ_seconds.get(h, 0.0) + dur
            day_hours_seen.setdefault(h, set()).add(cur.date())
            cur = slice_end

    def is_unusual(self, room: str, occupied_now: bool, *, at: datetime | None = None,
                    weeks: float = DEFAULT_ROUTINE_WEEKS,
                    min_samples: int = DEFAULT_MIN_ROUTINE_SAMPLES,
                    threshold: float = DEFAULT_UNUSUAL_THRESHOLD) -> dict:
        """Compare ``occupied_now`` against this room's routine baseline for the CURRENT
        hour. Returns ``{"unusual": bool|None, "baseline_probability": float|None,
        "samples": int, "hour": int}``. ``unusual`` is ``None`` (never ``False``) when the
        baseline doesn't have ``min_samples`` distinct days of coverage for this hour yet --
        an honest "don't know" rather than a fabricated verdict on day one."""
        at = at or datetime.now(timezone.utc)
        base = self.routine(room, weeks=weeks, now=at, min_samples=min_samples)
        cell = base["hours"][at.hour]
        if not cell["trusted"] or cell["probability"] is None:
            return {"unusual": None, "baseline_probability": cell["probability"],
                    "samples": cell["samples"], "hour": at.hour}
        observed = 1.0 if occupied_now else 0.0
        unusual = abs(observed - cell["probability"]) > threshold
        return {"unusual": unusual, "baseline_probability": cell["probability"],
                "samples": cell["samples"], "hour": at.hour}

    @staticmethod
    def _to_dict(r) -> dict:
        return {"room": r["room"], "occupied": bool(r["occupied"]),
                "person_count": r["person_count"], "confidence": r["confidence"],
                "ts": r["ts"]}

    def close(self) -> None:
        # Acquire the same lock every other method uses: closing while a background
        # asyncio.to_thread call is mid-`execute`/`commit` on this connection (from a
        # different thread) is what actually hung shutdown -- see the class docstring.
        with self._lock:
            self._conn.close()
