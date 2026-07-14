""""Approve on the Core" pairing-request lifecycle (design 2026-07-11).

A companion that hasn't got a code yet asks the Core to let it in; the LOCAL
operator approves or denies the request on the Core itself. This is a SECOND,
additive onboarding path alongside the existing 8-digit `/api/pair` +
`/api/pair-code` flow (unchanged, still the fallback) -- same in-memory,
never-persisted, TTL'd, injectable-clock discipline as `PairingManager`
(pairing.py) and `NodeEnroller` (nodes.py), because this is ephemeral
handshake state, not a device record (ADR-0002: nothing sensitive here is
ever written to disk).

Three independent factors gate every minted token:
  1. an unguessable `request_id` (secrets.token_urlsafe(24), 192-bit) -- the
     capability to poll a request's status and pick up its token,
  2. a loopback-root Approve -- a LOCAL operator action; app.py gates
     approve/deny/list behind `require_local` + `require_root`, the same
     tier as the peers-admin / nodes-admin control planes, so even an
     authenticated remote 'central' peer can never approve itself, and
  3. a per-request `compare_code` (Bluetooth-SSP-style 6-digit numeric
     comparison, minted unique-among-pending in `create()`) that the operator
     must read off the companion's screen and echo back to `approve()` before
     ANY token is minted. Factor 2 alone is not enough: the Core's own cert
     fingerprint (shown for the transport-MitM check) is byte-identical for
     every pending request, so a racing impostor with a spoofed name is
     otherwise indistinguishable from the honest device on the approve
     prompt. `compare_code` closes that gap -- see `approve()`.

`create()`/`poll()` MINT NOTHING -- the only mint site is `approve()`, which
(on a correct `confirm_code` echo) calls the SAME `device_store.add(name,
role)` `/api/pair` already uses, so a device paired this way is
byte-identical (same token entropy/hashing) to one paired with the 8-digit
code.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Operator must notice the Core banner and walk over; a bit more generous than
# the 120s pairing-code TTL since there's no code to pre-read on the phone.
REQUEST_TTL_SECONDS = 180
# Token-pickup window after Approve: bounds how long a minted-but-not-yet-
# -collected token can sit in memory. Not single-use-on-read -- a dropped poll
# response (network blip) must be retryable within this window rather than
# stranding the operator's Approve.
APPROVAL_TTL_SECONDS = 120
# Bounded maps: an in-subnet flood must never grow memory unboundedly. Over
# either cap, the OLDEST record is evicted -- never a hard refuse (an evicted
# record just times out cleanly on the companion's next poll).
MAX_PENDING = 20
MAX_PENDING_PER_IP = 3

_MAX_NAME_LEN = 64
_MAX_PLATFORM_LEN = 32
_MAX_FP_LEN = 128

# Bluetooth-SSP-style numeric comparison (fix design 2026-07-11): the ONLY
# per-approval anchor before this was the Core's own cert_fingerprint, which
# is byte-identical for every pending request and therefore proves "this is
# the real hub" but never "which request is MY phone" -- a racing impostor
# with a spoofed name is indistinguishable from the honest device on the
# approve prompt. This code is unique-among-pending, single-use (consumed by
# approve()'s confirm_code check), and TTL'd with its parent record.
_COMPARE_CODE_DIGITS = 6


def _mint_compare_code() -> str:
    return f"{secrets.randbelow(10 ** _COMPARE_CODE_DIGITS):0{_COMPARE_CODE_DIGITS}d}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _PendingPairRequest:
    request_id: str
    requester_name: str
    platform: str | None
    source_ip: str | None
    reported_fp: str | None          # self-reported by the companion; UNTRUSTED,
                                       # never the anchor for the operator's
                                       # fingerprint comparison -- see PRODUCT/
                                       # design notes. Kept for future display,
                                       # deliberately NOT serialized by to_dict().
    created_at: datetime
    expires_at: datetime
    status: str = "pending"           # "pending" | "approved" | "denied"
    device_id: str | None = None      # set on approve
    token: str | None = None          # set on approve; delivered only via poll()
    compare_code: str = ""            # per-request numeric-comparison anchor
                                       # (Bluetooth SSP style) -- see
                                       # _mint_compare_code(). Returned to the
                                       # requester ONLY in create()'s own
                                       # response (its own pinned channel) and
                                       # to the operator ONLY via to_dict()
                                       # below. poll() deliberately never
                                       # returns it -- an in-subnet caller
                                       # must never learn any request's
                                       # (its own or another's) code by
                                       # polling.

    def to_dict(self) -> dict:
        """Admin list view -- loopback-root ONLY (`list_pending()` ->
        GET /api/pending-pairings). Includes `compare_code` because this
        dict never reaches an untrusted network caller; it is deliberately
        NOT returned by `poll()`, which a companion (honest or attacker) can
        reach unauthenticated in-subnet. Never includes `token` (delivered
        only through `poll()`, to the companion, over its own pinned
        channel) or `reported_fp` (attacker-controllable; must not be
        trusted/displayed as if it were the Core's own fingerprint)."""
        return {
            "request_id": self.request_id,
            "requester_name": self.requester_name,
            "platform": self.platform,
            "source_ip": self.source_ip,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "compare_code": self.compare_code,
        }


class PairApprovalManager:
    """In-memory registry of pending "approve on the Core" pairing requests,
    bound to a `DeviceStore` for the actual device creation on approve.
    Nothing here is written to disk (mirrors `PairingManager`/`NodeEnroller`)."""

    def __init__(self, store, now_fn=_utcnow,
                 request_ttl: float = REQUEST_TTL_SECONDS,
                 approval_ttl: float = APPROVAL_TTL_SECONDS,
                 max_pending: int = MAX_PENDING,
                 max_pending_per_ip: int = MAX_PENDING_PER_IP):
        self._store = store
        self._now = now_fn
        self._request_ttl = request_ttl
        self._approval_ttl = approval_ttl
        self._max_pending = max_pending
        self._max_pending_per_ip = max_pending_per_ip
        # Insertion-ordered (dict preserves insertion order): oldest-first
        # eviction below just walks this order.
        self._requests: dict[str, _PendingPairRequest] = {}

    # -- companion-facing (unauth, in-subnet-bounded by app.py) ------------
    def create(self, requester_name: str, source_ip: str | None = None,
               platform: str | None = None,
               reported_fp: str | None = None) -> tuple[str, str]:
        """Open a PENDING request and return `(request_id, compare_code)`.
        Mints no token. Raises ValueError for an empty/whitespace-only name
        (caller -> 400). `compare_code` is minted unique among the
        CURRENTLY-pending records (never reused while another pending
        request already holds it) so two simultaneously-pending requests can
        never show the operator the same number -- ambiguity there would
        defeat the whole point of the numeric-comparison gate."""
        name = (requester_name or "").strip()[:_MAX_NAME_LEN]
        if not name:
            raise ValueError("requester_name is required")
        platform_v = (platform or "").strip()[:_MAX_PLATFORM_LEN] or None
        fp_v = (reported_fp or "").strip()[:_MAX_FP_LEN] or None
        now = self._now()
        self._purge_expired(now)
        key = source_ip or ""
        self._evict_oldest_for_ip(key)
        self._evict_oldest_global()
        request_id = secrets.token_urlsafe(24)
        in_use = {r.compare_code for r in self._requests.values()
                  if r.status == "pending"}
        compare_code = _mint_compare_code()
        for _ in range(20):
            if compare_code not in in_use:
                break
            compare_code = _mint_compare_code()
        self._requests[request_id] = _PendingPairRequest(
            request_id=request_id, requester_name=name, platform=platform_v,
            source_ip=source_ip, reported_fp=fp_v,
            created_at=now, expires_at=now + timedelta(seconds=self._request_ttl),
            compare_code=compare_code,
        )
        return request_id, compare_code

    def poll(self, request_id: str) -> dict:
        """`{"status": "pending"}` | `{"status":"approved","device_id","token"}`
        | `{"status":"denied"}` | `{"status":"expired"}` (unknown or TTL'd out
        -- so the companion always reaches a clean terminal state, never hangs).
        `request_id` IS the capability: an unguessable 192-bit secret is the
        only thing that lets a caller read this record."""
        now = self._now()
        self._purge_expired(now)
        rec = self._requests.get(request_id)
        if rec is None:
            return {"status": "expired"}
        if rec.status == "approved":
            return {"status": "approved", "device_id": rec.device_id, "token": rec.token}
        if rec.status == "denied":
            return {"status": "denied"}
        return {"status": "pending"}

    # -- operator-facing (loopback-root only, gated by app.py) -------------
    def list_pending(self) -> list[dict]:
        now = self._now()
        self._purge_expired(now)
        return [r.to_dict() for r in self._requests.values() if r.status == "pending"]

    def approve(self, request_id: str, role: str = "user",
                confirm_code: str | None = None) -> str | None:
        """THE mint site: on a valid pending request WHOSE `compare_code` the
        operator has echoed back correctly, calls
        `device_store.add(requester_name, role)` -- the identical call
        `/api/pair` uses -- and returns the new `device_id`, or None if the
        request is unknown/expired/already decided, OR `confirm_code` is
        missing/doesn't match (fail-closed: no match confirmed -> no token,
        ever). This is the "operator match confirmed" gate that makes the
        numeric comparison load-bearing rather than cosmetic -- without it, a
        UI-level compare that the caller could skip would leave the same
        every-request-looks-the-same hole the fingerprint-only design had.
        `secrets.compare_digest` -- constant-time, mirrors token comparisons
        elsewhere in this codebase. The token itself is never returned here;
        it is collected by the companion via `poll()`."""
        now = self._now()
        self._purge_expired(now)
        rec = self._requests.get(request_id)
        if rec is None or rec.status != "pending":
            return None
        if not confirm_code or not secrets.compare_digest(
                str(confirm_code).encode("utf-8"), rec.compare_code.encode("utf-8")):
            # encode both operands: a non-ASCII operator-typed confirm_code would
            # otherwise raise TypeError -> uncaught 500 instead of a clean no-match.
            return None
        device_id, token = self._store.add(rec.requester_name, role)
        rec.status = "approved"
        rec.device_id = device_id
        rec.token = token
        # Fresh pickup window from the moment of approval, never shorter than
        # whatever was already left on the original request TTL.
        rec.expires_at = max(rec.expires_at, now + timedelta(seconds=self._approval_ttl))
        return device_id

    def deny(self, request_id: str) -> bool:
        rec = self._requests.get(request_id)
        if rec is None or rec.status != "pending":
            return False
        rec.status = "denied"
        return True

    # -- internals -----------------------------------------------------------
    def _purge_expired(self, now: datetime) -> None:
        self._requests = {k: v for k, v in self._requests.items() if now < v.expires_at}

    def _evict_oldest_for_ip(self, key: str) -> None:
        """A single flooding source IP can only ever saturate its OWN bucket
        (mirrors the per-IP discipline in pairing.py/nodes.py): once that IP
        already has `max_pending_per_ip` pending requests, its own oldest is
        dropped to make room, never another IP's."""
        mine = [k for k, v in self._requests.items()
                if (v.source_ip or "") == key and v.status == "pending"]
        while len(mine) >= self._max_pending_per_ip:
            oldest = min(mine, key=lambda k: self._requests[k].created_at)
            self._requests.pop(oldest, None)
            mine.remove(oldest)

    def _evict_oldest_global(self) -> None:
        """Global bound regardless of source IP diversity (a distributed flood
        from many IPs still can't grow the map past this)."""
        while len(self._requests) >= self._max_pending:
            oldest = min(self._requests, key=lambda k: self._requests[k].created_at)
            self._requests.pop(oldest, None)
