"""Discovery Inbox — the queue that turns raw discovery into decisions.

Wavr already discovers a great deal: ARP, mDNS, SSDP, NetBIOS, SNMP, DHCP
fingerprints, ONVIF probes, BLE, peer Cores, node enrolment attempts. Until now
all of that landed in a network *table* — a list the operator had to read,
interpret and act on. This module is the missing half: an **inbox of things that
need a human decision**, each with a plain sentence and concrete buttons.

The governing rule is §15: **discover aggressively, activate conservatively.**
Passive observation runs freely. Nothing observed ever becomes active,
identified or trusted on its own. Every step from "we saw an ESP32" to "this is
the kitchen radar node" passes through an item in here that a person accepted.

## Three properties that make an inbox usable instead of noisy

1. **Deduplicated by subject.** A camera seen on every 15-second sweep is ONE
   item whose `last_seen` moves, not 5,760 items a day. `subject` is the caller's
   stable key (a MAC, a node id, a core id) — never a timestamp, never a
   generated uuid.

2. **Dismissal sticks.** A dismissed item does not come back just because the
   thing was seen again — that is how an inbox trains people to ignore it. It
   returns only if the *substance* changed (`fingerprint`, below): a camera you
   dismissed reappears if its model or address changed, not if it merely pinged.

3. **Confidence is carried, never rounded away.** "This looks like a compatible
   camera — 0.72" survives to the UI. §16's rule is that uncertain inference is
   never rendered as certainty, and the only way to keep that promise at the
   edge is to keep the number all the way through.

Nothing here persists anything ADR-0002 forbids: an item holds identifiers and
a short human sentence, never frames, never positions, never vitals.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# -- Kinds -------------------------------------------------------------------
# Every kind maps to one sentence the operator can act on. Adding a kind means
# adding a sentence and its buttons — never a raw event dump.
KIND_DEVICE_NEW = "device_new"            # a device we have not seen before
KIND_CAMERA_FOUND = "camera_found"        # an ONVIF/RTSP-looking camera
KIND_NODE_PENDING = "node_pending"        # a sensor node asking to enrol
KIND_PEER_CORE = "peer_core"              # another Wavr Core advertising itself
KIND_DEVICE_MOVED = "device_moved"        # a known device changed address
KIND_CORE_CONTESTED = "core_contested"    # two Cores claim primary at one epoch

# Deliberately ABSENT, and this note is here so they are not re-added as
# constants before they are re-added as features: a portable Core noticing it
# changed room, an unknown BLE device seen repeatedly, and a device that looks
# like it belongs to a person. All three were declared here once, complete with
# actions and frontend handlers, while nothing could produce them -- which reads
# as a shipped feature to anyone who greps. Each needs a detector that does not
# exist yet (a Core has no room signal; nothing accumulates BLE sightings across
# passes; nothing correlates an unknown device with a person's presence). Add
# the kind and its producer in the same change, or not at all.

DISCOVERY_KINDS: frozenset[str] = frozenset({
    KIND_DEVICE_NEW, KIND_CAMERA_FOUND, KIND_NODE_PENDING, KIND_PEER_CORE,
    KIND_DEVICE_MOVED, KIND_CORE_CONTESTED,
})

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"
DISCOVERY_STATUSES: frozenset[str] = frozenset(
    {STATUS_PENDING, STATUS_ACCEPTED, STATUS_DISMISSED})

# A pending item nobody touched for this long is pruned. Not a decision — an
# expiry. A phone that visited once and never came back should not sit in the
# inbox forever pretending to be a pending question.
STALE_PENDING_DAYS = 14
# Hard ceiling so a hostile or broken LAN cannot grow the table without bound.
# When full, the OLDEST pending item is evicted, never a newer one: on a noisy
# network the recent observations are the actionable ones.
MAX_PENDING = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discoveries (
    discovery_id TEXT PRIMARY KEY,
    kind         TEXT    NOT NULL,
    subject      TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    detail       TEXT    NOT NULL DEFAULT '{}',
    confidence   REAL    NOT NULL DEFAULT 0.0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    -- Hash of the substantive detail. A re-observation with the SAME
    -- fingerprint refreshes last_seen only; a CHANGED fingerprint reopens a
    -- dismissed item, because the thing itself changed.
    fingerprint  TEXT    NOT NULL DEFAULT '',
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    decided_ts   TEXT,
    UNIQUE (kind, subject)
);
"""

_MAX_TITLE = 160
_MAX_DETAIL_BYTES = 4096


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


class DiscoveryError(ValueError):
    """Bad input — the API layer turns this into a 400/422."""


