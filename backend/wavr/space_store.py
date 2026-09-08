"""Wavr Space — the first-class container this product was missing.

A **Space** is one physical environment Wavr understands: a home, a flat, an
office, a shop, a workshop. Everything else hangs off it — people, rooms,
devices, Cores, Nodes, policies, live context. "Home" stays the default UX
template, but it is no longer baked into the architecture.

## The separation this module exists to make

Before this, `Device.role == "central"` meant two unrelated things at once:
*"this credential may administer Wavr"* and *"this box is a hub"*. That
conflation makes three ordinary sentences unsayable:

  * "Augusto is the Owner, and these four devices are his."
  * "The tablet is a Client only; the laptop is Core **and** Node **and** Client."
  * "Make Ana an Admin" — without touching a single device.

So there are now three independent axes, and this module owns the first two:

  1. **Person role** (`people` table) — what a *human* may do. owner/admin/
     user/guest, capability-based so arbitrary org roles (facilities, IT,
     contractor…) can be added later without a schema change.
  2. **Device function** (`device_functions` table) — what *job* a box does for
     the Space: core / node / client, freely combinable.
  3. **Device auth role** (`devices.py`, UNTOUCHED) — what a *credential* may
     reach. Still central/user/agent/guest, still the thing `auth.py` enforces.

Axis 3 is deliberately left exactly as it is. It is the tested security surface,
and widening it would be the easiest way to turn an onboarding convenience into
a privilege bug. `device_role_for_person()` below expresses how axis 1 derives
axis 3 — an Owner's new phone pairing as `central`, a User's as `user`.

**That function is load-bearing, on every authenticated request.** `app.py`'s
`_person_role` reads the credential owner's CURRENT person role and passes it
through `device_role_for_person()` into `auth._apply_person_cap` as
`person_role_fn`, so axis 1 caps axis 3 live: demote somebody to `user` and
their already-issued `central` token narrows on the very next request, remove
them from the Space and it stops working entirely. `test_person_authorization.py`
pins that wiring.

The one thing still true of the original note is the direction. `pairing.py`
does NOT consult this — nothing here MINTS a credential. It only ever narrows
one that `devices.py` already issued (`narrower_role`, never wider), so this is
a ceiling over the existing gate, not a second door into it.

## Storage

Shares `wavr.db`, owns its own tables, same pattern as every other `*_store.py`
here. Holds configuration and identity only: no RoomState, no x/y targets, no
vitals, no frames. ADR-0002 is unaffected by anything in this file.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# -- Space kinds -------------------------------------------------------------
# Open-ish vocabulary: the UI offers these, an operator may store any short
# slug, and nothing in the engine branches on the value. It exists so the
# onboarding copy and the room templates can differ between a home and a shop
# without "home" being hard-coded into the data model.
SPACE_KINDS: tuple[str, ...] = (
    "home", "apartment", "office", "shop", "restaurant", "school",
    "warehouse", "workshop", "hotel", "laboratory", "other")
DEFAULT_SPACE_KIND = "home"

# -- Person roles ------------------------------------------------------------
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_GUEST = "guest"
PERSON_ROLES: frozenset[str] = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST})

# Person capabilities. A SEPARATE vocabulary from `auth.SCOPES` on purpose:
# those gate a *credential's* reach into the HTTP surface; these gate a
# *human's* authority over the Space. Keeping them apart is what lets an
# arbitrary future org role (facilities, contractor, executive) be expressed as
# a capability set without touching the tested auth gate.
CAP_SPACE_TRANSFER = "space:transfer"   # hand the Space to another person
CAP_SPACE_CONFIGURE = "space:configure"  # rename, change kind, change policy
CAP_PEOPLE_MANAGE = "people:manage"     # invite, change role, remove
CAP_DEVICE_MANAGE = "device:manage"     # pair, revoke, assign function/room
CAP_NODE_MANAGE = "node:manage"         # approve, disable, revoke nodes
CAP_CORE_MANAGE = "core:manage"         # promote/demote Cores
CAP_SENSOR_CONFIGURE = "sensor:configure"  # cameras, calibration, sources
CAP_DIAGNOSTICS_VIEW = "diagnostics:view"
CAP_CONTEXT_VIEW = "context:view"       # see presence/context they're allowed
CAP_CONTROL_USE = "control:use"         # use permitted controls

PERSON_CAPABILITIES: frozenset[str] = frozenset({
    CAP_SPACE_TRANSFER, CAP_SPACE_CONFIGURE, CAP_PEOPLE_MANAGE,
    CAP_DEVICE_MANAGE, CAP_NODE_MANAGE, CAP_CORE_MANAGE,
    CAP_SENSOR_CONFIGURE, CAP_DIAGNOSTICS_VIEW, CAP_CONTEXT_VIEW,
    CAP_CONTROL_USE,
})

_ADMIN_CAPS = frozenset({
    CAP_SPACE_CONFIGURE, CAP_PEOPLE_MANAGE, CAP_DEVICE_MANAGE,
    CAP_NODE_MANAGE, CAP_CORE_MANAGE, CAP_SENSOR_CONFIGURE,
    CAP_DIAGNOSTICS_VIEW, CAP_CONTEXT_VIEW, CAP_CONTROL_USE,
})

# Owner == Admin + the one thing an Admin must never do: give the Space away.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ROLE_OWNER: _ADMIN_CAPS | {CAP_SPACE_TRANSFER},
    ROLE_ADMIN: _ADMIN_CAPS,
    ROLE_USER: frozenset({CAP_CONTEXT_VIEW, CAP_CONTROL_USE}),
    # A guest is presence-only by default: it may *be counted* as present, and
    # it may see nothing. Least privilege, and it expires on its own.
    ROLE_GUEST: frozenset(),
}

# The credential role a person's next device SHOULD receive when it pairs.
#
# This TABLE is a preview, not a decision: the API renders it so an admin can
# see what granting someone "Admin" will actually mean before any device is
# paired. Do not read that as "the person axis does not affect pairing" — it
# does, from the other direction. `/api/pair-code` calls
# `narrower_role(requested, granted)` and refuses to mint a credential wider
# than the person holds, so an admin cannot pair a User's phone as `central`.
# The operator still CHOOSES the role; this table only says what would be
# reasonable.
#
# The mapping is deliberately narrow and one-directional: no person role maps to
# anything a human could not already have been granted by hand. That property is
# what would let pairing pre-select from it later without widening anything.
_PERSON_TO_DEVICE_ROLE: dict[str, str] = {
    ROLE_OWNER: "central",
    ROLE_ADMIN: "central",
    ROLE_USER: "user",
    ROLE_GUEST: "guest",
}


def capabilities_for(role: str, explicit: frozenset[str] | None = None) -> frozenset[str]:
    """Effective capability set for a person.

    Mirrors `auth.effective_scopes` exactly: an explicit grant (even an EMPTY
    one) wins outright; `None` means "derive from role"; an unknown role
    resolves to the empty set — **fails closed**."""
    if explicit is not None:
        return frozenset(explicit)
    return ROLE_CAPABILITIES.get(role, frozenset())


def has_capability(caps: frozenset[str] | None, cap: str) -> bool:
    return bool(caps) and cap in caps


def device_role_for_person(person_role: str) -> str:
    """The credential role this person's authority maps to. Unknown person role
    → `"guest"`, the weakest thing `devices.VALID_ROLES` has.

    Read on every authenticated request: `app.py`'s `_person_role` calls this and
    hands the answer to `auth._apply_person_cap`, which caps the credential at the
    narrower of (issued role, this). It is a ceiling, never a mint — `pairing.py`
    does not consult it."""
    return _PERSON_TO_DEVICE_ROLE.get(person_role, "guest")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _is_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    """Same semantics as `devices._is_expired`: None never expires, and a
    MALFORMED timestamp is treated as expired (fails closed)."""
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= (now or _utcnow())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS space (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    space_id    TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    created_ts  TEXT    NOT NULL,
    -- Monotonic generation counter for Core leadership (see core_registry.py).
    -- Lives on the Space, not on a Core, because it is the SPACE's notion of
    -- "which leadership decision is the current one".
    epoch       INTEGER NOT NULL DEFAULT 1,
    -- Free-form policy blob (JSON). Bounded on write; never executed.
    policy      TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS people (
    person_id    TEXT PRIMARY KEY,
    display_name TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    created_ts   TEXT    NOT NULL,
    -- Explicit capability grant, space-delimited. NULL = derive from role
    -- (the backward-compatible default, same idiom as devices.scopes).
    capabilities TEXT,
    -- Guests expire on their own; everyone else is NULL.
    expires_at   TEXT,
    -- PERSONALIZATION, deliberately separate from authorization (§29): a
    -- preference must never be able to widen a permission, so it lives in its
    -- own opaque JSON column that no gate ever reads.
    profile      TEXT    NOT NULL DEFAULT '{}',
    removed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS person_devices (
    device_id  TEXT PRIMARY KEY,
    person_id  TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    -- How the association was made. 'confirmed' = a human said yes;
    -- 'inferred' = discovery guessed and NOBODY has confirmed it. The
    -- distinction is load-bearing for §36: Wavr may detect "a Samsung phone",
    -- it must never silently decide "this is Augusto".
    origin     TEXT NOT NULL DEFAULT 'confirmed'
);

CREATE TABLE IF NOT EXISTS device_functions (
    device_id   TEXT PRIMARY KEY,
    functions   TEXT    NOT NULL DEFAULT '',   -- space-delimited: core node client
    platform    TEXT    NOT NULL DEFAULT '',
    room        TEXT    NOT NULL DEFAULT '',
    -- A Core on a phone can physically move. When it does, silently keeping its
    -- sensors bound to the old room would be a lie (§31), so portability is
    -- recorded and a move raises a Discovery instead of being absorbed.
    portable    INTEGER NOT NULL DEFAULT 0,
    updated_ts  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS device_capabilities (
    device_id  TEXT PRIMARY KEY,
    manifest   TEXT NOT NULL,                  -- JSON CapabilityManifest
    updated_ts TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Space:
    space_id: str
    name: str
    kind: str
    created_ts: str
    epoch: int = 1
    policy: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"space_id": self.space_id, "name": self.name, "kind": self.kind,
                "created_ts": self.created_ts, "epoch": self.epoch,
                "policy": dict(self.policy)}


@dataclass(frozen=True)
class Person:
    person_id: str
    display_name: str
    role: str
    created_ts: str
    capabilities: frozenset[str] | None = None
    expires_at: str | None = None
    profile: dict = field(default_factory=dict)
    removed: bool = False

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id, "display_name": self.display_name,
            "role": self.role, "created_ts": self.created_ts,
            # Always the RESOLVED set, so a caller never has to know about the
            # NULL-derives-from-role rule to render an accurate answer.
            "capabilities": sorted(capabilities_for(self.role, self.capabilities)),
            "expires_at": self.expires_at, "profile": dict(self.profile),
            "expired": _is_expired(self.expires_at),
        }


@dataclass(frozen=True)
class DeviceFunctions:
    device_id: str
    functions: frozenset[str]
    platform: str = ""
    room: str = ""
    portable: bool = False
    updated_ts: str = ""

    def to_dict(self) -> dict:
        return {"device_id": self.device_id, "functions": sorted(self.functions),
                "platform": self.platform, "room": self.room,
                "portable": self.portable, "updated_ts": self.updated_ts}


class SpaceError(ValueError):
    """Bad input from a caller — the API layer turns this into a 400/422."""


_MAX_NAME = 64
_MAX_ROOM = 64
_MAX_PROFILE_BYTES = 4096
_MAX_POLICY_BYTES = 8192
_MAX_PEOPLE = 200          # a Space is a building, not a directory service


def _clean_name(raw: str, what: str = "name") -> str:
    name = " ".join(str(raw or "").split())[:_MAX_NAME]
    if not name:
        raise SpaceError(f"{what} must not be empty")
    return name


def _dump_json(obj, limit: int, what: str) -> str:
    if obj is None:
        return "{}"
    if not isinstance(obj, dict):
        raise SpaceError(f"{what} must be an object")
    blob = json.dumps(obj, separators=(",", ":"))
    if len(blob.encode("utf-8")) > limit:
        raise SpaceError(f"{what} is too large")
    return blob


def _load_json(blob: str | None) -> dict:
    try:
        val = json.loads(blob or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _split(blob: str | None) -> frozenset[str] | None:
    """NULL -> None ('derive'); '' -> empty frozenset (an EXPLICIT empty grant).
    The two must stay distinguishable, same as devices._parse_scopes."""
    if blob is None:
        return None
    return frozenset(p for p in blob.split() if p)


def _join(items: frozenset[str] | None) -> str | None:
    if items is None:
        return None
    return " ".join(sorted(items))


class SpaceStore:
    """SQLite-backed Space / people / device-function registry."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def healthy(self) -> bool:
        """Can this store still be WRITTEN to?

        `runtime_status.assess` carries a storage finding — "Wavr cannot write
        to its database. Nothing is being recorded." — and `app.py` fed it
        `db_ok=True` because nothing implemented this method. The state was
        therefore unreachable: a full disk, a read-only mount or a deleted file
        left every surface saying everything was fine while nothing was being
        recorded. A failure state that cannot be reached is not a safeguard, it
        is a sentence in a docstring.

        A read would not answer the question, because SQLite keeps serving
        reads from a database it can no longer write.

        So this takes a write LOCK and immediately drops it. `BEGIN IMMEDIATE`
        acquires the RESERVED lock a write needs — it fails on a read-only
        file, a read-only mount and a database locked by another process — and
        `ROLLBACK` releases it having changed nothing.

        The first version of this wrote a row into a scratch table and rolled
        back, which was wrong twice. Python's sqlite3 does not open an implicit
        transaction around DDL, so the `CREATE TABLE` committed and the rollback
        took only the insert: the probe left a permanent `_health_probe` table
        behind. `test_data_inventory_is_complete` caught it immediately — every
        table Wavr creates has to be named on the privacy screen, and this one
        was a table the product could not explain to the household whose
        database it was in.
        """
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("ROLLBACK")
            return True
        except sqlite3.Error:
            # An in-flight transaction of our own would raise here too. Leave
            # the connection usable rather than half-open.
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            return False

    # -- Space ---------------------------------------------------------------

    def get_space(self) -> Space | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT space_id, name, kind, created_ts, epoch, policy"
                " FROM space WHERE id = 1").fetchone()
        if row is None:
            return None
        return Space(space_id=row["space_id"], name=row["name"], kind=row["kind"],
                     created_ts=row["created_ts"], epoch=int(row["epoch"]),
                     policy=_load_json(row["policy"]))

    def create_space(self, name: str, kind: str = DEFAULT_SPACE_KIND,
                     space_id: str | None = None) -> Space:
        """Create THE Space this Core serves. A Core serves exactly one Space —
        the `CHECK (id = 1)` is that rule in the schema rather than in prose.

        `space_id` is passed in when *joining* an existing Space (a second Core
        adopting the first Core's identity); omitted when creating a new one."""
        if self.get_space() is not None:
            raise SpaceError("this Core already belongs to a Space")
        name = _clean_name(name, "space name")
        kind = str(kind or DEFAULT_SPACE_KIND).strip().lower()
        if kind not in SPACE_KINDS:
            kind = "other"
        sid = str(space_id or secrets.token_hex(16))
        if not (8 <= len(sid) <= 64) or not sid.replace("-", "").isalnum():
            raise SpaceError("space_id must be 8-64 alphanumeric characters")
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO space (id, space_id, name, kind, created_ts, epoch, policy)"
                " VALUES (1, ?, ?, ?, ?, 1, '{}')", (sid, name, kind, ts))
            self._conn.commit()
        return Space(space_id=sid, name=name, kind=kind, created_ts=ts, epoch=1)

    def rename_space(self, name: str, kind: str | None = None) -> Space:
        space = self._require_space()
        name = _clean_name(name, "space name")
        new_kind = space.kind
        if kind is not None:
            k = str(kind).strip().lower()
            new_kind = k if k in SPACE_KINDS else "other"
        with self._lock:
            self._conn.execute("UPDATE space SET name = ?, kind = ? WHERE id = 1",
                               (name, new_kind))
            self._conn.commit()
        return Space(space_id=space.space_id, name=name, kind=new_kind,
                     created_ts=space.created_ts, epoch=space.epoch,
                     policy=space.policy)

    def set_policy(self, policy: dict) -> Space:
        space = self._require_space()
        blob = _dump_json(policy, _MAX_POLICY_BYTES, "policy")
        with self._lock:
            self._conn.execute("UPDATE space SET policy = ? WHERE id = 1", (blob,))
            self._conn.commit()
        return Space(space_id=space.space_id, name=space.name, kind=space.kind,
                     created_ts=space.created_ts, epoch=space.epoch,
                     policy=_load_json(blob))

    def destroy_space(self) -> bool:
        """Unwind a Space whose setup never finished. Returns False if there was
        nothing to unwind.

        The ONLY caller is the first-run flow's failure path. Space creation is
        several writes (space row, Owner, Core registration, epoch bump) and only
        the first two are in this store; a failure in the later ones used to
        leave a Space that could never be completed and could never be retried,
        recoverable only by deleting the database.

        REFUSES once the Space has anything real attached — a paired device or a
        second person. At that point it is a household, not a failed setup, and
        no error handler should be able to erase it as a side effect. There is
        deliberately no HTTP route to this."""
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM space WHERE id = 1").fetchone() is None:
                return False
            attached = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM person_devices)"
                "     + (SELECT COUNT(*) FROM device_functions)"
                "     + (SELECT COUNT(*) FROM device_capabilities) AS n"
            ).fetchone()["n"]
            people = self._conn.execute(
                "SELECT COUNT(*) AS n FROM people WHERE removed = 0").fetchone()["n"]
            if attached or people > 1:
                raise SpaceError(
                    "this Space has devices or people attached — refusing to "
                    "delete it")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("DELETE FROM people")
                self._conn.execute("DELETE FROM space WHERE id = 1")
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return True

    def bump_epoch(self) -> int:
        """Advance the Space's leadership generation and return the new value.

        This is the single-writer primitive for Core promotion: a Core that
        believes it is primary at epoch N must stand down the moment it observes
        an epoch > N. Monotonic and never reused, so a stale Core coming back
        from a long sleep can always tell it lost, without a clock."""
        with self._lock:
            row = self._conn.execute("SELECT epoch FROM space WHERE id = 1").fetchone()
            if row is None:
                raise SpaceError("no Space on this Core")
            new = int(row["epoch"]) + 1
            self._conn.execute("UPDATE space SET epoch = ? WHERE id = 1", (new,))
            self._conn.commit()
        return new

    def _require_space(self) -> Space:
        space = self.get_space()
        if space is None:
            raise SpaceError("no Space on this Core")
        return space

    # -- People --------------------------------------------------------------

    def add_person(self, display_name: str, role: str = ROLE_USER,
                   expires_at: str | None = None,
                   capabilities: frozenset[str] | None = None,
                   profile: dict | None = None) -> Person:
        if role not in PERSON_ROLES:
            raise SpaceError(f"role must be one of {sorted(PERSON_ROLES)}")
        name = _clean_name(display_name, "display_name")
        if capabilities is not None:
            unknown = set(capabilities) - PERSON_CAPABILITIES
            if unknown:
                raise SpaceError(f"unknown capabilities: {sorted(unknown)}")
        if role == ROLE_OWNER and self._owner_count() > 0:
            # Exactly one Owner. Adding a second is how you accidentally end up
            # with two people who can each give the Space away; transfer is the
            # supported path (`transfer_ownership`), and it is atomic.
            raise SpaceError("this Space already has an Owner — use transfer instead")
        blob = _dump_json(profile, _MAX_PROFILE_BYTES, "profile")
        pid = secrets.token_hex(16)
        ts = self._now()
        with self._lock:
            live = self._conn.execute(
                "SELECT COUNT(*) AS n FROM people WHERE removed = 0").fetchone()["n"]
            if live >= _MAX_PEOPLE:
                raise SpaceError("this Space has reached its people limit")
            self._conn.execute(
                "INSERT INTO people (person_id, display_name, role, created_ts,"
                " capabilities, expires_at, profile, removed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (pid, name, role, ts, _join(capabilities), expires_at, blob))
            self._conn.commit()
        return Person(person_id=pid, display_name=name, role=role, created_ts=ts,
                      capabilities=capabilities, expires_at=expires_at,
                      profile=_load_json(blob))

    def get_person(self, person_id: str) -> Person | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM people WHERE person_id = ?", (person_id,)).fetchone()
        return self._row_to_person(row) if row else None

    def list_people(self, include_removed: bool = False) -> list[Person]:
        sql = "SELECT * FROM people"
        if not include_removed:
            sql += " WHERE removed = 0"
        sql += " ORDER BY created_ts ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_person(r) for r in rows]

    def set_person_role(self, person_id: str, role: str) -> Person:
        """Change what a human may do. Deliberately does NOT touch that person's
        devices, and does not need to: the credential keeps whatever role it was
        issued with, and `auth._apply_person_cap` resolves the EFFECTIVE role as
        the lesser of the two, fresh on every request and on every live-stream
        re-check. So a demotion lands immediately without revoking anything, and
        a promotion never widens a token already out in the world.

        This docstring used to claim the opposite -- that a demotion "is always
        accompanied by an explicit revoke". That was the model before ADR-0011,
        and believing it is very likely how `/ws/live` came to skip the person cap
        and keep streaming to a demoted person. Revocation remains available and
        is still the right tool for a lost device; it is not required for a role
        change."""
        if role not in PERSON_ROLES:
            raise SpaceError(f"role must be one of {sorted(PERSON_ROLES)}")
        person = self.get_person(person_id)
        if person is None:
            raise SpaceError("unknown person")
        if person.role == ROLE_OWNER and role != ROLE_OWNER:
            raise SpaceError("the Owner cannot be demoted — transfer ownership first")
        if role == ROLE_OWNER:
            raise SpaceError("use transfer_ownership to make someone the Owner")
        with self._lock:
            self._conn.execute("UPDATE people SET role = ? WHERE person_id = ?",
                               (role, person_id))
            self._conn.commit()
        return self.get_person(person_id)      # type: ignore[return-value]

    def set_person_profile(self, person_id: str, profile: dict) -> Person:
        """Personalization only. This can never change a permission — `profile`
        is not read by `capabilities_for` or by any gate (§29)."""
        if self.get_person(person_id) is None:
            raise SpaceError("unknown person")
        blob = _dump_json(profile, _MAX_PROFILE_BYTES, "profile")
        with self._lock:
            self._conn.execute("UPDATE people SET profile = ? WHERE person_id = ?",
                               (blob, person_id))
            self._conn.commit()
        return self.get_person(person_id)      # type: ignore[return-value]

    def remove_person(self, person_id: str) -> list[str]:
        """Soft-remove a person and return the device ids that were theirs, so
        the caller can revoke those credentials. Returning them rather than
        revoking here keeps this store free of any auth authority — revocation
        stays `DeviceStore`'s job, and the API layer does both in one step."""
        person = self.get_person(person_id)
        if person is None:
            raise SpaceError("unknown person")
        if person.role == ROLE_OWNER:
            raise SpaceError("the Owner cannot be removed — transfer ownership first")
        devices = self.devices_of(person_id)
        with self._lock:
            self._conn.execute("UPDATE people SET removed = 1 WHERE person_id = ?",
                               (person_id,))
            self._conn.execute("DELETE FROM person_devices WHERE person_id = ?",
                               (person_id,))
            self._conn.commit()
        return devices

    def transfer_ownership(self, to_person_id: str) -> tuple[Person, Person]:
        """Atomically move Owner from the current holder to `to_person_id`, who
        becomes Owner while the previous Owner becomes Admin. Returns
        (new_owner, previous_owner). One transaction, so there is never a moment
        with two Owners or none."""
        target = self.get_person(to_person_id)
        if target is None or target.removed:
            raise SpaceError("unknown person")
        if target.role == ROLE_OWNER:
            raise SpaceError("that person is already the Owner")
        with self._lock:
            cur = self._conn.execute(
                "SELECT person_id FROM people WHERE role = ? AND removed = 0",
                (ROLE_OWNER,)).fetchone()
            if cur is None:
                raise SpaceError("this Space has no Owner to transfer from")
            prev_id = cur["person_id"]
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("UPDATE people SET role = ? WHERE person_id = ?",
                                   (ROLE_ADMIN, prev_id))
                self._conn.execute("UPDATE people SET role = ? WHERE person_id = ?",
                                   (ROLE_OWNER, to_person_id))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return (self.get_person(to_person_id),      # type: ignore[arg-type]
                self.get_person(prev_id))           # type: ignore[arg-type]

    def _owner_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM people WHERE role = ? AND removed = 0",
                (ROLE_OWNER,)).fetchone()["n"]

    @staticmethod
    def _row_to_person(row: sqlite3.Row) -> Person:
        return Person(
            person_id=row["person_id"], display_name=row["display_name"],
            role=row["role"], created_ts=row["created_ts"],
            capabilities=_split(row["capabilities"]),
            expires_at=row["expires_at"], profile=_load_json(row["profile"]),
            removed=bool(row["removed"]),
        )

    # -- Person <-> device associations --------------------------------------

    def associate_device(self, device_id: str, person_id: str,
                         origin: str = "confirmed") -> None:
        """Bind a device to a person.

        `origin` must be `"confirmed"` (a human said yes) or `"inferred"`
        (discovery's guess, unconfirmed). §36 turns on this distinction: seeing
        a Samsung phone on the LAN is evidence, not identity, and an inferred
        association must never be rendered as fact."""
        if origin not in ("confirmed", "inferred"):
            raise SpaceError("origin must be 'confirmed' or 'inferred'")
        if self.get_person(person_id) is None:
            raise SpaceError("unknown person")
        with self._lock:
            self._conn.execute(
                "INSERT INTO person_devices (device_id, person_id, created_ts, origin)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(device_id) DO UPDATE SET person_id = excluded.person_id,"
                " origin = excluded.origin",
                (device_id, person_id, self._now(), origin))
            self._conn.commit()

    def disassociate_device(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM person_devices WHERE device_id = ?", (device_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def person_of_device(self, device_id: str) -> tuple[str, str] | None:
        """(person_id, origin) or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT person_id, origin FROM person_devices WHERE device_id = ?",
                (device_id,)).fetchone()
        return (row["person_id"], row["origin"]) if row else None

    def devices_of(self, person_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id FROM person_devices WHERE person_id = ?"
                " ORDER BY created_ts ASC", (person_id,)).fetchall()
        return [r["device_id"] for r in rows]

    # -- Device functions + capabilities -------------------------------------

    def set_functions(self, device_id: str, functions, platform: str = "",
                      room: str = "", portable: bool = False) -> DeviceFunctions:
        from wavr.capabilities import DEVICE_FUNCTIONS

        wanted = frozenset(str(f).strip().lower() for f in (functions or []))
        unknown = wanted - DEVICE_FUNCTIONS
        if unknown:
            raise SpaceError(f"unknown device functions: {sorted(unknown)}")
        ts = self._now()
        room = " ".join(str(room or "").split())[:_MAX_ROOM]
        platform = str(platform or "").strip().lower()[:32]
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_functions"
                " (device_id, functions, platform, room, portable, updated_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(device_id) DO UPDATE SET functions = excluded.functions,"
                " platform = excluded.platform, room = excluded.room,"
                " portable = excluded.portable, updated_ts = excluded.updated_ts",
                (device_id, " ".join(sorted(wanted)), platform, room,
                 int(bool(portable)), ts))
            self._conn.commit()
        return DeviceFunctions(device_id=device_id, functions=wanted,
                               platform=platform, room=room,
                               portable=bool(portable), updated_ts=ts)

    def get_functions(self, device_id: str) -> DeviceFunctions | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device_functions WHERE device_id = ?",
                (device_id,)).fetchone()
        if row is None:
            return None
        return DeviceFunctions(
            device_id=row["device_id"],
            functions=frozenset(p for p in (row["functions"] or "").split() if p),
            platform=row["platform"], room=row["room"],
            portable=bool(row["portable"]), updated_ts=row["updated_ts"])

    def list_functions(self) -> list[DeviceFunctions]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_functions ORDER BY updated_ts ASC").fetchall()
        return [DeviceFunctions(
            device_id=r["device_id"],
            functions=frozenset(p for p in (r["functions"] or "").split() if p),
            platform=r["platform"], room=r["room"],
            portable=bool(r["portable"]), updated_ts=r["updated_ts"]) for r in rows]

    def set_manifest(self, device_id: str, manifest) -> None:
        """Store a device's Capability Manifest. Accepts a `CapabilityManifest`
        or a raw dict; a raw dict is parsed through `from_dict` first so an
        off-spec claim from a LAN device can never land in the db verbatim."""
        from wavr.capabilities import CapabilityManifest

        if not isinstance(manifest, CapabilityManifest):
            manifest = CapabilityManifest.from_dict(manifest)
        blob = json.dumps(manifest.to_dict(), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_capabilities (device_id, manifest, updated_ts)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(device_id) DO UPDATE SET manifest = excluded.manifest,"
                " updated_ts = excluded.updated_ts",
                (device_id, blob, self._now()))
            self._conn.commit()

    def get_manifest(self, device_id: str):
        from wavr.capabilities import CapabilityManifest

        with self._lock:
            row = self._conn.execute(
                "SELECT manifest FROM device_capabilities WHERE device_id = ?",
                (device_id,)).fetchone()
        if row is None:
            return None
        return CapabilityManifest.from_dict(_load_json(row["manifest"]))

    def forget_device(self, device_id: str) -> None:
        """Drop every trace of a device from THIS store. Called after a revoke
        so an unpaired box leaves no orphan function/manifest/association rows."""
        with self._lock:
            for table in ("person_devices", "device_functions", "device_capabilities"):
                self._conn.execute(f"DELETE FROM {table} WHERE device_id = ?",
                                   (device_id,))
            self._conn.commit()

    # -- Legacy adoption -----------------------------------------------------

    def adopt_legacy(self, device_rows, owner_name: str = "Owner",
                     space_name: str = "My Home") -> Space:
        """Give an EXISTING pre-Space installation a Space without losing or
        inventing anything.

        Called once, on a db that has devices but no Space. Every existing
        `central` device becomes a device of a single synthesized Owner; every
        other device is left unassociated (we genuinely do not know whose it
        is, and guessing would be exactly the §36 mistake). Device auth roles
        are NOT touched — the credential each device holds keeps working
        byte-identically.

        ⚠️ This writes only the SPACE's half of the link. The half that
        ENFORCES is `devices.person_id`, which this store has no handle on, and
        the caller must set it for every id in the returned `adopted` list —
        `api_space.adopt_existing` does. Without that second write the person cap
        never fires on precisely the devices that hold the most authority."""
        space = self.get_space()
        if space is not None:
            return space
        space = self.create_space(space_name, DEFAULT_SPACE_KIND)
        owner = self.add_person(owner_name, ROLE_OWNER)
        for row in device_rows or []:
            did = getattr(row, "device_id", None) or (
                row.get("device_id") if isinstance(row, dict) else None)
            role = getattr(row, "role", None) or (
                row.get("role") if isinstance(row, dict) else None)
            if not did:
                continue
            if role == "central":
                self.associate_device(did, owner.person_id, origin="confirmed")
                self.set_functions(did, ["client"])
        return space

    def close(self) -> None:
        self._conn.close()
