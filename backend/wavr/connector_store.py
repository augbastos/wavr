"""Persistent CONNECTOR REGISTRY for the single 'Connectors & Services' egress
surface (project_wavr_connectors_vision).

Wavr is local and MUTE by default. There is exactly ONE screen that manages
anything reaching OUTWARD, and this store is its persistence. It mirrors
camera_store.py / identity_store.py: a small sqlite store sharing wavr.db
(git-ignored) but owning its own table; ":memory:" for tests; lock-guarded so it
can be driven from a thread pool.

The registry NEVER becomes a second way to ENABLE egress (that would be a silent
cloud/PII leak). For the built-in, env-gated features (narrator, HA import) it is a
MONOTONE, RESTRICT-ONLY overlay: a row can only turn a live feature OFF (a
kill-switch), never bypass the env flag. Effective state stays
`env_active AND NOT is_suppressed(id)` -- absent row => pure env => byte-identical
to today. For future generic connectors (kind='generic') the registry IS the full
enforcing gate: default enabled=0, an explicit toggle flips it, the connector's own
egress code checks is_enabled(id).

SECRETS: this table stores NON-SECRET metadata only. `config_json` references env
by NAME, never a key/token/rtsp-url-with-credentials -- nothing here is ever logged
as a secret, and `label`/`scope` are rendered via textContent (never innerHTML) at
the frontend, so a hostile label is data, not markup.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

# Fixed slugs for the built-in connectors surfaced from existing gated features.
# A registry row for one of these is an OVERLAY (kill-switch), never the source of
# truth for whether the feature is configured -- that stays the env flag / config.
BUILTIN_IDS = frozenset({"narrator", "ha-import", "ha-control", "mcp-read", "mcp-http"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connectors (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 0,
    scope       TEXT,
    config_json TEXT,
    created_ts  TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorStore:
    """SQLite-backed connector registry. Two row kinds:

      * kind='builtin' -- an OVERLAY row for an env-gated feature. Its only power is
        `enabled=0` = SUPPRESSED = kill-switch (is_suppressed True). enabled=1 clears
        the suppression; it can NEVER exceed the env flag (the chokepoint ANDs env).
      * kind='generic' -- a full gate for a future MCP/API connector. enabled=1 =
        is_enabled True = the connector's egress code may run; default 0 = off.

    Absent id => is_suppressed False AND is_enabled False, so an empty registry is
    byte-identical to today (nothing suppressed, no generic active)."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def upsert(self, id: str, kind: str, label: str, scope: str | None = None,
               config_json: str | None = None) -> dict:
        """Insert the row if absent, else refresh label/scope/config_json ONLY --
        `enabled` is preserved on conflict so an upsert never silently flips a
        kill-switch. Returns the row. `created_ts` is set once (first sighting)."""
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO connectors (id, kind, label, enabled, scope, config_json, created_ts)"
                " VALUES (?, ?, ?, 0, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   label = excluded.label,"
                "   scope = excluded.scope,"
                "   config_json = excluded.config_json",
                (id, kind, label, scope, config_json, ts),
            )
            self._conn.commit()
        return self.get(id)

    def get(self, id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id, kind, label, enabled, scope, config_json, created_ts"
                " FROM connectors WHERE id = ?",
                (id,),
            ).fetchone()
        return dict(r) if r else None

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, label, enabled, scope, config_json, created_ts"
                " FROM connectors ORDER BY created_ts, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_enabled(self, id: str, enabled: bool) -> bool:
        """Flip a row's enabled bit. Returns True if a row changed (False on an
        unknown id -- the caller upserts first for a built-in overlay)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE connectors SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM connectors WHERE id = ?", (id,))
            self._conn.commit()
            return cur.rowcount > 0

    def is_suppressed(self, id: str) -> bool:
        """Built-in kill-switch: True iff a row exists AND enabled==0. Read at each
        request in the narrator/HA-import chokepoints -> a toggle takes effect on the
        next call (REVOCABLE, no restart, no lingering grant). Absent row => False =>
        the env flag alone decides => byte-identical default."""
        row = self.get(id)
        return row is not None and row["enabled"] == 0

    def is_enabled(self, id: str) -> bool:
        """Generic full gate: True iff a row exists AND enabled==1. Absent row =>
        False => a future connector's egress code stays inert (DEFAULT-OFF)."""
        row = self.get(id)
        return row is not None and row["enabled"] == 1

    def close(self) -> None:
        self._conn.close()
