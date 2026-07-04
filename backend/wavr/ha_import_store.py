"""Persisted per-MAC identity IMPORTED from the user's local Home Assistant
device registry (A4.1). Mirrors wavr.device_meta's shape (a small sqlite store,
injectable path, ":memory:" for tests) but holds the HA-sourced make/model/os/
device_type that `wavr.netinventory_service` folds back into every scan as the
recog `ha` signal (wavr.recog, A4.0).

WHY A STORE (not just a per-request result): the HA import is user-triggered and
on-demand (never a timer), but its output must SURVIVE restart and feed every
subsequent LAN scan -- otherwise a device identified via HA would go anonymous
again on the next reboot. Keyed by MAC because recog fuses per-MAC.

PRIVACY / PUBLIC-REPO SAFETY: this table lives in `wavr.db`, which is
git-ignored (`.gitignore`: `wavr.db*`) -- HA-derived home data (device names,
MACs, models) is exactly the PII-leak class that must never be committed to this
public AGPL repo. The Home Assistant long-lived token is NEVER stored here (it
stays in the env/`.env`, read only at import time); only the resulting device
identity fields are persisted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from wavr.device_meta import normalize_mac

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ha_devices (
    mac         TEXT PRIMARY KEY,
    device_type TEXT,
    make        TEXT,
    model       TEXT,
    os          TEXT,
    imported_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HAImportStore:
    """Persisted {mac -> {device_type, make, model, os, imported_at}} imported
    from Home Assistant. `upsert` writes one device; `signals()` returns the
    shape recog's `ha` signal expects, keyed by MAC, for the scan loop to fold
    in. All writes are plain upserts (idempotent re-import overwrites)."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert(self, mac: str, device_type: str | None = None,
               make: str | None = None, model: str | None = None,
               os: str | None = None) -> None:
        """Persist one HA-imported device identity, keyed by MAC. Raises
        ValueError (via normalize_mac) on a malformed MAC -- the caller
        (wavr.ha_import) skips such rows rather than storing garbage."""
        mac = normalize_mac(mac)
        self._conn.execute(
            """INSERT INTO ha_devices (mac, device_type, make, model, os, imported_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(mac) DO UPDATE SET
                   device_type = excluded.device_type,
                   make        = excluded.make,
                   model       = excluded.model,
                   os          = excluded.os,
                   imported_at = excluded.imported_at""",
            (mac, device_type, make, model, os, _now()),
        )
        self._conn.commit()

    def all(self) -> dict:
        rows = self._conn.execute(
            "SELECT mac, device_type, make, model, os, imported_at FROM ha_devices"
        ).fetchall()
        return {
            r["mac"]: {"device_type": r["device_type"], "make": r["make"],
                       "model": r["model"], "os": r["os"],
                       "imported_at": r["imported_at"]}
            for r in rows
        }

    def signals(self) -> dict:
        """All imported identities as {mac: {device_type, make, model, os}} --
        the per-MAC `ha` signal dict wavr.recog.recognize consumes. Rows carry
        only non-empty fields so recog never sees empty strings as opinions."""
        rows = self._conn.execute(
            "SELECT mac, device_type, make, model, os FROM ha_devices"
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            sig = {k: r[k] for k in ("device_type", "make", "model", "os") if r[k]}
            if sig:
                out[r["mac"]] = sig
        return out

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM ha_devices").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
