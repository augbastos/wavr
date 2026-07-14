"""Per-device token store for multi-device client auth (ADR-0006, Phase 1).

Persists DEVICE DEFINITIONS only — an id, a human name, a role, the token *hash*,
and coarse timestamps. Consistent with `storage.py` / `camera_store.py`: this holds
configuration/metadata, never RoomState, never x/y targets, never vitals.

Tokens are random 256-bit secrets returned exactly once at pairing and stored
**hashed** (sha256) — the plaintext token never touches disk, so a leaked db file
cannot be replayed against the API. Off by default: nothing here runs unless
`WAVR_MULTIDEVICE` is enabled and a peer pairs.

Writes/reads are guarded by a lock so the connection can be driven from a thread
pool (`asyncio.to_thread`) without contention, same pattern as Storage.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

# The three grantable device roles (the loopback root central needs no token/row).
# 'agent' (Phase 2A / B4) is the bounded MCP-client principal: it gets NOTHING from
# can_view/can_change_state (both fail closed -- 'agent' is absent from both role
# tuples in auth.py by construction, unchanged by adding it here) and reaches the
# API surface ONLY via /mcp, further bounded there by its per-tool allow-list (see
# `tool_scopes` below + auth.effective_tool_scopes) -- "a bounded capability set,
# not the whole API" by design, not by convention.
VALID_ROLES = frozenset({"central", "user", "agent"})

# Device-scope participation tri-color (mobile companion consent, 2026-07-11
# reconciliation): the SAME axis the shim's POST /api/consent has always
# targeted (wavr-mobile-shim.js's CONSENT map) -- this column is what finally
# makes that endpoint real. green=full (named presence), yellow=presence only
# (no name label), red=off (contributes nothing, enforced server-side at
# register_companion, not just client-side). NULL (every pre-existing row, and
# every add() call that doesn't pass consent=) resolves to "green" -- the same
# NULL-derives-a-default idiom `scopes`/`tool_scopes` already use, so this is
# additive-only for every device paired before this feature existed.
VALID_CONSENT = frozenset({"green", "yellow", "red"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    name         TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    created_ts   TEXT    NOT NULL,
    last_seen_ts TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Device:
    """A paired device, minus its (hashed, never-returned) token."""

    device_id: str
    name: str
    role: str
    created_ts: str
    last_seen_ts: str | None
    revoked: bool
    # Wavr Pass (Phase 1): the device's OWN explicit scope grant, or None when it
    # has never been granted one -- the backward-compat lever. `None` means
    # "derive from role" (auth.effective_scopes); every row that existed before
    # this column was added, and every row `add()` creates without an explicit
    # `scopes=`, is None here. An explicit (even empty) frozenset means a P2
    # consent flow has actually narrowed/widened this device's grant.
    scopes: frozenset[str] | None = None
    # Wavr Pass (Phase 2A / B4): the device's OWN explicit MCP TOOL-NAME allow-list
    # -- a SEPARATE axis from `scopes` above (route scopes vs. individual tool
    # names). `None` means "derive from role" (auth.effective_tool_scopes); only
    # meaningful for role == "agent" -- every other role resolves this axis to
    # None ("not restricted by it at all"), unchanged pre-existing behaviour.
    tool_scopes: frozenset[str] | None = None
    # Device-scope participation tri-color (see VALID_CONSENT above). `None` means
    # "no explicit grant yet" -- every pre-existing row and every add() call
    # without an explicit consent= -- and resolves to "green" everywhere this is
    # READ (to_dict() below, and app.py's enforcement); it is stored as-is (raw
    # None, never silently rewritten to "green") so a fresh pairing's very first
    # /api/consent GET can still tell "never set" apart from "explicitly green".
    consent: str | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "role": self.role,
            "created_ts": self.created_ts,
            "last_seen_ts": self.last_seen_ts,
            "revoked": self.revoked,
            # Resolved (never raw NULL) so a device-list caller always sees an
            # honest tri-color, matching the "NULL -> green" default everywhere
            # else consent is consumed.
            "consent": self.consent or "green",
        }


def _hash_token(token: str) -> str:
    """sha256 hex of the token. Tokens are high-entropy (256-bit) random secrets,
    so a plain fast hash is appropriate here — there is nothing to brute-force."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_scopes(raw: str | None) -> frozenset[str] | None:
    """Column value -> `Device.scopes`. NULL (`raw is None`) => None ("derive
    from role" -- every pre-Wavr-Pass row and every default `add()` call reads
    back this way). A non-NULL value (even `""`) is an EXPLICIT grant -- a
    space-delimited scope list; `"".split()` correctly yields `[]` -> an
    explicit empty frozenset (deny-all), distinct from NULL."""
    if raw is None:
        return None
    return frozenset(raw.split())


