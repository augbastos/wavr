from __future__ import annotations

import json
import sqlite3

from wavr.roomstate import RoomState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_states (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT    NOT NULL,
    occupied    INTEGER NOT NULL,
    confidence  REAL    NOT NULL,
    vitals      TEXT    NOT NULL,   -- JSON
    sources     TEXT    NOT NULL,   -- JSON
    explanation TEXT    NOT NULL,
    ts          TEXT    NOT NULL
);
"""


class Storage:
    """Persists ONLY derived RoomState. Never stores raw frames or CSI."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert_state(self, rs: RoomState) -> None:
        self._conn.execute(
            "INSERT INTO room_states (room, occupied, confidence, vitals, sources, explanation, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rs.room, int(rs.occupied), rs.confidence, json.dumps(rs.vitals),
             json.dumps(rs.sources), rs.explanation, rs.ts),
        )
        self._conn.commit()

    def recent(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT room, occupied, confidence, vitals, sources, explanation, ts"
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
            "vitals": json.loads(r["vitals"]),
            "sources": json.loads(r["sources"]),
            "explanation": r["explanation"],
            "ts": r["ts"],
        }

    def close(self) -> None:
        self._conn.close()