@dataclass(frozen=True)
class Discovery:
    discovery_id: str
    kind: str
    subject: str
    title: str
    detail: dict = field(default_factory=dict)
    confidence: float = 0.0
    status: str = STATUS_PENDING
    fingerprint: str = ""
    first_seen: str = ""
    last_seen: str = ""
    decided_ts: str | None = None

    def to_dict(self) -> dict:
        return {
            "discovery_id": self.discovery_id, "kind": self.kind,
            "subject": self.subject, "title": self.title,
            "detail": dict(self.detail), "confidence": round(self.confidence, 3),
            "status": self.status, "first_seen": self.first_seen,
            "last_seen": self.last_seen, "decided_ts": self.decided_ts,
            "actions": list(ACTIONS_FOR_KIND.get(self.kind, DEFAULT_ACTIONS)),
        }


# What the UI may offer for each kind. Held here rather than in the frontend so
# the Core stays the single source of truth about what is actually possible —
# a button the backend cannot honour should not be renderable.
DEFAULT_ACTIONS: tuple[dict, ...] = (
    {"id": "dismiss", "label": "Ignore", "kind": "secondary"},
)
ACTIONS_FOR_KIND: dict[str, tuple[dict, ...]] = {
    KIND_DEVICE_NEW: (
        {"id": "assign_person", "label": "It's someone's", "kind": "primary"},
        {"id": "name", "label": "Name it", "kind": "secondary"},
        {"id": "dismiss", "label": "Ignore", "kind": "secondary"},
    ),
    KIND_CAMERA_FOUND: (
        {"id": "add_camera", "label": "Add to Wavr", "kind": "primary"},
        {"id": "dismiss", "label": "Ignore", "kind": "secondary"},
    ),
    KIND_NODE_PENDING: (
        {"id": "approve_node", "label": "Approve", "kind": "primary"},
        {"id": "dismiss", "label": "Deny", "kind": "danger"},
    ),
    KIND_PEER_CORE: (
        {"id": "pair_core", "label": "Connect", "kind": "primary"},
        {"id": "dismiss", "label": "Ignore", "kind": "secondary"},
    ),
    KIND_DEVICE_MOVED: (
        {"id": "acknowledge", "label": "Got it", "kind": "primary"},
    ),
    KIND_CORE_CONTESTED: (
        {"id": "open_cores", "label": "Review Cores", "kind": "primary"},
    ),
}


