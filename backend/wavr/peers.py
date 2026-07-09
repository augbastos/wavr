"""Peer-instance relationships for cross-instance fusion + portable admin
identity (2026-07-09 design spec, Phase 1). A "peer" is another Wavr instance
(Desktop, Core, a future second Core) this instance has mutually pointed at
via the exchange protocol in `api_peers.py` -- NOT a plain companion device
(phone/tablet), even though both end up as a `role=central` row in the SAME
`DeviceStore` (Wavr Pass draws no new role; see the design spec).

`PeerStore` is this instance's OWN-DIRECTION bookkeeping: for each peer, how
do WE reach THEM (base_url, pinned cert fingerprint, OUR credential -- the
token THEY issued to US). This is deliberately separate from `DeviceStore`
(which is the SERVER-perspective "who can present a token to ME" table) --
`DeviceStore` already gets its own row for the peer from the ordinary pairing
redeem; `PeerStore` is the CLIENT-perspective row this instance needs to
actively call them back (for fusion's `RemoteSource` in Phase 2, and for
remote config in Phase 4).

`local_device_id` links back to the `Device.device_id` `DeviceStore` assigned
this peer when they redeemed a code from us -- follow it to check their role/
revoked state via `DeviceStore.get()` rather than duplicating that here.

Token is stored PLAINTEXT (not hashed like `DeviceStore`'s tokens): unlike a
companion's token (which only ever needs to be VERIFIED, never re-sent), this
token is one WE must re-present as our own Authorization header on every
outbound call to the peer -- a hash can't be un-hashed to do that. This
mirrors how a client stores an OAuth access token it must keep sending, not
how a server stores a password it only ever compares against."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id          TEXT PRIMARY KEY,
    name             TEXT    NOT NULL,
    base_url         TEXT    NOT NULL,
    cert_fingerprint TEXT    NOT NULL,
    local_device_id  TEXT    NOT NULL,
    token            TEXT,
    room_map         TEXT    NOT NULL DEFAULT '{}',
    created_ts       TEXT    NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Peer:
    """A peer relationship, minus its live token (see `PeerStore.token_for`)."""

    peer_id: str
    name: str
    base_url: str
    cert_fingerprint: str
    local_device_id: str
    room_map: dict = field(default_factory=dict)
    created_ts: str = ""
    revoked: bool = False

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id, "name": self.name, "base_url": self.base_url,
            "cert_fingerprint": self.cert_fingerprint,
            "local_device_id": self.local_device_id, "room_map": dict(self.room_map),
            "created_ts": self.created_ts, "revoked": self.revoked,
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PeerStore:
    """SQLite-backed peer-relationship store. Shares the db file with
    `DeviceStore`/`Storage` but owns its own `peers` table, same pattern as
    every other *_store.py in this codebase."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, name: str, base_url: str, cert_fingerprint: str,
            local_device_id: str, token: str) -> str:
        peer_id = secrets.token_hex(16)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO peers (peer_id, name, base_url, cert_fingerprint,"
                " local_device_id, token, room_map, created_ts, revoked)"
                " VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 0)",
                (peer_id, name, base_url, cert_fingerprint, local_device_id, token, ts),
            )
            self._conn.commit()
        return peer_id

    def token_for(self, peer_id: str) -> str | None:
        """The token to present when WE call THEM, or None if unknown/revoked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT token, revoked FROM peers WHERE peer_id = ?", (peer_id,)
            ).fetchone()
        if row is None or row["revoked"]:
            return None
        return row["token"]

    def list(self) -> list[Peer]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT peer_id, name, base_url, cert_fingerprint, local_device_id,"
                " room_map, created_ts, revoked FROM peers ORDER BY created_ts, peer_id"
            ).fetchall()
        return [self._to_peer(r) for r in rows]

    def get(self, peer_id: str) -> Peer | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT peer_id, name, base_url, cert_fingerprint, local_device_id,"
                " room_map, created_ts, revoked FROM peers WHERE peer_id = ?",
                (peer_id,),
            ).fetchone()
        return self._to_peer(row) if row else None

    def set_room_map(self, peer_id: str, room_map: dict) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peers SET room_map = ? WHERE peer_id = ?",
                (json.dumps(room_map), peer_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def revoke(self, peer_id: str) -> bool:
        """Mark revoked AND clear the token (belt-and-suspenders: `token_for`
        already checks `revoked`, but a cleared token can't leak via a future
        code path that forgets to check the flag). Idempotent."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peers SET revoked = 1, token = NULL WHERE peer_id = ?",
                (peer_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _to_peer(r: sqlite3.Row) -> Peer:
        return Peer(
            peer_id=r["peer_id"], name=r["name"], base_url=r["base_url"],
            cert_fingerprint=r["cert_fingerprint"], local_device_id=r["local_device_id"],
            room_map=json.loads(r["room_map"] or "{}"),
            created_ts=r["created_ts"], revoked=bool(r["revoked"]),
        )

    def close(self) -> None:
        self._conn.close()
