"""Persistent CONNECTOR REGISTRY for the single 'Connectors & Services' egress
surface (project_wavr_connectors_vision).

Wavr is local and MUTE by default. There is exactly ONE screen that manages
anything reaching OUTWARD, and this store is its persistence. It mirrors
camera_store.py / identity_store.py: a small sqlite store sharing wavr.db
(git-ignored) but owning its own table; ":memory:" for tests; lock-guarded so it
can be driven from a thread pool.

The registry is the single admin PERMISSION BROKER for outward-facing features
(project_wavr_connectors_vision): the one screen where an admin turns things ON, all
default-OFF. For the built-in, env-gated features (narrator, HA import, MCP-HTTP) a
row is a persisted admin OVERRIDE that WINS over the env flag: enabled=1 = a
DELIBERATE enable that force-activates the gate even when the env flag is unset;
enabled=0 = a DELIBERATE disable (kill-switch) even when the env flag is on. The
effective gate is `effective_active(id, env_active)` -- override when a row exists,
else the env flag. An ABSENT row => pure env => byte-identical to today, so an empty
registry egresses nothing. The override enables the GATE only; a connector cannot
actually egress until it is separately configured (a provider key, HA creds, an HTTP
mount) -- the chokepoints prove that readiness independently, so an
enabled-but-unconfigured connector reaches nowhere. Writing an override is gated at
the route to the loopback admin (require_local CSRF + admin scope). For future generic
connectors (kind='generic') the registry IS the full enforcing gate: default
enabled=0, an explicit toggle flips it, the connector's own egress code checks
is_enabled(id).

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

      * kind='builtin' -- a persisted admin OVERRIDE row for an env-gated feature.
        enabled=1 = override "on" = a DELIBERATE enable that force-activates the gate
        even when the env flag is unset; enabled=0 = override "off" = kill-switch
        (is_suppressed True) even when the env flag is on. See override() /
        effective_active(): the override WINS over the env flag when a row exists.
      * kind='generic' -- a full gate for a future MCP/API connector. enabled=1 =
        is_enabled True = the connector's egress code may run; default 0 = off.

    Absent id => override None => the env flag alone decides, so an empty registry is
    byte-identical to today (nothing suppressed, nothing force-enabled, no generic
    active). An override enables the GATE only; actual egress still requires the
    feature to be configured, proven separately at each chokepoint."""

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

    def override(self, id: str) -> str | None:
        """The persisted admin OVERRIDE for a built-in connector, or None when absent.

          * "on"  -- a row with enabled==1: a DELIBERATE admin ENABLE. It force-enables
            the feature's gate even when the env flag is unset (subject to the feature
            actually being configured -- an enabled-but-unconfigured connector still
            egresses NOTHING; the chokepoint proves that separately).
          * "off" -- a row with enabled==0: a DELIBERATE admin DISABLE (kill-switch),
            overriding an env flag that is on.
          * None  -- no row: the env flag alone decides => byte-identical to today.

        The override is the single admin choice persisted on the box (survives restart);
        writing it is gated at the route (require_local + admin scope on the loopback)."""
        row = self.get(id)
        if row is None:
            return None
        return "on" if row["enabled"] == 1 else "off"

    def egress_allowed(self) -> bool:
        """system-toggles master gate: True unless the operator has explicitly
        flipped the reserved `sys:egress` row OFF from the System tab. Absent row
        (the default -- nobody has ever touched the toggle) => True => every
        existing egress chokepoint keeps its own pre-existing gate as the sole
        decider, byte-identical to before this feature shipped. This is a GATE
        ONLY -- an ALLOW here never grants egress a feature wasn't already
        configured to attempt; it can only ADD a block on top."""
        row = self.get("sys:egress")
        return row is None or row["enabled"] == 1

    def sensing_allowed(self) -> bool:
        """system-toggles master gate for the active/passive network-sensing
        collectors (port scan, mDNS/SSDP/NetBIOS/SNMP/DHCP-fp, latency probe).
        Same default-ALLOW/absent-row contract as `egress_allowed` -- see there.
        The base zero-egress ARP inventory scan is NOT gated by this (it is core
        LAN-read presence, not an optional collector)."""
        row = self.get("sys:sensing")
        return row is None or row["enabled"] == 1

    def effective_active(self, id: str, env_active: bool) -> bool:
        """Effective GATE for a built-in feature: the persisted admin override WINS when
        present (a deliberate, loopback-admin action), else the env flag decides. An
        empty registry => `env_active` unchanged => byte-identical to today. This is the
        gate ONLY -- whether the feature can actually run (provider key, HA creds, an
        HTTP mount) is a separate readiness check the caller ANDs in, so this never
        conjures egress from an unconfigured connector."""
        ov = self.override(id)
        if ov is None:
            return bool(env_active)
        return ov == "on"

    def close(self) -> None:
        self._conn.close()
