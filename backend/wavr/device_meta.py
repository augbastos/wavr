"""Persistent per-MAC device metadata (Feature A): a custom name + first-seen /
last-seen timestamps that survive restarts. Mirrors wavr.camera_store's shape
(a small sqlite store, injectable path, ":memory:" for tests) but is keyed by
MAC instead of camera name.

Naming a device is NOT sensitive (a MAC is already visible in the /api/inventory
response) but it IS a write, so the HTTP route that calls set_name() is gated by
the same require_local CSRF guard as every other state-changing route (wired in
app.py, same rule as the camera routes).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_meta (
    mac        TEXT PRIMARY KEY,
    name       TEXT,
    first_seen TEXT,
    last_seen  TEXT
);
"""

_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_NAME_LEN = 64


def normalize_mac(mac: str) -> str:
    """Lowercase colon-form MAC, accepting either '-' or ':' separators (same
    convention as netinventory/netutils). Raises ValueError on anything that
    isn't a well-formed 6-octet MAC -- callers (the API route) turn that into a
    400 rather than letting a garbage string reach the DB."""
    norm = (mac or "").strip().replace("-", ":").lower()
    if not _MAC_RE.match(norm):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return norm


def sanitize_name(name: str) -> str:
    """Trim + strip control characters from a device name. The frontend renders
    names via textContent (XSS-safe there already) -- this is defense-in-depth
    against garbage/oversized values reaching the DB. Raises ValueError if the
    cleaned result is empty or exceeds MAX_NAME_LEN characters."""
    cleaned = _CONTROL_CHARS_RE.sub("", name or "").strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    if len(cleaned) > MAX_NAME_LEN:
        raise ValueError(f"name must be at most {MAX_NAME_LEN} characters")
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceMeta:
    """Persisted per-MAC metadata: {name, first_seen, last_seen}.

    `seen(mac)` is called on every inventory-scan sighting -- it sets
    first_seen the first time a MAC is observed and bumps last_seen on every
    call after that, without touching a previously-set name. `set_name` is the
    only write reachable from the HTTP API and never touches first_seen/
    last_seen. Both are plain upserts so callers don't need to pre-check
    existence."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def seen(self, mac: str) -> None:
        mac = normalize_mac(mac)
        now = _now()
        self._conn.execute(
            """INSERT INTO device_meta (mac, name, first_seen, last_seen)
               VALUES (?, NULL, ?, ?)
               ON CONFLICT(mac) DO UPDATE SET last_seen = excluded.last_seen""",
            (mac, now, now),
        )
        self._conn.commit()

    def set_name(self, mac: str, name: str) -> dict:
        mac = normalize_mac(mac)
        clean = sanitize_name(name)
        self._conn.execute(
            """INSERT INTO device_meta (mac, name, first_seen, last_seen)
               VALUES (?, ?, NULL, NULL)
               ON CONFLICT(mac) DO UPDATE SET name = excluded.name""",
            (mac, clean),
        )
        self._conn.commit()
        return self.get(mac)

    def get(self, mac: str) -> dict | None:
        mac = normalize_mac(mac)
        r = self._conn.execute(
            "SELECT mac, name, first_seen, last_seen FROM device_meta WHERE mac = ?",
            (mac,),
        ).fetchone()
        return dict(r) if r else None

    def all(self) -> dict:
        rows = self._conn.execute(
            "SELECT mac, name, first_seen, last_seen FROM device_meta"
        ).fetchall()
        return {
            r["mac"]: {"name": r["name"], "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows
        }

    def close(self) -> None:
        self._conn.close()
