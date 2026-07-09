"""Core Panel admin unlock PIN (gates wake-to-dashboard on the Core's own panel).

Stores ONLY a salted hash of the operator's PIN -- NEVER the plaintext.
Mirrors devices.py / camera_store.py: a small sqlite store sharing wavr.db
(git-ignored), injectable path (":memory:" for tests), lock-guarded for
thread-pool use.

Unlike devices.py's device tokens (256-bit random secrets, where a plain fast
hash is fine because there's nothing to brute-force), a PIN is short and
low-entropy, so it is stretched with stdlib `hashlib.pbkdf2_hmac` (no new
dependency) over a per-install random salt -- slow enough to blunt an offline
guess of a leaked db, fast enough for an interactive unlock. `verify` compares
in constant time (`hmac.compare_digest`).

Single row (`id = 1`): there is exactly one Core Panel PIN per install.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

# Cost factor for pbkdf2-hmac-sha256. High enough to meaningfully slow offline
# brute force of a short numeric PIN; low enough (~tens of ms) to stay
# interactive for a real unlock attempt.
ITERATIONS = 200_000
_SALT_BYTES = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_pin (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    salt_hex   TEXT NOT NULL,
    hash_hex   TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    updated_ts TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


class PinStore:
    """SQLite-backed single-PIN store. `set_pin` persists a FRESH random salt +
    hash (a re-set never reuses the old salt); `verify` never raises -- an
    unset PIN or a malformed row both verify False, never a crash on the
    unlock path."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def set_pin(self, pin: str) -> None:
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _derive(pin, salt, ITERATIONS)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO core_pin (id, salt_hex, hash_hex, iterations, updated_ts)"
                " VALUES (1, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   salt_hex = excluded.salt_hex,"
                "   hash_hex = excluded.hash_hex,"
                "   iterations = excluded.iterations,"
                "   updated_ts = excluded.updated_ts",
                (salt.hex(), digest.hex(), ITERATIONS, ts),
            )
            self._conn.commit()

    def is_set(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM core_pin WHERE id = 1").fetchone()
        return row is not None

    def clear(self) -> None:
        """Remove the Core Panel PIN entirely -- the 'no lock' state. Idempotent
        (a no-op when no PIN is set). After this, is_set() is False and verify()
        returns False for any input, so the panel wakes straight to the dashboard
        with no unlock gate. The DELETE /api/core/pin route is gated identically
        to the setter (require_local + admin scope), so only a local admin can
        remove the lock."""
        with self._lock:
            self._conn.execute("DELETE FROM core_pin WHERE id = 1")
            self._conn.commit()

    def verify(self, pin: str) -> bool:
        """Constant-time compare against the stored salted hash. False (never
        raises) when no PIN has been set yet, or the row is malformed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT salt_hex, hash_hex, iterations FROM core_pin WHERE id = 1"
            ).fetchone()
        if row is None:
            return False
        try:
            salt = bytes.fromhex(row["salt_hex"])
            digest = _derive(pin, salt, row["iterations"])
            return hmac.compare_digest(digest.hex(), row["hash_hex"])
        except Exception:
            return False

    def close(self) -> None:
        self._conn.close()
