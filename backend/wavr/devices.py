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

# The grantable device roles (the loopback root central needs no token/row).
#   central -> full admin over the LAN transport   user -> read + own-device telemetry
#   sensor  -> WRITE-ONLY phone telemetry; confined by app.py middleware to
#              POST /api/telemetry (403 on every read route + /ws/live). Smallest
#              blast radius for a stolen token (mobile unification, blueprint step 1).
# Both add() and set_role() validate against this set, so a "sensor" row can only ever
# exist once it is listed here. Free-text TEXT column -> no DB migration needed.
VALID_ROLES = frozenset({"central", "user", "sensor"})

# Per-device CONSENT TIER (privacy centerpiece). Monotone green ⊃ yellow ⊃ red; consent
# is purely SUBTRACTIVE -- it can only REDUCE what a device contributes, never raise trust.
#   green  -> full telemetry ingested; device NAMED in who's-home; coarse presence vote.
#   yellow -> telemetry ingested but REDUCED server-side (rssi/ssid/bssid=None, sensors={});
#             device votes present but ANONYMOUS (never named in who's-home).
#   red    -> telemetry DROPPED server-side (never reaches the hub); not present, not named.
# Unlike `role` (which existed from schema v1), `consent` is a NEW column, so an existing
# wavr.db needs the additive ALTER migration in DeviceStore.__init__ below -- CREATE TABLE
# IF NOT EXISTS is a no-op on a table that already exists and would silently skip it.
#
# DEFENSE-IN-DEPTH: the fresh-db _SCHEMA below adds a CHECK (consent IN green/yellow/red) so
# a direct-SQL garbage write is rejected at the DB layer too. This is a SECOND line only:
# the ALTER path (existing dbs) cannot add a CHECK without a full table rebuild, which we
# deliberately do NOT do -- the app-layer FAIL-CLOSED WHITELIST gate (app.py telemetry +
# phone.py consume-side) is the load-bearing protection for BOTH fresh and migrated dbs.
VALID_CONSENT = frozenset({"green", "yellow", "red"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    name         TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    created_ts   TEXT    NOT NULL,
    last_seen_ts TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0,
    consent      TEXT    NOT NULL DEFAULT 'green'
                 CHECK (consent IN ('green', 'yellow', 'red'))
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
    consent: str = "green"

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "role": self.role,
            "created_ts": self.created_ts,
            "last_seen_ts": self.last_seen_ts,
            "revoked": self.revoked,
            "consent": self.consent,
        }


def _hash_token(token: str) -> str:
    """sha256 hex of the token. Tokens are high-entropy (256-bit) random secrets,
    so a plain fast hash is appropriate here — there is nothing to brute-force."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._migrate_consent()

    def _migrate_consent(self) -> None:
        """Idempotent additive migration for the `consent` column (privacy tiers).
        A fresh db already has it from _SCHEMA; an EXISTING v1 db does NOT -- CREATE
        TABLE IF NOT EXISTS never touches an existing table. Detect via PRAGMA and
        ALTER it on only when absent, so opening an old wavr.db backfills the column
        (defaulting every prior device to 'green' -- no behavioural change on upgrade)
        while a fresh db is a no-op. Runs once at construction (single-threaded)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(devices)").fetchall()}
        if "consent" not in cols:
            self._conn.execute(
                "ALTER TABLE devices ADD COLUMN consent TEXT NOT NULL DEFAULT 'green'"
            )
            self._conn.commit()

    def add(self, name: str, role: str) -> tuple[str, str]:
        """Create a device and return (device_id, token). The token is generated
        here, stored hashed, and returned exactly once — the caller must hand it to
        the device now; it can never be recovered later."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (expected one of {sorted(VALID_ROLES)})")
        device_id = secrets.token_hex(16)          # 128-bit opaque id
        token = secrets.token_urlsafe(32)          # 256-bit secret, URL-safe
        token_hash = _hash_token(token)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (device_id, name, role, token_hash, created_ts,"
                " last_seen_ts, revoked) VALUES (?, ?, ?, ?, ?, NULL, 0)",
                (device_id, name, role, token_hash, ts),
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
                "SELECT device_id, name, role, created_ts, last_seen_ts, revoked, consent"
                " FROM devices WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked"]:
                return None
            # GDPR-red completeness (appsec): a withdrawn (red) device that keeps POSTing must
            # NOT advance its "last contacted" timestamp -- otherwise the central GET
            # /api/devices exposes a live presence oracle for a device that opted OUT of being
            # tracked. Skip the last_seen UPDATE for red (freeze it at the last non-red
            # contact); green/yellow update as before. The consent-gate in app.py already
            # drops/reduces the reading itself -- this closes the residual metadata channel.
            if row["consent"] == "red":
                last_seen = row["last_seen_ts"]
            else:
                self._conn.execute(
                    "UPDATE devices SET last_seen_ts = ? WHERE device_id = ?",
                    (ts, row["device_id"]),
                )
                self._conn.commit()
                last_seen = ts
        # consent is read off this SAME already-fetched row -> the telemetry hot path
        # (verify -> consent gate) costs no extra query.
        return Device(
            device_id=row["device_id"], name=row["name"], role=row["role"],
            created_ts=row["created_ts"], last_seen_ts=last_seen, revoked=False,
            consent=row["consent"],
        )

    def list(self) -> list[Device]:
        """All devices (including revoked ones) for the revocation UI. Never
        includes token material."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, name, role, created_ts, last_seen_ts, revoked, consent"
                " FROM devices ORDER BY created_ts, device_id"
            ).fetchall()
        return [self._to_device(r) for r in rows]

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT device_id, name, role, created_ts, last_seen_ts, revoked, consent"
                " FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return self._to_device(row) if row else None

    def get_label(self, device_id: str) -> str | None:
        """Human name for a device_id, or None if unknown. Thin read-only accessor for
        PhoneSensorSource's who's-home view — resolves the operator label by device_id
        WITHOUT ever returning token material. The label is derived here, on demand, and
        never placed on a SensingEvent (privacy boundary)."""
        d = self.get(device_id)
        return d.name if d else None

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
        """Set a paired device's CONSENT TIER (green/yellow/red). Returns True if the
        device exists (row updated), False for an unknown id. Touches ONLY the consent
        column -- never the token hash, revoked flag, or role -- so a consent change can
        neither grant/void credentials nor alter authorization; it is purely subtractive
        at the ingest/who's-home layer. Raises ValueError for a level outside
        VALID_CONSENT (validated before touching the db), mirroring set_role's discipline."""
        if level not in VALID_CONSENT:
            raise ValueError(
                f"invalid consent: {level!r} (expected one of {sorted(VALID_CONSENT)})")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET consent = ? WHERE device_id = ?", (level, device_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_consent(self, device_id: str) -> str | None:
        """Current consent tier for a device_id, or None if unknown. Thin read-only
        accessor for PhoneSensorSource's who's-home / queue-residue re-check -- resolves
        the tier by device_id without returning token material. A None result (unknown/
        deleted device) is treated fail-closed by callers (not named, not recorded)."""
        d = self.get(device_id)
        return d.consent if d else None

    @staticmethod
    def _to_device(r: sqlite3.Row) -> Device:
        return Device(
            device_id=r["device_id"], name=r["name"], role=r["role"],
            created_ts=r["created_ts"], last_seen_ts=r["last_seen_ts"],
            revoked=bool(r["revoked"]), consent=r["consent"],
        )

    def close(self) -> None:
        self._conn.close()
