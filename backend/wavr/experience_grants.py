"""Which experience, on which device, may read what.

## The hole this closes

`experience_manifest` defines a spatial scope vocabulary — `room.presence`,
`room.count`, `room.position`, `anchors.read`, `devices.read`,
`events.subscribe` — and its docstring says a person decides which an experience
gets, "enforced at the API gate".

Nothing enforced it. Every caller holding `presence:read` received the full room
census: the headcount, every anchor, every device in the room, regardless of what
its manifest asked for. The vocabulary drove validation *warnings* and nothing
else, and the module promising "a manifest is a request, never a grant" was
technically right and practically irrelevant, because the grant was total.

That is worse than not having the vocabulary. A stated guarantee nothing
implements is the exact failure this codebase spends its effort avoiding, and it
was caught in review rather than by the household it would have leaked to.

## The model, which is deliberately small

A grant is `(device, experience) -> scopes`. An admin makes it. Nothing else
can, and a manifest certainly cannot: the whole point is that asking and
receiving are different acts.

### ⚠ The DEVICE half is authenticated. The EXPERIENCE half is not.

The device id comes from the bearer token and cannot be forged. The experience
id arrives as a plain query parameter and is **self-asserted** — there is no
per-experience credential anywhere in Wavr today.

The practical consequence, stated plainly because a boundary nobody names is a
boundary everybody assumes: **two applications sharing one device's token share
that device's most-privileged grant.** An application granted only presence can
pass `?experience=` naming one that was granted `anchors.read`, and receive the
anchors.

So the honest description of this axis is *organisational, not adversarial*. It
keeps an operator's grants legible — "the recipe app may read anchors, the alarm
clock may not" — and it stops a well-behaved application from receiving more
than it asked for. It does not defend against a hostile application already
holding the device's credential, and it is not a security boundary between two
applications on the same device.

Closing it needs a credential minted per (device, experience) at install time,
which is a real feature and a decision, not an oversight to patch. Until that
exists this stays written down here, in `/api/experience/scopes`, and in a test
that fails if this paragraph is deleted while the hole remains.

**A caller with no grant gets the narrowest useful answer**, not an error and not
everything. Presence — is anybody in this room — and nothing more: no count, no
position, no anchors, no device list. That is enough for an application to be
worth running while it waits to be trusted, and it is the shape a household
would pick if asked.

**Loopback root is not scoped.** The Core's own screens, the developer tools and
the MCP surface run there, and they already hold every authority in the product;
inventing a second, weaker gate in front of them would only be a second thing to
keep correct.

## What no grant can ever include

Identity. There is no scope for it in `SPATIAL_SCOPES`, so there is nothing to
grant, and `experience.py` strips it before this module is ever consulted. Two
independent reasons a name cannot reach an experience, which is the right number
for the one thing that must not.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from wavr.experience_manifest import SCOPE_PRESENCE, SPATIAL_SCOPES

# What a caller with no grant receives. Presence and nothing else: enough for an
# application to do something useful while it waits to be trusted, and the
# narrowest answer that is not simply a refusal.
DEFAULT_SCOPES: frozenset[str] = frozenset({SCOPE_PRESENCE})

MAX_ID = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experience_grants (
    device_id     TEXT NOT NULL,
    experience_id TEXT NOT NULL,
    scopes        TEXT NOT NULL DEFAULT '',   -- space-delimited
    created_ts    TEXT NOT NULL,
    PRIMARY KEY (device_id, experience_id)
);
"""


class GrantError(ValueError):
    """A grant that would hand out something Wavr does not have to give."""


def _slug(value, what: str) -> str:
    s = " ".join(str(value or "").split())
    if not s or len(s) > MAX_ID:
        raise GrantError(f"{what} must be 1-{MAX_ID} characters")
    return s


class GrantStore:
    """Per-device, per-experience spatial scopes. Shares `wavr.db`."""

    def __init__(self, path: str = "wavr.db", now_fn=None):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def grant(self, device_id: str, experience_id: str, scopes) -> dict:
        """Give one experience on one device a set of spatial scopes.

        An unknown scope is REFUSED rather than dropped. A grant that silently
        ignored half of what an admin selected would leave them believing they
        had given more — or less — than they had, and neither mistake is visible
        afterwards.
        """
        device_id = _slug(device_id, "device_id")
        experience_id = _slug(experience_id, "experience_id")
        wanted = [str(s).strip() for s in (scopes or ()) if str(s).strip()]
        unknown = [s for s in wanted if s not in SPATIAL_SCOPES]
        if unknown:
            raise GrantError(
                f"unknown scopes {sorted(unknown)}; Wavr grants only "
                f"{sorted(SPATIAL_SCOPES)}")
        text = " ".join(sorted(set(wanted)))
        with self._lock:
            self._conn.execute(
                "INSERT INTO experience_grants (device_id, experience_id,"
                " scopes, created_ts) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(device_id, experience_id) DO UPDATE SET"
                " scopes = excluded.scopes",
                (device_id, experience_id, text, self._now()))
            self._conn.commit()
        return {"device_id": device_id, "experience_id": experience_id,
                "scopes": sorted(set(wanted))}

    def revoke(self, device_id: str, experience_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM experience_grants WHERE device_id = ?"
                " AND experience_id = ?", (device_id, experience_id))
            self._conn.commit()
        return cur.rowcount > 0

    def scopes_for(self, device_id: str | None,
                   experience_id: str = "") -> frozenset[str]:
        """What this caller may read. Never raises.

        A store that failed by raising would take a context route down with it;
        one that failed OPEN would hand out everything. So a failure resolves to
        the default — presence only — which is the same answer an ungranted
        caller gets and the safe direction to be wrong in.
        """
        if not device_id:
            return DEFAULT_SCOPES
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT scopes FROM experience_grants WHERE device_id = ?"
                    " AND experience_id = ?",
                    (device_id, experience_id or "")).fetchone()
        except sqlite3.Error:
            return DEFAULT_SCOPES
        if row is None:
            return DEFAULT_SCOPES
        granted = {s for s in (row["scopes"] or "").split() if s in SPATIAL_SCOPES}
        # A grant may be empty on purpose — "this experience gets nothing" is a
        # real thing an admin might mean. `presence` is still included, because
        # an application that cannot tell whether anybody is in the room has
        # nothing to run on, and the honest way to express "nothing" is to
        # revoke the device rather than to strand it.
        return frozenset(granted | DEFAULT_SCOPES)

    def list(self, device_id: str | None = None) -> list[dict]:
        sql = ("SELECT device_id, experience_id, scopes, created_ts"
               " FROM experience_grants")
        args: tuple = ()
        if device_id:
            sql += " WHERE device_id = ?"
            args = (device_id,)
        sql += " ORDER BY device_id, experience_id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"device_id": r["device_id"],
                 "experience_id": r["experience_id"],
                 "scopes": sorted(s for s in (r["scopes"] or "").split()),
                 "created_ts": r["created_ts"]} for r in rows]

    def forget_device(self, device_id: str) -> int:
        """Drop every grant for a device. Called when it is unpaired.

        Without this, revoking a device and pairing a replacement under the same
        id would hand the new one the old one's spatial scopes — which is the
        kind of inheritance nobody remembers granting.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM experience_grants WHERE device_id = ?", (device_id,))
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
