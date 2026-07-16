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

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from wavr.device_meta import normalize_mac, sanitize_name

# A registered device feeds exactly one presence modality.
VALID_SOURCES = frozenset({"ble", "network"})
# How the admin's consent was expressed (see module docstring).
VALID_ORIGINS = frozenset({"bonded", "manual", "companion"})

# The ANONYMOUS person marker (tri-color "yellow", 2026-07-16). A row whose
# `person` is empty is a device that consented to CORROBORATE presence but NOT to
# be named -- "counted as home, without a name". It is a real row (so the live
# provider counts its MAC toward presence) that carries no PII at rest, which is
# what yellow means; every read path that hands out a NAME skips it. Empty-string
# rather than a nullable column or a parallel flag: one field, one meaning, no
# two-fields-out-of-sync failure mode. `add()` can never produce one (sanitize_name
# rejects empty) -- `add_anonymous()` is the only writer.
ANONYMOUS = ""

# The operator's own loopback box, when IT self-registers via register-companion.
# Root holds no Device row (its participation lever is /api/system/toggle, which is
# why /api/consent 409s for it), so its row would otherwise be indistinguishable
# from a pre-upgrade row with a NULL device_id -- which fails CLOSED. Server-
# assigned only: device_id is never client-supplied, it comes from the verified
# bearer token (app._self_device). Cannot collide with a real device_id (those are
# 32-char hex).
ROOT_DEVICE_ID = "root"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_devices (
    address    TEXT PRIMARY KEY,
    person     TEXT NOT NULL,
    source     TEXT NOT NULL,
    origin     TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    details    INTEGER NOT NULL DEFAULT 0,
    device_id  TEXT
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(r: sqlite3.Row) -> dict:
    """sqlite stores `details` as an INTEGER (0/1); every dict this store hands
    back to a caller/route surfaces it as a real bool, never a raw 0/1 int.

    `anonymous` is derived, never stored: it says the row deliberately carries no
    person label ("yellow"). Every consumer of a row predates ANONYMOUS existing
    and assumes `person` is a non-empty string (add() guarantees that via
    sanitize_name), so handing one an empty string and letting it work out what
    that means is how a blank name ends up rendered as a real one. This is the
    explicit flag to branch on."""
    d = dict(r)
    d["details"] = bool(d["details"])
    d["anonymous"] = d.get("person") == ANONYMOUS
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
        # Live consent lookup (device_id -> "green"|"yellow"|"red"), injected by
        # create_app via set_consent_lookup. None (the default, and every test that
        # doesn't wire it) -> every row reads "green", i.e. byte-identical to the
        # behaviour before the tri-color gate existed.
        self._consent_of = None
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # Migration-safe additive columns (mirrors wavr.device_meta._migrate): a DB
        # created before the `details` opt-in existed lacks the column -- CREATE
        # TABLE IF NOT EXISTS won't add it to an already-existing table.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(identity_devices)")}
        if "details" not in cols:
            self._conn.execute(
                "ALTER TABLE identity_devices ADD COLUMN details INTEGER NOT NULL DEFAULT 0")
        # `device_id` links a companion self-registration back to the Device row
        # that owns the consent tri-color, so a LATER consent change (POST
        # /api/consent) applies to an ALREADY-written row. Nullable on purpose:
        # admin-added rows (origin bonded/manual) have no consent axis of their own
        # -- the admin's act IS the consent -- and read as "green".
        if "device_id" not in cols:
            self._conn.execute("ALTER TABLE identity_devices ADD COLUMN device_id TEXT")

    def set_consent_lookup(self, consent_of) -> None:
        """Wire the LIVE device-consent lookup (create_app owns the DeviceStore, so
        it is injected after construction rather than taken as a ctor arg).

        This is what makes consent READ-time rather than write-time. Enforcing it
        only where a row is WRITTEN (register-companion) was the original bug: the
        level is changed LATER by POST /api/consent, so a device registered green
        and then withdrawn to red kept feeding named presence forever. Every read
        path funnels through _rows(), so a new consumer cannot forget the gate."""
        self._consent_of = consent_of

    def _level(self, device_id: str | None, origin: str) -> str:
        """This row's LIVE consent level.

        Only origin='companion' rows have a consent axis of their own: a device
        granted its OWN participation and can withdraw it. An admin-added row
        (bonded/manual) has none -- the admin's affirmative act IS the consent, and
        there is no Device row to ask -- so it reads "green".

        Everything else about a companion row fails CLOSED. A row we cannot tie to
        a live, readable grant does not get one:
          * device_id NULL -- a row written before this column existed (an UPGRADE:
            the ALTER TABLE cannot backfill a link that was never recorded). Such a
            row would otherwise be pinned green forever, un-withdrawable, because
            POST /api/consent could never reach it. It reads "red" until its owner
            re-registers (the shim does that on every boot/attach/resume, which
            re-links it via _write's COALESCE), so a participating device self-heals
            within one app launch and a non-participating one correctly stays dark.
          * no lookup wired -- a store nobody gave a consent source to cannot
            confirm a grant either.
          * the lookup RAISED (e.g. sqlite busy on the Core's SD card). Logged
            loudly -- a consent gate that degrades must never do it silently -- and
            never re-raised, because this runs inside the network source's per-cycle
            read where an exception would kill the scan loop.

        ROOT_DEVICE_ID is the one companion row with no Device row that is still
        legitimate: the operator's own loopback box, whose lever is
        /api/system/toggle rather than /api/consent. It is server-assigned and can
        never be client-supplied (see app.register_companion)."""
        if origin != "companion":
            return "green"
        if not device_id or self._consent_of is None:
            return "red"
        try:
            return self._consent_of(device_id) or "green"
        except Exception:
            logging.warning(
                "identity consent lookup failed; failing CLOSED (device treated as "
                "withdrawn until the lookup recovers)", exc_info=True)
            return "red"

    def add_anonymous(self, address: str, source: str = "network",
                      origin: str = "companion", device_id: str | None = None) -> dict:
        """Register a device as ANONYMOUS presence (tri-color "yellow"): it counts
        toward presence, it is never named. The ONLY writer of an `ANONYMOUS` row
        -- `add()` cannot produce one because sanitize_name rejects an empty label.

        Yellow used to write NOTHING at all, on the reasoning that skipping the
        write is what withholds the name. It withheld the presence too: the live
        provider is built from these rows, so an unwritten device never entered the
        `known` set and `known & seen` (sources/network.py) could not match it --
        yellow silently delivered exactly what red does. This row is the presence
        half, without the PII half."""
        return self._write(address, ANONYMOUS, source, origin, None, device_id)

    def add(self, address: str, person: str, source: str = "ble",
            origin: str = "manual", details: bool | None = None,
            device_id: str | None = None) -> dict:
        """Register (or re-register) a consented device. Validates before writing;
        raises ValueError on a malformed address, an empty/oversized person label,
        or a source/origin outside its fixed set. Re-registering the same address
        updates person/source/origin but preserves the original created_ts (the
        first act of consent), so a re-confirm never rewrites history.

        `device_id` links the row to the Device row that owns the consent tri-color
        (companion self-registration); None for admin-added rows, which have no
        consent axis of their own. See set_consent_lookup.

        `details` is consent #2 (see module docstring), OPTIONAL and separate from
        the act of registering: None (the default -- e.g. a plain re-register that
        only updates the person label) leaves any existing `details` value alone
        rather than silently reverting an earlier opt-in; a new row with no
        `details` given starts at False (opt-in is never implied). Pass an
        explicit True/False to set it as part of the same write (used by the
        register route when the caller does supply it) -- `set_details` remains
        the standalone toggle for the common case of flipping it on its own."""
        return self._write(address, sanitize_name(person), source, origin, details,
                           device_id)

    def _write(self, address: str, who: str, source: str, origin: str,
               details: bool | None, device_id: str | None) -> dict:
        """The single INSERT both writers share. `who` is already sanitized by the
        caller (or is ANONYMOUS, which sanitize_name would reject) -- everything
        else is validated here so neither entry point can skip it."""
        addr = normalize_mac(address)          # raises ValueError on junk MAC
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source: {source!r} (expected one of {sorted(VALID_SOURCES)})")
        if origin not in VALID_ORIGINS:
            raise ValueError(f"invalid origin: {origin!r} (expected one of {sorted(VALID_ORIGINS)})")
        ts = self._now()
        details_val = 0 if details is None else int(bool(details))
        with self._lock:
            self._conn.execute(
                "INSERT INTO identity_devices"
                " (address, person, source, origin, created_ts, details, device_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(address) DO UPDATE SET"
                "   person = excluded.person,"
                "   source = excluded.source,"
                "   origin = excluded.origin,"
                "   details = CASE WHEN ? IS NULL THEN identity_devices.details ELSE ? END,"
                # A re-register from the same companion refreshes the link; an admin
                # re-add (device_id NULL) must not ORPHAN a row from the device whose
                # consent governs it, so NULL leaves the existing link intact.
                "   device_id = COALESCE(excluded.device_id, identity_devices.device_id)",
                (addr, who, source, origin, ts, details_val, device_id, details, details_val),
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
        corroboration itself never consults this (row existence alone drives it).

        Consent #2 rides ON TOP of the tri-color, it does not outrank it: only a
        "green" row can surface details. A device that opted into details while
        green and then stepped down to "yellow" is asking to be counted without a
        name -- surfacing its first/last-seen and device type would be the opposite
        of the "minimal data" that step means. The narrower grant does not survive
        the wider one being withdrawn."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, device_id, origin FROM identity_devices"
                " WHERE source = 'network' AND details = 1"
            ).fetchall()
        return {r["address"] for r in rows
                if self._level(r["device_id"], r["origin"]) == "green"}

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
        """Live {mac: person} for source='network': the NAMED map -- every read
        path that discloses a person label (NetworkSource's identities,
        known_presence's corroborators) composes over this.

        Consent-gated: ONLY "green" rows appear. A withdrawn ("red") or anonymous
        ("yellow") device is absent, so no consumer can name it -- including one
        written after this gate, which is why the filter lives here and not in each
        caller."""
        return self._as_map("network")

    def as_net_known(self) -> dict[str, str | None]:
        """Live PRESENCE map for source='network' -- what NetworkSource's `known`
        set is built from. Wider than as_net_map by exactly one level:

            green  -> {mac: person}   counted, named
            yellow -> {mac: None}     counted, ANONYMOUS ("without a name")
            red    -> absent          not counted at all

        A None value is the whole point: it says "this MAC corroborates presence
        and has no label", which a {mac: person} map had no way to express -- so
        yellow had to either lie (a name) or vanish (it vanished)."""
        out: dict[str, str | None] = {}
        for addr, person, level in self._rows("network"):
            if level == "red":
                continue
            out[addr] = person if (level == "green" and person) else None
        return out

    def _as_map(self, source: str) -> dict[str, str]:
        return {addr: person for addr, person, level in self._rows(source)
                if level == "green" and person}

    def _rows(self, source: str) -> list[tuple[str, str, str]]:
        """(address, person, live consent level) for one modality. Every PRESENCE /
        NAME read path funnels through here, so the consent gate cannot be bypassed
        by forgetting it in a new consumer. (list()/get() deliberately do not: they
        are the admin's raw registry view, gated to central/root at the route.)

        _level is called AFTER the lock is released -- it calls out to the device
        store, and holding our lock across another store's lock would invite a
        deadlock."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, person, device_id, origin FROM identity_devices"
                " WHERE source = ?",
                (source,),
            ).fetchall()
        return [(r["address"], r["person"], self._level(r["device_id"], r["origin"]))
                for r in rows]

    def close(self) -> None:
        self._conn.close()
