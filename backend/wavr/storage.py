from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import suppress

from wavr.roomstate import RoomState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_states (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT    NOT NULL,
    occupied    INTEGER NOT NULL,
    confidence  REAL    NOT NULL,
    sources     TEXT    NOT NULL,   -- JSON
    explanation TEXT    NOT NULL,
    ts          TEXT    NOT NULL
);
"""


class Storage:
    """Persists ONLY coarse derived RoomState (occupancy / confidence / per-modality
    sources / explanation). Per ADR-0002, vital-sign estimates and x/y targets are
    LIVE-ONLY and never touch disk — there is no `vitals` or `targets` column, and any
    legacy `vitals` column from an older db is dropped on open (purging old biometric
    history). Never stores raw frames or CSI.

    Writes/reads are guarded by a lock so the connection can be driven from a thread
    pool (`asyncio.to_thread`) without keeping the fsync on the event loop.
    """

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # WAL + synchronous=NORMAL: commits no longer fsync the main db file
        # on every insert_state(), only at WAL-checkpoint boundaries -- fewer/
        # smaller flushes, the SD-card-wear/latency win on the G9. Acceptable
        # durability trade for this table: room_states is a DERIVED history
        # (the debounced occupancy verdict is computed live and is what the
        # UI treats as source of truth -- this table is only ever read back
        # for /api/history-style trends), so losing the last commit or two on
        # an unclean shutdown costs a few seconds of trend history, never a
        # live decision. :memory: databases (used throughout the test suite)
        # don't support WAL; suppressed same as before.
        with suppress(sqlite3.OperationalError):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_drop_vitals()
        self._conn.commit()

    def _migrate_drop_vitals(self) -> None:
        # ADR-0002: purge any legacy biometric column left by an older schema.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(room_states)")}
        if "vitals" in cols:
            try:
                self._conn.execute("ALTER TABLE room_states DROP COLUMN vitals")
            except sqlite3.OperationalError:
                pass  # sqlite < 3.35 without DROP COLUMN; new rows simply omit vitals

    def insert_state(self, rs: RoomState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_states (room, occupied, confidence, sources, explanation, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (rs.room, int(rs.occupied), rs.confidence,
                 json.dumps(rs.sources), rs.explanation, rs.ts),
            )
            self._conn.commit()

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT room, occupied, confidence, sources, explanation, ts"
                " FROM room_states ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    @staticmethod
    def _to_dict(r: sqlite3.Row) -> dict:
        return {
            "room": r["room"],
            "occupied": bool(r["occupied"]),
            "confidence": r["confidence"],
            "sources": json.loads(r["sources"]),
            "explanation": r["explanation"],
            "ts": r["ts"],
        }

    def close(self) -> None:
        self._conn.close()
