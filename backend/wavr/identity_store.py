"""Consent-first identity/device registry (2026-07-06 ethics decision).

Persists ONLY self-attested / admin-confirmed devices: a device becomes a tracked
presence signal *solely* by an affirmative act of its owner. Two paths reach this
store, both consented:

  * bonded-confirm -- a Bluetooth device BONDED to this PC is a deliberate pairing
    act (distinct from an involuntary BLE broadcast, which is NOT consent). The
    admin still explicitly confirms "these are mine" before a row is written; a
    bonded device is a SUGGESTION, never a blind auto-register (a housemate may
    have paired their phone to the shared PC once -> the admin must be able to
    uncheck it). origin='bonded'.
  * manual add -- address + label typed for anything not bonded. origin='manual'.
  * companion self-register -- a PAIRED LAN companion (or the loopback operator)
    registers ITS OWN device via POST /api/presence/register-companion; the MAC
    is derived server-side from the request's own source IP (never client-
    supplied), so this is a THIRD, self-attested consent path distinct from an
    admin confirming someone else's device. origin='companion'.

Un-registering a row IS the participation opt-out: it immediately stops the device
being a presence signal (the live known-provider stops returning it on the next
scan cycle) and removes its person label. Wavr NEVER fingerprints-and-follows an
unknown / non-consenting device -- only rows in this table carry a person label.

TWO-LEVEL consent (known-presence, 2026-07-11): a row existing IS consent #1 --
"this device may corroborate house-level presence" -- and is the ONLY thing
presence corroboration (wavr.known_presence) reads. `details` is a SEPARATE,
narrower consent #2 -- "also surface this device's already-collected metadata
(first/last seen, device type) and let its per-device network label emit even
with the global identity flag off" -- toggled independently via `set_details`,
never implied by registration and never required for presence to count.

Mirrors camera_store.py: a small sqlite store sharing wavr.db (git-ignored) but
owning its own table; ":memory:" for tests; lock-guarded so it can be driven from
a thread pool. Purely local -- there is no feedback-to-anywhere loop, and `person`
is PII that is never logged.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from wavr.device_meta import normalize_mac, sanitize_name

# A registered device feeds exactly one presence modality.
VALID_SOURCES = frozenset({"ble", "network"})
# How the admin's consent was expressed (see module docstring).
VALID_ORIGINS = frozenset({"bonded", "manual", "companion"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_devices (
    address    TEXT PRIMARY KEY,
    person     TEXT NOT NULL,
    source     TEXT NOT NULL,
    origin     TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    details    INTEGER NOT NULL DEFAULT 0
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(r: sqlite3.Row) -> dict:
    """sqlite stores `details` as an INTEGER (0/1); every dict this store hands
    back to a caller/route surfaces it as a real bool, never a raw 0/1 int."""
    d = dict(r)
    d["details"] = bool(d["details"])
    return d


class IdentityStore:
    """SQLite-backed consent registry: {address -> (person, source, origin)}.

    `add` is the ONLY write and validates everything before it touches the db
    (normalized MAC, non-empty <=64-char person, source/origin in their fixed
    sets) -- a junk/injection address raises ValueError, which the API route turns
    into a 400 rather than letting garbage reach SQL or be reflected via a later
    GET. `as_ble_map`/`as_net_map` are the LIVE providers the sources re-read each
    scan cycle, so a registration/opt-out takes effect on the next cycle with no
    server restart."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # Migration-safe additive column (mirrors wavr.device_meta._migrate): a DB
        # created before the `details` opt-in existed lacks the column -- CREATE
        # TABLE IF NOT EXISTS won't add it to an already-existing table.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(identity_devices)")}
        if "details" not in cols:
            self._conn.execute(
                "ALTER TABLE identity_devices ADD COLUMN details INTEGER NOT NULL DEFAULT 0")

    def add(self, address: str, person: str, source: str = "ble",
            origin: str = "manual", details: bool | None = None) -> dict:
        """Register (or re-register) a consented device. Validates before writing;
        raises ValueError on a malformed address, an empty/oversized person label,
        or a source/origin outside its fixed set. Re-registering the same address
        updates person/source/origin but preserves the original created_ts (the
        first act of consent), so a re-confirm never rewrites history.

        `details` is consent #2 (see module docstring), OPTIONAL and separate from
        the act of registering: None (the default -- e.g. a plain re-register that
        only updates the person label) leaves any existing `details` value alone
        rather than silently reverting an earlier opt-in; a new row with no
        `details` given starts at False (opt-in is never implied). Pass an
        explicit True/False to set it as part of the same write (used by the
        register route when the caller does supply it) -- `set_details` remains
        the standalone toggle for the common case of flipping it on its own."""
        addr = normalize_mac(address)          # raises ValueError on junk MAC
        who = sanitize_name(person)            # raises ValueError on empty/oversized
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source: {source!r} (expected one of {sorted(VALID_SOURCES)})")
        if origin not in VALID_ORIGINS:
            raise ValueError(f"invalid origin: {origin!r} (expected one of {sorted(VALID_ORIGINS)})")
        ts = self._now()
        details_val = 0 if details is None else int(bool(details))
        with self._lock:
            self._conn.execute(
                "INSERT INTO identity_devices (address, person, source, origin, created_ts, details)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(address) DO UPDATE SET"
                "   person = excluded.person,"
                "   source = excluded.source,"
                "   origin = excluded.origin,"
                "   details = CASE WHEN ? IS NULL THEN identity_devices.details ELSE ? END",
                (addr, who, source, origin, ts, details_val, details, details_val),
            )
            self._conn.commit()
        return self.get(addr)

    def get(self, address: str) -> dict | None:
        addr = normalize_mac(address)
        with self._lock:
            r = self._conn.execute(
                "SELECT address, person, source, origin, created_ts, details"
                " FROM identity_devices WHERE address = ?",
                (addr,),
            ).fetchone()
        return _row_to_dict(r) if r else None

    def list(self) -> list[dict]:
        """All registered devices, oldest first. Includes the person label (PII) --
        the route that returns this is gated (central/root only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, person, source, origin, created_ts, details"
                " FROM identity_devices ORDER BY created_ts, address"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def set_details(self, address: str, on: bool) -> bool:
        """Consent #2 toggle (see module docstring): flips the `details` opt-in for
        an already-registered device WITHOUT touching person/source/origin/
        created_ts. Returns False (no-op, no row created) if `address` isn't
        registered -- this can only narrow/widen disclosure for an existing,
        already-consented row, never itself register a device."""
        addr = normalize_mac(address)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE identity_devices SET details = ? WHERE address = ?",
                (1 if on else 0, addr),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def detailed_net_addresses(self) -> set[str]:
        """Live allowlist of source='network' MACs opted into consent #2 -- the
        ONLY gate wavr.known_presence and NetworkSource's per-device label emission
        read for "may this device's richer metadata/label surface". Presence
        corroboration itself never consults this (row existence alone drives it)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT address FROM identity_devices WHERE source = 'network' AND details = 1"
            ).fetchall()
        return {r["address"] for r in rows}

    def delete(self, address: str) -> bool:
        """Opt-out: remove a device from the registry. Returns True if a row was
        removed. After this the live provider stops returning the address, so it
        stops being a presence signal on the next scan cycle."""
        addr = normalize_mac(address)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM identity_devices WHERE address = ?", (addr,))
            self._conn.commit()
            return cur.rowcount > 0

    def as_ble_map(self) -> dict[str, str]:
        """Live {address: person} for source='ble' -- the map BLESource re-reads
        each cycle (merged with the env allowlist for back-compat)."""
        return self._as_map("ble")

    def as_net_map(self) -> dict[str, str]:
        """Live {mac: person} for source='network' -- the map NetworkSource re-reads
        each cycle. Its keys also count toward network presence."""
        return self._as_map("network")

    def _as_map(self, source: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, person FROM identity_devices WHERE source = ?",
                (source,),
            ).fetchall()
        return {r["address"]: r["person"] for r in rows}

    def close(self) -> None:
        self._conn.close()