def _fingerprint(detail: dict) -> str:
    """Stable hash of the substantive detail. Keys that change on every
    observation (`last_seen`, `rssi`, counters) are excluded, so a re-sighting
    of an unchanged thing does not look like a change."""
    volatile = {"last_seen", "seen_ts", "rssi", "count", "age_s", "uptime"}
    stable = {k: v for k, v in sorted(detail.items()) if k not in volatile}
    blob = json.dumps(stable, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class DiscoveryInbox:
    """SQLite-backed inbox. Shares `wavr.db`, owns the `discoveries` table."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def observe(self, kind: str, subject: str, title: str, *,
                detail: dict | None = None, confidence: float = 0.0) -> Discovery:
        """Report a sighting. Idempotent per `(kind, subject)`.

        Returns the resulting item, whether it was created, refreshed or
        reopened. Callers are expected to invoke this from a discovery loop on
        every sweep — the deduplication is this method's job, not theirs."""
        if kind not in DISCOVERY_KINDS:
            raise DiscoveryError(f"unknown discovery kind: {kind}")
        subject = str(subject or "").strip()[:128]
        if not subject:
            raise DiscoveryError("subject must not be empty")
        title = " ".join(str(title or "").split())[:_MAX_TITLE]
        if not title:
            raise DiscoveryError("title must not be empty")
        detail = detail if isinstance(detail, dict) else {}
        blob = json.dumps(detail, separators=(",", ":"), default=str)
        if len(blob.encode("utf-8")) > _MAX_DETAIL_BYTES:
            raise DiscoveryError("detail is too large")
        conf = max(0.0, min(1.0, float(confidence or 0.0)))
        fp = _fingerprint(detail)
        now = self._now()

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM discoveries WHERE kind = ? AND subject = ?",
                (kind, subject)).fetchone()

            if row is None:
                self._evict_if_full_locked()
                did = hashlib.sha256(
                    f"{kind}|{subject}".encode("utf-8")).hexdigest()[:24]
                self._conn.execute(
                    "INSERT INTO discoveries (discovery_id, kind, subject, title,"
                    " detail, confidence, status, fingerprint, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (did, kind, subject, title, blob, conf, STATUS_PENDING, fp,
                     now, now))
                self._conn.commit()
                return Discovery(discovery_id=did, kind=kind, subject=subject,
                                 title=title, detail=detail, confidence=conf,
                                 status=STATUS_PENDING, fingerprint=fp,
                                 first_seen=now, last_seen=now)

            changed = row["fingerprint"] != fp
            status = row["status"]
            decided = row["decided_ts"]
            # A decided item reopens ONLY when the thing itself changed. This is
            # the anti-nag rule: seeing a dismissed camera again is not news.
            if changed and status in (STATUS_DISMISSED, STATUS_ACCEPTED):
                status, decided = STATUS_PENDING, None
            self._conn.execute(
                "UPDATE discoveries SET title = ?, detail = ?, confidence = ?,"
                " fingerprint = ?, last_seen = ?, status = ?, decided_ts = ?"
                " WHERE discovery_id = ?",
                (title, blob, conf, fp, now, status, decided, row["discovery_id"]))
            self._conn.commit()
            return Discovery(
                discovery_id=row["discovery_id"], kind=kind, subject=subject,
                title=title, detail=detail, confidence=conf, status=status,
                fingerprint=fp, first_seen=row["first_seen"], last_seen=now,
                decided_ts=decided)

    def _evict_if_full_locked(self) -> None:
        """Caller holds the lock. Drops the oldest PENDING rows once the table
        is at capacity; decided rows are kept (they are the memory that stops an
        item nagging) and are pruned by age in `prune` instead."""
        n = self._conn.execute(
            "SELECT COUNT(*) AS n FROM discoveries WHERE status = ?",
            (STATUS_PENDING,)).fetchone()["n"]
        if n < MAX_PENDING:
            return
        self._conn.execute(
            "DELETE FROM discoveries WHERE discovery_id IN ("
            "  SELECT discovery_id FROM discoveries WHERE status = ?"
            "  ORDER BY last_seen ASC LIMIT ?)",
            (STATUS_PENDING, n - MAX_PENDING + 1))

    def get(self, discovery_id: str) -> Discovery | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM discoveries WHERE discovery_id = ?",
                (discovery_id,)).fetchone()
        return self._row(row) if row else None

    def list_items(self, status: str | None = STATUS_PENDING,
                   kind: str | None = None, limit: int = 200) -> list[Discovery]:
        sql = "SELECT * FROM discoveries"
        clauses, args = [], []
        if status is not None:
            if status not in DISCOVERY_STATUSES:
                raise DiscoveryError(f"unknown status: {status}")
            clauses.append("status = ?")
            args.append(status)
        if kind is not None:
            if kind not in DISCOVERY_KINDS:
                raise DiscoveryError(f"unknown discovery kind: {kind}")
            clauses.append("kind = ?")
            args.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # Most recently seen first: an inbox is read from the top.
        sql += " ORDER BY last_seen DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def decide(self, discovery_id: str, status: str) -> Discovery:
        if status not in (STATUS_ACCEPTED, STATUS_DISMISSED):
            raise DiscoveryError("status must be 'accepted' or 'dismissed'")
        if self.get(discovery_id) is None:
            raise DiscoveryError("unknown discovery")
        with self._lock:
            self._conn.execute(
                "UPDATE discoveries SET status = ?, decided_ts = ?"
                " WHERE discovery_id = ?", (status, self._now(), discovery_id))
            self._conn.commit()
        return self.get(discovery_id)      # type: ignore[return-value]

    def counts(self) -> dict:
        """`{"pending": n, "accepted": n, "dismissed": n}` — the badge number."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM discoveries GROUP BY status"
            ).fetchall()
        out = {s: 0 for s in DISCOVERY_STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        return out

    def prune(self, days: int = STALE_PENDING_DAYS,
              now: datetime | None = None) -> int:
        """Drop pending items nobody has seen in `days`. Returns how many went."""
        cutoff = ((now or _utcnow()) - timedelta(days=max(1, int(days)))).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM discoveries WHERE status = ? AND last_seen < ?",
                (STATUS_PENDING, cutoff))
            self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _row(row: sqlite3.Row) -> Discovery:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        return Discovery(
            discovery_id=row["discovery_id"], kind=row["kind"],
            subject=row["subject"], title=row["title"],
            detail=detail if isinstance(detail, dict) else {},
            confidence=float(row["confidence"]), status=row["status"],
            fingerprint=row["fingerprint"], first_seen=row["first_seen"],
            last_seen=row["last_seen"], decided_ts=row["decided_ts"])

    def close(self) -> None:
        self._conn.close()


# -- Phrasing ----------------------------------------------------------------

def describe_device(name: str, vendor: str = "", kind: str = "",
                    confidence: float = 0.0) -> str:
    """One honest sentence for a newly-seen device.

    The confidence bands are the §16 rule made concrete: below 0.5 Wavr says it
    does not know, and it never upgrades a guess to a statement by phrasing."""
    label = name or vendor or "A device"
    if kind and confidence >= 0.8:
        return f"{label} — looks like {kind}."
    if kind and confidence >= 0.5:
        return f"{label} — possibly {kind}."
    if vendor and vendor != label:
        return f"{label} — made by {vendor}. We can't tell what it is."
    return f"{label} — we can't tell what this is yet."