def _serialize_scopes(scopes: frozenset[str] | None) -> str | None:
    """`Device.scopes` -> column value. None => NULL (unset). Sorted + space-
    joined for a stable, human-readable value (helps eyeballing the db file
    directly; verify()/list()/get() never rely on ordering)."""
    if scopes is None:
        return None
    return " ".join(sorted(scopes))


class DeviceStore:
    """SQLite-backed device/token store. Shares the db file with Storage but owns
    its own `devices` table. Tokens are stored hashed; `verify` is the only way a
    presented token is checked, and it never reveals the hash."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_scopes_column()
        self._migrate_tool_scopes_column()
        self._migrate_consent_column()

    def _migrate_scopes_column(self) -> None:
        """Wavr Pass (Phase 1), additive: add the nullable `scopes` column to an
        existing `devices` table that predates it. PRAGMA-guarded so this is a
        no-op (never a duplicate-column error) on every init after the first --
        safe to call once per __init__, on a brand-new db (freshly created by
        the CREATE TABLE above, column still absent) or a years-old one."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(devices)")}
        if "scopes" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN scopes TEXT")
            self._conn.commit()

    def _migrate_tool_scopes_column(self) -> None:
        """Wavr Pass (Phase 2A / B4), additive: add the nullable `tool_scopes`
        column -- the AGENT principal's MCP tool-name allow-list, a SEPARATE axis
        from `scopes` (route scopes). Same PRAGMA-guarded, idempotent, no-backfill
        pattern as `_migrate_scopes_column` (and run right after it), so a
        pre-existing db (with or without `scopes` already) gains this column
        exactly once, with every existing row reading back `tool_scopes=None`
        ("derive from role" -- meaningless for non-agent roles, the sane
        READ-ONLY default for 'agent', see auth.effective_tool_scopes)."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(devices)")}
        if "tool_scopes" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN tool_scopes TEXT")
            self._conn.commit()

    def _migrate_consent_column(self) -> None:
        """Mobile companion consent tri-color, additive: same idempotent
        PRAGMA-guarded pattern as `_migrate_scopes_column`/`_migrate_tool_scopes_
        column` -- a pre-existing db gains this column exactly once, every
        existing row reading back `consent=None` ("derive green", the sane
        full-participation default so a device paired before this feature
        existed keeps contributing exactly as it always has)."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(devices)")}
        if "consent" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN consent TEXT")
            self._conn.commit()

    def add(self, name: str, role: str, scopes: frozenset[str] | None = None,
            tool_scopes: frozenset[str] | None = None,
            consent: str | None = None) -> tuple[str, str]:
        """Create a device and return (device_id, token). The token is generated
        here, stored hashed, and returned exactly once — the caller must hand it to
        the device now; it can never be recovered later.

        `scopes` defaults to None (NULL column -- "derive from role", auth.
        effective_scopes) so every EXISTING caller of `add(name, role)` keeps its
        current behaviour byte-for-byte; pass an explicit frozenset only for a
        future consent-granted device (P2). `tool_scopes` (Phase 2A / B4) is the
        SAME NULL-derives-from-role idiom for the MCP tool-name axis -- meaningful
        only for role="agent" (auth.effective_tool_scopes); every other caller
        passing neither kwarg is unaffected. `consent` is the SAME NULL-derives-
        default idiom for the mobile companion tri-color -- None (every existing
        caller) resolves to "green" (full participation); pass an explicit
        green/yellow/red only for a future consent-aware pairing flow."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (expected one of {sorted(VALID_ROLES)})")
        if consent is not None and consent not in VALID_CONSENT:
            raise ValueError(f"invalid consent: {consent!r} (expected one of {sorted(VALID_CONSENT)})")
        device_id = secrets.token_hex(16)          # 128-bit opaque id
        token = secrets.token_urlsafe(32)          # 256-bit secret, URL-safe
        token_hash = _hash_token(token)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (device_id, name, role, token_hash, created_ts,"
                " last_seen_ts, revoked, scopes, tool_scopes, consent)"
                " VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)",
                (device_id, name, role, token_hash, ts, _serialize_scopes(scopes),
                 _serialize_scopes(tool_scopes), consent),
            )
            self._conn.commit()
        return device_id, token

    def verify(self, token: str) -> Device | None:
        """Return the Device for a valid, non-revoked token (updating last_seen), or
        None if the token is unknown or the device is revoked. Constant work either
        way from the caller's view — the lookup is by token_hash."""
        if not token:
            return None
        token_hash = _hash_token(token)
        ts = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT device_id, name, role, created_ts, revoked, scopes, tool_scopes, consent"
                " FROM devices WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked"]:
                return None
            self._conn.execute(
                "UPDATE devices SET last_seen_ts = ? WHERE device_id = ?",
                (ts, row["device_id"]),
            )
            self._conn.commit()
        return Device(
            device_id=row["device_id"], name=row["name"], role=row["role"],
            created_ts=row["created_ts"], last_seen_ts=ts, revoked=False,
            scopes=_parse_scopes(row["scopes"]),
            tool_scopes=_parse_scopes(row["tool_scopes"]),
            consent=row["consent"],
        )

    def list(self) -> list[Device]:
        """All devices (including revoked ones) for the revocation UI. Never
        includes token material."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, name, role, created_ts, last_seen_ts, revoked, scopes, tool_scopes, consent"
                " FROM devices ORDER BY created_ts, device_id"
            ).fetchall()
        return [self._to_device(r) for r in rows]

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT device_id, name, role, created_ts, last_seen_ts, revoked, scopes, tool_scopes, consent"
                " FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return self._to_device(row) if row else None

    def revoke(self, device_id: str) -> bool:
        """Mark a device revoked. Returns True if the device exists (idempotent — a
        second revoke of the same id still returns True). A revoked token fails on
        its very next `verify`."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET revoked = 1 WHERE device_id = ?", (device_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_role(self, device_id: str, role: str) -> bool:
        """Change a paired device's role (promote/demote between VALID_ROLES).
        Returns True if the device exists (row updated), False for an unknown id.
        Touches ONLY the role column — never the token hash or the revoked flag, so
        a role change can never grant or void credentials. Raises ValueError for a
        role outside VALID_ROLES (validated before touching the db)."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (expected one of {sorted(VALID_ROLES)})")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET role = ? WHERE device_id = ?", (role, device_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_consent(self, device_id: str, level: str) -> bool:
        """Self-service device-scope participation change (POST /api/consent):
        the ONLY writer of the `consent` column. Returns True if the device
        exists (row updated), False for an unknown id. Raises ValueError for a
        level outside VALID_CONSENT (validated before touching the db) -- the
        route turns that into a clean 422, same convention as `set_role`."""
        if level not in VALID_CONSENT:
            raise ValueError(f"invalid consent: {level!r} (expected one of {sorted(VALID_CONSENT)})")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET consent = ? WHERE device_id = ?", (level, device_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _to_device(r: sqlite3.Row) -> Device:
        return Device(
            device_id=r["device_id"], name=r["name"], role=r["role"],
            created_ts=r["created_ts"], last_seen_ts=r["last_seen_ts"],
            revoked=bool(r["revoked"]), scopes=_parse_scopes(r["scopes"]),
            tool_scopes=_parse_scopes(r["tool_scopes"]),
            consent=r["consent"],
        )

    def close(self) -> None:
        self._conn.close()
