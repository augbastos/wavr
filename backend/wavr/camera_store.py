from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    name       TEXT PRIMARY KEY,
    room       TEXT NOT NULL,
    rtsp_url   TEXT NOT NULL,
    confidence REAL NOT NULL
);
"""


class CameraStore:
    """Persisted camera DEFINITIONS (name/room/rtsp_url/confidence). Never stores an
    ON state — cameras always boot OFF; this is configuration, not runtime state.
    Never stores frames. Shares the sqlite file with Storage but owns its own table."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, name: str, room: str, rtsp_url: str, confidence: float) -> None:
        self._conn.execute(
            "INSERT INTO cameras (name, room, rtsp_url, confidence) VALUES (?, ?, ?, ?)",
            (name, room, rtsp_url, confidence),
        )
        self._conn.commit()

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence FROM cameras ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> dict | None:
        r = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence FROM cameras WHERE name = ?", (name,)
        ).fetchone()
        return dict(r) if r else None

    def delete(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM cameras WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
