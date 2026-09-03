"""Core topology for a Space: who is authoritative, who is standing by.

A **Core** runs the authoritative Wavr runtime for a Space — fusion, storage,
policy, the API. A Space may legitimately have several (a laptop, a Pi, a phone,
a NAS), and the architecture must not fight an operator who wants that (§8).
But at any instant exactly ONE of them is authoritative, and the product must
never be quietly wrong about which.

## The mechanism: a monotonic epoch, not a consensus protocol

Deliberately NOT Raft. A home Space with three Cores does not need distributed
consensus; it needs a rule that is impossible to misread. The rule is:

    The primary is the Core named by the HIGHEST epoch anyone has seen.

`epoch` lives on the Space (`space_store.bump_epoch`) and only ever increases.
Promotion = bump the epoch and stamp the new primary at that epoch. Everything
else falls out of that one fact:

  * **A stale Core cannot silently keep acting** — *once it observes the newer
    epoch*. A Core that believes it is primary at epoch 7 and sees epoch 8
    stands down immediately: no timeout, no clock, no quorum.

    **This is live.** `app.py`'s discovery loop polls every PAIRED peer Core
    (`_observe_peer_cores`, ~60s) over the certificate-pinned channel and feeds
    the answers to `observe_peer()` below.

    ⚠️ **Read this before touching the callers.** `peer_claims_primary` DEMOTES
    the local primary on the caller's say-so, so who is allowed to supply it is
    the entire security design:

      * Only a PAIRED peer may. Pairing means a pinned certificate and a bearer
        token, and `peer_client` completes that handshake before a single byte of
        application data leaves the socket.
      * mDNS may NOT. Anything on the segment can broadcast a `_wavr._tcp`
        record; feeding that here would let any device shut a household's Core
        down by claiming a huge epoch. Advertisements produce a Discovery Inbox
        card for a human and nothing else.
      * A compromised PAIRED peer can force this Core to stand down. That is
        accepted: such a peer already holds a central credential for this Space.
        An equal-epoch conflict is still surfaced (`VERDICT_CONTESTED`).

    Any new caller must meet the same bar, and must coerce `peer_epoch` to an int
    before calling.
  * **A Core that slept through three promotions still resolves correctly**, for
    the same reason: 4 < 7.
  * **Genuine split-brain is SURFACED, never absorbed.** Two Cores claiming
    primary at the SAME epoch is not something an epoch can resolve — it means
    two promotions happened without seeing each other. We break the tie
    deterministically (lowest `core_id` wins, so both sides independently
    compute the same answer and converge) *and* raise a Discovery so a human
    finds out. Converging silently would be the failure mode; converging
    loudly is the design.

## What is deliberately NOT here

**Automatic failover — refused, not pending.** Health, staleness and
`promotion_candidate()` all work and are used; what is absent is any code path
that promotes without a human, and that absence is the decision. A Core that
stops answering may be off, asleep, on another Wi-Fi, or in a drawer; promoting
a phone to run the house because a laptop lidded is a worse outcome than a
banner saying "your Core is offline". So `promotion_candidate()` names who
*should* take over and the operator pulls the trigger. Should that ever change,
promotion is already epoch-fenced, so an opt-in policy would supply a new
*trigger* against the same architecture.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

STATUS_PRIMARY = "primary"      # authoritative at the Space's current epoch
STATUS_STANDBY = "standby"      # synced, ready, not authoritative
STATUS_OFFLINE = "offline"      # not heard from within LEASE_SECONDS
CORE_STATUSES: frozenset[str] = frozenset({STATUS_PRIMARY, STATUS_STANDBY, STATUS_OFFLINE})

# How long a Core may go unheard-from before it is shown as offline. Generous on
# purpose: a laptop that lids for a coffee should not paint the dashboard red.
LEASE_SECONDS = 120.0

# Verdicts from `observe_peer` — what THIS Core should do about what it just saw.
VERDICT_HOLD = "hold"            # we remain authoritative
VERDICT_YIELD = "yield"          # a higher epoch exists; we stand down now
VERDICT_CONTESTED = "contested"  # same epoch, two claimants — tie broken + flagged

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cores (
    core_id          TEXT PRIMARY KEY,
    space_id         TEXT    NOT NULL,
    name             TEXT    NOT NULL,
    base_url         TEXT    NOT NULL DEFAULT '',
    cert_fingerprint TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'standby',
    -- The epoch at which this Core was made primary. Meaningless for a standby;
    -- compared against the Space's epoch to detect staleness.
    epoch            INTEGER NOT NULL DEFAULT 0,
    platform         TEXT    NOT NULL DEFAULT '',
    -- A Core on battery hardware can physically move rooms (SS31).
    portable         INTEGER NOT NULL DEFAULT 0,
    room             TEXT    NOT NULL DEFAULT '',
    -- Set on exactly one row: the Core this process IS.
    is_self          INTEGER NOT NULL DEFAULT 0,
    last_seen_ts     TEXT,
    -- Opaque health blob (battery %, wifi quality, version...). Rendered, never
    -- branched on, so a future Core can report a richer shape safely.
    health           TEXT    NOT NULL DEFAULT '{}',
    created_ts       TEXT    NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass(frozen=True)
class Core:
    core_id: str
    space_id: str
    name: str
    base_url: str = ""
    cert_fingerprint: str = ""
    status: str = STATUS_STANDBY
    epoch: int = 0
    platform: str = ""
    portable: bool = False
    room: str = ""
    is_self: bool = False
    last_seen_ts: str | None = None
    health: dict = field(default_factory=dict)
    created_ts: str = ""

    def stale(self, now: datetime | None = None, lease: float = LEASE_SECONDS) -> bool:
        """True when this Core has not checked in within the lease. A Core that
        has NEVER checked in (`last_seen_ts is None`) counts as stale — never as
        healthy-by-default."""
        seen = _parse_ts(self.last_seen_ts)
        if seen is None:
            return True
        return (now or _utcnow()) - seen > timedelta(seconds=lease)

    def to_dict(self, now: datetime | None = None) -> dict:
        stale = self.stale(now)
        return {
            "core_id": self.core_id, "space_id": self.space_id, "name": self.name,
            "base_url": self.base_url, "cert_fingerprint": self.cert_fingerprint,
            # An offline PRIMARY is reported as `primary` with `stale: true`
            # rather than being silently rewritten to `offline` — losing contact
            # with the primary is not the same event as it handing over, and
            # collapsing the two is how an operator ends up not knowing which.
            "status": self.status, "stale": stale,
            "effective_status": STATUS_OFFLINE if stale and self.status != STATUS_PRIMARY
                                else self.status,
            "epoch": self.epoch, "platform": self.platform,
            "portable": self.portable, "room": self.room, "is_self": self.is_self,
            "last_seen_ts": self.last_seen_ts, "health": dict(self.health),
            "created_ts": self.created_ts,
        }


class CoreRegistryError(ValueError):
    """Bad input — the API layer turns this into a 400/422."""


def _load_json(blob: str | None) -> dict:
    try:
        v = json.loads(blob or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


class CoreRegistry:
    """SQLite-backed Core topology. Shares `wavr.db`, owns the `cores` table."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- Registration --------------------------------------------------------

    def register(self, core_id: str, space_id: str, name: str, *,
                 base_url: str = "", cert_fingerprint: str = "",
                 platform: str = "", portable: bool = False, room: str = "",
                 is_self: bool = False, health: dict | None = None) -> Core:
        """Add or update a Core row. Idempotent — re-registering the same
        `core_id` refreshes its metadata and its last-seen stamp without
        touching its `status` or `epoch`, so a restart never self-promotes."""
        core_id = str(core_id or "").strip()
        if not (8 <= len(core_id) <= 64):
            raise CoreRegistryError("core_id must be 8-64 characters")
        name = " ".join(str(name or "").split())[:64] or core_id[:12]
        ts = self._now()
        blob = json.dumps(health or {}, separators=(",", ":"))[:4096]
        with self._lock:
            if is_self:
                # Exactly one row may be `is_self`; a machine cannot be two Cores.
                self._conn.execute("UPDATE cores SET is_self = 0 WHERE core_id != ?",
                                   (core_id,))
            self._conn.execute(
                "INSERT INTO cores (core_id, space_id, name, base_url,"
                " cert_fingerprint, status, epoch, platform, portable, room,"
                " is_self, last_seen_ts, health, created_ts)"
                " VALUES (?, ?, ?, ?, ?, 'standby', 0, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(core_id) DO UPDATE SET"
                "   name = excluded.name, base_url = excluded.base_url,"
                "   cert_fingerprint = excluded.cert_fingerprint,"
                "   platform = excluded.platform, portable = excluded.portable,"
                "   room = excluded.room, is_self = excluded.is_self,"
                "   last_seen_ts = excluded.last_seen_ts, health = excluded.health",
                (core_id, space_id, name, base_url, cert_fingerprint, platform,
                 int(bool(portable)), room, int(bool(is_self)), ts, blob, ts))
            self._conn.commit()
        return self.get(core_id)      # type: ignore[return-value]

    def heartbeat(self, core_id: str, health: dict | None = None) -> bool:
        """Refresh a Core's lease. Returns False for an unknown Core rather than
        creating one — a heartbeat is not an enrollment."""
        ts = self._now()
        with self._lock:
            if health is None:
                cur = self._conn.execute(
                    "UPDATE cores SET last_seen_ts = ? WHERE core_id = ?", (ts, core_id))
            else:
                cur = self._conn.execute(
                    "UPDATE cores SET last_seen_ts = ?, health = ? WHERE core_id = ?",
                    (ts, json.dumps(health, separators=(",", ":"))[:4096], core_id))
            self._conn.commit()
        return cur.rowcount > 0

    def get(self, core_id: str) -> Core | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cores WHERE core_id = ?",
                                     (core_id,)).fetchone()
        return self._row(row) if row else None

    def self_core(self) -> Core | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cores WHERE is_self = 1").fetchone()
        return self._row(row) if row else None

    def list_cores(self) -> list[Core]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cores ORDER BY created_ts ASC").fetchall()
        return [self._row(r) for r in rows]

    def primary(self) -> Core | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cores WHERE status = ? ORDER BY epoch DESC LIMIT 1",
                (STATUS_PRIMARY,)).fetchone()
        return self._row(row) if row else None

    def forget(self, core_id: str) -> bool:
        """Remove a Core from the topology. Refuses to remove the current
        primary — that would leave the Space with no authority and no record of
        why. Demote or promote someone else first."""
        core = self.get(core_id)
        if core is None:
            return False
        if core.status == STATUS_PRIMARY:
            raise CoreRegistryError(
                "cannot remove the primary Core — promote another Core first")
        with self._lock:
            self._conn.execute("DELETE FROM cores WHERE core_id = ?", (core_id,))
            self._conn.commit()
        return True

    # -- Leadership ----------------------------------------------------------

    def promote(self, core_id: str, new_epoch: int) -> Core:
        """Make `core_id` the primary at `new_epoch`, demoting everyone else.

        `new_epoch` MUST come from `SpaceStore.bump_epoch()` — this method does
        not invent one, so the Space's counter stays the single source of
        monotonicity. A non-increasing epoch is rejected: replaying an old
        promotion is exactly the split-brain this design exists to prevent."""
        core = self.get(core_id)
        if core is None:
            raise CoreRegistryError("unknown core")
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(epoch) AS e FROM cores").fetchone()
            highest = int(row["e"] or 0)
            if new_epoch <= highest:
                raise CoreRegistryError(
                    f"epoch {new_epoch} is not newer than {highest} — refusing to "
                    "replay a promotion")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE cores SET status = ? WHERE status = ?",
                    (STATUS_STANDBY, STATUS_PRIMARY))
                self._conn.execute(
                    "UPDATE cores SET status = ?, epoch = ? WHERE core_id = ?",
                    (STATUS_PRIMARY, int(new_epoch), core_id))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return self.get(core_id)      # type: ignore[return-value]

    def demote(self, core_id: str) -> Core:
        """Stand a Core down to standby. Used by graceful handoff and by
        `observe_peer`'s YIELD verdict."""
        if self.get(core_id) is None:
            raise CoreRegistryError("unknown core")
        with self._lock:
            self._conn.execute("UPDATE cores SET status = ? WHERE core_id = ?",
                               (STATUS_STANDBY, core_id))
            self._conn.commit()
        return self.get(core_id)      # type: ignore[return-value]

    def observe_peer(self, peer_core_id: str, peer_epoch: int,
                     peer_claims_primary: bool) -> str:
        """Reconcile what a peer Core claims against what we believe.

        CALLED BY `app.py`'s discovery loop (`_observe_peer_cores`), once per
        pass, for each PAIRED peer, over the certificate-pinned channel. Two
        requirements bind every caller: authenticate the peer first (this
        demotes the local primary on its say-so), and coerce `peer_epoch` to an
        int before calling — the comparisons below assume a number.

        Returns one of VERDICT_HOLD / VERDICT_YIELD / VERDICT_CONTESTED. Pure
        decision + local bookkeeping: it never calls the peer, so it is safe to
        run from a discovery loop.

        The tie-break for an equal-epoch conflict is `min(core_id)`. It is
        arbitrary but TOTAL and SYMMETRIC — both Cores run the identical
        comparison and reach the identical answer without talking — so the Space
        converges on one primary even while the humans work out what happened.
        The conflict is still reported, because converging quietly on a
        coin-flip is not the same as being correct."""
        me = self.self_core()
        if me is None or me.status != STATUS_PRIMARY:
            return VERDICT_HOLD          # not primary: nothing of ours to defend
        if not peer_claims_primary:
            return VERDICT_HOLD
        if peer_epoch > me.epoch:
            self.demote(me.core_id)
            return VERDICT_YIELD
        if peer_epoch < me.epoch:
            return VERDICT_HOLD          # the peer is stale; it will yield to us
        # Equal epochs, two claimants: a real split.
        if str(peer_core_id) < str(me.core_id):
            self.demote(me.core_id)
        return VERDICT_CONTESTED

    def promotion_candidate(self, now: datetime | None = None) -> Core | None:
        """Who *should* take over if the primary is gone — the input to a manual
        promotion, and the seam an opt-in auto-failover would later use.

        Returns None when the primary is healthy, when there is no primary at
        all (that is a setup problem, not a failover), or when no standby is
        itself healthy. Prefers a mains-powered Core over a portable one: a
        laptop that is plugged in is a better house-runner than a phone in a
        pocket, and picking the phone because it answered first is exactly the
        kind of technically-correct choice that makes a product feel broken."""
        now = now or _utcnow()
        primary = self.primary()
        if primary is None or not primary.stale(now):
            return None
        healthy = [c for c in self.list_cores()
                   if c.core_id != primary.core_id and not c.stale(now)]
        if not healthy:
            return None
        healthy.sort(key=lambda c: (c.portable, c.created_ts))
        return healthy[0]

    def topology(self, now: datetime | None = None) -> dict:
        """One shape the Admin UI can render without doing any reasoning of its
        own — including an honest `contested` flag and an honest "nobody is in
        charge" state, rather than a list the caller has to interpret."""
        now = now or _utcnow()
        cores = self.list_cores()
        primaries = [c for c in cores if c.status == STATUS_PRIMARY]
        candidate = self.promotion_candidate(now)
        return {
            "cores": [c.to_dict(now) for c in cores],
            "primary_core_id": primaries[0].core_id if len(primaries) == 1 else None,
            "contested": len(primaries) > 1,
            "leaderless": len(primaries) == 0 and bool(cores),
            "primary_stale": bool(primaries) and primaries[0].stale(now),
            "promotion_candidate": candidate.to_dict(now) if candidate else None,
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> Core:
        return Core(
            core_id=row["core_id"], space_id=row["space_id"], name=row["name"],
            base_url=row["base_url"], cert_fingerprint=row["cert_fingerprint"],
            status=row["status"], epoch=int(row["epoch"]),
            platform=row["platform"], portable=bool(row["portable"]),
            room=row["room"], is_self=bool(row["is_self"]),
            last_seen_ts=row["last_seen_ts"], health=_load_json(row["health"]),
            created_ts=row["created_ts"])

    def close(self) -> None:
        self._conn.close()
