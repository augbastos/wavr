from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    name       TEXT PRIMARY KEY,
    room       TEXT NOT NULL,
    rtsp_url   TEXT NOT NULL,
    confidence REAL NOT NULL,
    mac        TEXT,
    level      INTEGER
);
"""


class CameraStore:
    """Persisted camera DEFINITIONS (name/room/rtsp_url/confidence/mac/level). Never
    stores an ON state — cameras always boot OFF; this is configuration, not runtime
    state. Never stores frames. Shares the sqlite file with Storage but owns its own
    table.

    `mac` (F3) is the optional MAC of the camera's LAN device, captured at add-time by
    resolving the rtsp host IP against the running inventory. It powers IP-drift
    detection (wavr.camera_health) so a DHCP address change can be surfaced + one-click
    rebound. It is nullable and still just configuration (already visible via
    /api/inventory) — the boot-OFF / no-frame invariant is unchanged.

    `level` (geometry fix) is the optional floor level (housemap.py's per-floor
    `level` integer) the camera's `room` lives on. A room NAME alone is ambiguous
    across a multi-floor house (e.g. two floors can both draw a "quarto"); `level`
    disambiguates which floor's same-named room this camera resolves against via
    `housemap.room_polygon(house, room, level=...)`. Nullable -- a camera with no
    stored level keeps the old (first-match-across-floors) behaviour exactly, so an
    existing single-floor house is unaffected."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # Migration-safe additive columns (mirrors device_meta._migrate): a DB created
        # before the F3 `mac` / geometry `level` columns existed lacks them; CREATE
        # TABLE IF NOT EXISTS won't add them, so ALTER them in when absent.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(cameras)")}
        if "mac" not in cols:
            self._conn.execute("ALTER TABLE cameras ADD COLUMN mac TEXT")
        if "level" not in cols:
            self._conn.execute("ALTER TABLE cameras ADD COLUMN level INTEGER")

    def add(self, name: str, room: str, rtsp_url: str, confidence: float,
            mac: str | None = None, level: int | None = None) -> None:
        self._conn.execute(
            "INSERT INTO cameras (name, room, rtsp_url, confidence, mac, level)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, room, rtsp_url, confidence, mac, level),
        )
        self._conn.commit()

    def set_url(self, name: str, rtsp_url: str) -> bool:
        """Rewrite a camera's rtsp_url (F3 rebind). Returns True if a row changed.
        Never logs the url (carries credentials)."""
        cur = self._conn.execute(
            "UPDATE cameras SET rtsp_url = ? WHERE name = ?", (rtsp_url, name))
        self._conn.commit()
        return cur.rowcount > 0

    def set_mac(self, name: str, mac: str | None) -> bool:
        """Set (or clear, with None) a camera's stored MAC. Returns True if a row
        changed. Caller is responsible for validating/normalizing `mac`."""
        cur = self._conn.execute(
            "UPDATE cameras SET mac = ? WHERE name = ?", (mac, name))
        self._conn.commit()
        return cur.rowcount > 0

    def set_level(self, name: str, level: int | None) -> bool:
        """Set (or clear, with None) a camera's stored floor LEVEL -- disambiguates
        which floor's same-named room this camera localizes against
        (`housemap.room_polygon`'s `level` param). Returns True if a row changed.
        Caller is responsible for validating `level` against the house map."""
        cur = self._conn.execute(
            "UPDATE cameras SET level = ? WHERE name = ?", (level, name))
        self._conn.commit()
        return cur.rowcount > 0

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence, mac, level FROM cameras ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> dict | None:
        r = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence, mac, level FROM cameras WHERE name = ?",
            (name,),
        ).fetchone()
        return dict(r) if r else None

    def delete(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM cameras WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
