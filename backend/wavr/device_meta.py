"""Persistent per-MAC device metadata (Feature A): a custom name + first-seen /
last-seen timestamps + an optional user device-type pin, all surviving
restarts. Mirrors wavr.camera_store's shape (a small sqlite store, injectable
path, ":memory:" for tests) but is keyed by MAC instead of camera name.

The device-type pin is the owner's manual override ("this IS a camera") -- it
is the HIGHEST-precedence signal in wavr.recog's fusion and must be one of the
fixed wavr.data.deviceclass.DEVICE_TYPES values. Purely local; there is no
feedback-to-anywhere loop.

Naming/pinning a device is NOT sensitive (a MAC is already visible in the
/api/inventory response) but it IS a write, so the HTTP routes that call
set_name()/set_type() are gated by the same require_local CSRF guard as every
other state-changing route (wired in app.py, same rule as the camera routes).
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

from wavr.data.deviceclass import DEVICE_TYPES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_meta (
    mac         TEXT PRIMARY KEY,
    name        TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    device_type TEXT
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


def sanitize_device_type(device_type) -> str | None:
    """Validate a user device-type pin against the fixed taxonomy. None or an
    empty/whitespace string means "clear the pin" and returns None; anything
    else must be one of DEVICE_TYPES (case-insensitive) or ValueError."""
    if device_type is None:
        return None
    cleaned = str(device_type).strip().lower()
    if not cleaned:
        return None
    if cleaned not in DEVICE_TYPES:
        raise ValueError(
            f"device_type must be one of {', '.join(DEVICE_TYPES)}")
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceMeta:
    """Persisted per-MAC metadata: {name, first_seen, last_seen, device_type}.

    `seen(mac)` is called on every inventory-scan sighting -- it sets
    first_seen the first time a MAC is observed and bumps last_seen on every
    call after that, without touching a previously-set name or pin.
    `set_name`/`set_type` are the only writes reachable from the HTTP API and
    never touch first_seen/last_seen. All are plain upserts so callers don't
    need to pre-check existence.

    `seen_many`/`get_many` are the BATCH forms of `seen`/`get`: one sqlite
    round-trip for many MACs instead of one per MAC. `get_many` is what GET
    /api/inventory (polled every 15s) uses to avoid an N+1 SELECT-per-device;
    `seen_many` is what the scan loop uses so one scan cycle is one commit,
    not one per observed device (matters for SD-card write-wear on the G9).

    All connection access is guarded by a lock (mirrors wavr.storage.Storage)
    so `seen_many` can be driven from a thread pool (`asyncio.to_thread`,
    keeping a slow disk off the event loop) without racing a same-loop
    `get`/`set_name`/`set_type` call on the same connection."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # WAL + synchronous=NORMAL (same tuning as wavr.storage.Storage):
        # commits no longer fsync the main db file on every write, only at
        # WAL-checkpoint boundaries -- fewer/smaller flushes, which is the
        # SD-card-wear/latency win on the G9. This trades "the last commit or
        # two survives a power-cut" for throughput; acceptable here because
        # device_meta is entirely re-derivable from the next scan (first_seen
        # is the only thing a lost commit could set back, and only by one
        # scan interval) -- never the source of truth for anything live.
        # :memory: databases (used throughout the test suite) don't support
        # WAL; suppressed the same way Storage already does.
        with suppress(sqlite3.OperationalError):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # Migration-safe additive column: DBs created before the device-type
        # pin existed lack the column; CREATE TABLE IF NOT EXISTS won't add it.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(device_meta)")}
        if "device_type" not in cols:
            self._conn.execute(
                "ALTER TABLE device_meta ADD COLUMN device_type TEXT")

    def seen(self, mac: str) -> None:
        mac = normalize_mac(mac)
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_meta (mac, name, first_seen, last_seen)
                   VALUES (?, NULL, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       first_seen = COALESCE(device_meta.first_seen, excluded.first_seen),
                       last_seen  = excluded.last_seen""",
                (mac, now, now),
            )
            self._conn.commit()

    def seen_many(self, macs) -> None:
        """Batch form of `seen()`: every MAC in `macs` upserted in ONE sqlite
        transaction (one commit for the whole scan cycle instead of one per
        device). Same per-MAC semantics as calling `seen()` once each
        (first_seen set only the first time, last_seen always bumped) --
        every MAC in this call shares the single timestamp taken at call
        time. Malformed MACs are skipped rather than raising: this is fed
        Device.mac values already produced by the scan pipeline, not raw
        user input, and one bad entry must not drop the rest of the batch."""
        now = _now()
        norm = []
        for mac in macs:
            with suppress(ValueError):
                norm.append(normalize_mac(mac))
        if not norm:
            return
        norm = list(dict.fromkeys(norm))   # de-dupe, preserve order
        with self._lock:
            self._conn.executemany(
                """INSERT INTO device_meta (mac, name, first_seen, last_seen)
                   VALUES (?, NULL, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       first_seen = COALESCE(device_meta.first_seen, excluded.first_seen),
                       last_seen  = excluded.last_seen""",
                [(mac, now, now) for mac in norm],
            )
            self._conn.commit()

    def set_name(self, mac: str, name: str) -> dict:
        mac = normalize_mac(mac)
        clean = sanitize_name(name)
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_meta (mac, name, first_seen, last_seen)
                   VALUES (?, ?, NULL, NULL)
                   ON CONFLICT(mac) DO UPDATE SET name = excluded.name""",
                (mac, clean),
            )
            self._conn.commit()
        return self.get(mac)

    def set_type(self, mac: str, device_type) -> dict:
        """Pin (or clear, with None/"") the user device-type override for a
        MAC. The pin is wavr.recog's highest-precedence signal. Raises
        ValueError on a malformed MAC or a value outside the taxonomy."""
        mac = normalize_mac(mac)
        clean = sanitize_device_type(device_type)
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_meta (mac, device_type, first_seen, last_seen)
                   VALUES (?, ?, NULL, NULL)
                   ON CONFLICT(mac) DO UPDATE SET device_type = excluded.device_type""",
                (mac, clean),
            )
            self._conn.commit()
        return self.get(mac)

    def get(self, mac: str) -> dict | None:
        mac = normalize_mac(mac)
        with self._lock:
            r = self._conn.execute(
                "SELECT mac, name, first_seen, last_seen, device_type"
                " FROM device_meta WHERE mac = ?",
                (mac,),
            ).fetchone()
        return dict(r) if r else None

    def get_many(self, macs) -> dict:
        """Batch form of `get()`: one `SELECT ... WHERE mac IN (...)` for every
        MAC in `macs` instead of one round-trip per MAC (the N+1 GET
        /api/inventory was doing, polled every 15s). Returns {mac: {name,
        first_seen, last_seen, device_type}} -- MACs not found (or malformed)
        are simply absent, same as `get()` returning None for them."""
        norm = []
        for mac in macs:
            with suppress(ValueError):
                norm.append(normalize_mac(mac))
        if not norm:
            return {}
        norm = list(dict.fromkeys(norm))   # de-dupe
        placeholders = ",".join("?" * len(norm))
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, name, first_seen, last_seen, device_type"
                f" FROM device_meta WHERE mac IN ({placeholders})",
                norm,
            ).fetchall()
        return {
            r["mac"]: {"name": r["name"], "first_seen": r["first_seen"],
                       "last_seen": r["last_seen"], "device_type": r["device_type"]}
            for r in rows
        }

    def all(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, name, first_seen, last_seen, device_type FROM device_meta"
            ).fetchall()
        return {
            r["mac"]: {"name": r["name"], "first_seen": r["first_seen"],
                       "last_seen": r["last_seen"], "device_type": r["device_type"]}
            for r in rows
        }

    def type_pins(self) -> dict:
        """All user device-type pins as {mac: device_type} (pinned MACs only)
        -- the shape wavr.netinventory's `pins` parameter expects."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, device_type FROM device_meta WHERE device_type IS NOT NULL"
            ).fetchall()
        return {r["mac"]: r["device_type"] for r in rows}

    def close(self) -> None:
        self._conn.close()
