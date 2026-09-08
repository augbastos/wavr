"""Pairing-code + WS-ticket lifecycle for multi-device client auth (ADR-0006).

Two short-lived, single-use secrets live here, both fully in-memory (never
persisted — they are ephemeral by design):

  * Pairing codes  — a short, human-typeable code the central shows the operator;
    the companion redeems it once over the LAN to receive a per-device token.
  * WS tickets     — because a browser WebSocket handshake can't carry an
    `Authorization` header, an already-authenticated device swaps its token for a
    short-lived single-use ticket, then opens `/ws/live?ticket=...`.

Time is injectable (`now_fn`, defaulting to real UTC now) so TTL/expiry is
deterministic under test with zero real waiting.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from wavr.devices import VALID_ROLES

# Defaults from the spec: ~2-min pairing window, short-lived WS ticket.
CODE_TTL_SECONDS = 120
TICKET_TTL_SECONDS = 30
# Brute-force defense (audit H1): cap FAILED redeem attempts per window. With an
# 8-digit code (10^8) this makes guessing the live code within its window infeasible.
MAX_FAILED_ATTEMPTS = 10
ATTEMPT_WINDOW_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _PendingCode:
    role: str
    expires_at: datetime                       # the ~2-min REDEMPTION window
    # Guest mode: the SEPARATE, host-chosen deadline stamped onto the Device at
    # redeem (the guest token's own lifetime, hours not minutes). None for every
    # normal pairing -- only a guest code carries it.
    session_expires_at: datetime | None = None


@dataclass
class _PendingTicket:
    device_id: str
    expires_at: datetime


class PairingManager:
    """In-memory registry of live pairing codes and WS tickets, bound to a
    DeviceStore for the actual device creation on redeem. Nothing here is written
    to disk."""

    def __init__(self, store, now_fn=_utcnow,
                 code_ttl: float = CODE_TTL_SECONDS,
                 ticket_ttl: float = TICKET_TTL_SECONDS,
                 max_failed: int = MAX_FAILED_ATTEMPTS,
                 attempt_window: float = ATTEMPT_WINDOW_SECONDS):
        self._store = store
        self._now = now_fn
        self._code_ttl = code_ttl
        self._ticket_ttl = ticket_ttl
        self._max_failed = max_failed
        self._attempt_window = attempt_window
        self._codes: dict[str, _PendingCode] = {}
        self._tickets: dict[str, _PendingTicket] = {}
        # Brute-force defense, keyed PER SOURCE IP (sweep [4]/[13]): source_ip ->
        # timestamps of that host's recent FAILED redeems. A single unauth in-subnet
        # host flooding junk guesses can only saturate ITS OWN bucket, so it can no
        # longer lock out legitimate pairing (device or peer) from other hosts.
        # `None`/absent source_ip shares one "" bucket (backward-compatible with the
        # old single global list for callers that don't pass a source).
        self._failed: dict[str, list[datetime]] = {}

    # -- pairing codes -----------------------------------------------------
    def mint_code(self, role: str = "user") -> str:
        """Mint a one-time pairing code carrying the role the operator authorised
        (default `user`; `central` only when explicitly requested at the UI)."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r} (expected one of {sorted(VALID_ROLES)})")
        self._purge_expired()
        code = self._fresh_code()
        self._codes[code] = _PendingCode(role, self._now() + timedelta(seconds=self._code_ttl))
        return code

    def mint_guest_code(self, hours: float) -> str:
        """Mint a one-time GUEST pairing code. The code itself still lives only the
        normal ~2-min redemption window; `session_expires_at` is the SEPARATE,
        host-chosen deadline (`hours` from now) stamped onto the Device at redeem, so
        the guest credential dies on its own with no in-memory timer. `hours` must be
        positive (the /api/guest/invite endpoint clamps it to a sane max)."""
        if hours <= 0:
            raise ValueError("guest invite hours must be positive")
        self._purge_expired()
        code = self._fresh_code()
        now = self._now()
        self._codes[code] = _PendingCode(
            "guest", now + timedelta(seconds=self._code_ttl),
            session_expires_at=now + timedelta(hours=hours))
        return code

    def is_throttled(self, source_ip: str | None = None) -> bool:
        """Is this host currently over the failed-attempt cap?

        Exists so a caller can tell the two refusals apart. `redeem` returns
        `None` both for a wrong code and for a throttled host, and a screen that
        renders one sentence for both tells somebody holding the CORRECT code
        that their code is invalid -- then tells them the same about the next
        code they generate, because the block is on the host. Two rounds of that
        and "pairing is broken" is the reasonable conclusion.

        Read-only: it purges expired timestamps (which is what keeps the map
        bounded) and counts. It never records an attempt, so ASKING can never
        push somebody over the line.
        """
        now = self._now()
        self._purge_failed(now)
        return len(self._failed.get(source_ip or "", ())) >= self._max_failed

    def redeem(self, code: str, name: str, source_ip: str | None = None,
               device_key: str | None = None) -> tuple[str, str] | None:
        """Redeem a code for a new device: returns (device_id, token) once, or None
        if the code is unknown, already used, or expired. One-time: the code is
        consumed on the first attempt (valid or not) so it can't be reused.

        `device_key` is an opaque, already-hashed value the client says is the
        same on the same physical device tomorrow. When one is given, the
        credentials this device held BEFORE are revoked as part of the redeem:
        one phone, one live key. Without it nothing is matched and nothing is
        revoked — which is every caller that predates this and every client that
        cannot produce a stable value.

        Pairing the same phone four times gave the first user four live
        credentials to his home, three of them forgotten. Re-pairing is not a
        rare event: it happens on every reinstall.

        `source_ip` (the caller's `request.client.host`) keys the FAILED-attempt
        rate-limiter PER HOST (sweep [4]/[13]): one host's junk guesses throttle
        only that host, never everyone's pairing. `None` uses a shared bucket so
        callers that pre-date this param behave exactly as before."""
        now = self._now()
        # Rate-limit brute force (audit H1), now per source IP: count only FAILED
        # attempts so legit redeems are never throttled; over the cap in the window
        # FOR THIS IP, reject outright. Purge expired timestamps across all buckets
        # first so the map stays bounded even under a flood of distinct source IPs.
        self._purge_failed(now)
        key = source_ip or ""
        if len(self._failed.get(key, ())) >= self._max_failed:
            return None
        pending = self._codes.pop(code, None)       # consume the correct code on hit
        if pending is None or now >= pending.expires_at:
            self._failed.setdefault(key, []).append(now)   # wrong/expired guess -> counts (per IP)
            return None
        # A guest code carries a session deadline -> stamp it on the Device so its token
        # expires on its own. A NORMAL code passes NO expires_at kwarg at all, so this
        # call is byte-identical to before guest mode for every existing caller and every
        # test stub whose add() signature predates the kwarg (regression-safe).
        chave = (device_key or "").strip() or None
        # `device_key=` is passed only when there IS one. A test double or an
        # older store whose `add()` predates the kwarg keeps working untouched,
        # which is the same care the `expires_at` line below was written with.
        extras = {"device_key": chave} if chave else {}
        if pending.session_expires_at is not None:
            resultado = self._store.add(
                name, pending.role,
                expires_at=pending.session_expires_at.isoformat(), **extras)
        else:
            resultado = self._store.add(name, pending.role, **extras)

        # Retire what this same phone held before. AFTER the new credential
        # exists, never before: a redeem that revoked first and then failed to
        # mint would lock somebody out of their own home with no way back in.
        if chave and resultado:
            aposentar = getattr(self._store, "supersede_same_device", None)
            if callable(aposentar):
                try:
                    # Say it. The store returns the ids precisely so a
                    # credential does not disappear from somebody's list with
                    # no record of why, and an empty list is worth nothing in
                    # the log, so only a real retirement is reported.
                    retirados = aposentar(chave, resultado[0]) or []
                    if retirados:
                        logging.info(
                            "pairing: %d earlier credential(s) for this same "
                            "device retired: %s", len(retirados),
                            ", ".join(retirados))
                except Exception:      # noqa: BLE001
                    # The pairing succeeded and the person has a working key.
                    # A stale row left behind is a tidiness problem; failing the
                    # redeem over it would be a lockout.
                    logging.warning("pairing: could not retire this device's "
                                    "previous credentials", exc_info=True)
            else:
                # Louder than the failure path used to be. A store without the
                # method degrades all the way back to stacking live keys, and
                # that must not be the quietest outcome in this function.
                logging.warning("pairing: this store cannot retire a device's "
                                "earlier credentials; duplicates will "
                                "accumulate")
        return resultado

    # -- WS tickets --------------------------------------------------------
    def mint_ticket(self, device_id: str) -> str:
        """Issue a short-lived single-use ticket for an already-authenticated
        device to open `/ws/live?ticket=...`."""
        self._purge_expired()
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = _PendingTicket(
            device_id, self._now() + timedelta(seconds=self._ticket_ttl))
        return ticket

    def redeem_ticket(self, ticket: str) -> str | None:
        """Consume a ticket, returning its device_id, or None if unknown/used/
        expired. Single-use: consumed on the first attempt."""
        pending = self._tickets.pop(ticket, None)  # single-use
        if pending is None or self._now() >= pending.expires_at:
            return None
        return pending.device_id

    # -- internals ---------------------------------------------------------
    def _fresh_code(self) -> str:
        """An 8-digit code (10^8 space; audit H1), retried on the (astronomically
        unlikely) collision with a still-live code so we never silently clobber an
        outstanding pairing."""
        for _ in range(10):
            code = f"{secrets.randbelow(100_000_000):08d}"
            if code not in self._codes:
                return code
        return f"{secrets.randbelow(100_000_000):08d}"

    def _purge_expired(self) -> None:
        """Drop expired codes/tickets so the in-memory maps stay bounded even if a
        minted secret is never redeemed."""
        now = self._now()
        self._codes = {k: v for k, v in self._codes.items() if now < v.expires_at}
        self._tickets = {k: v for k, v in self._tickets.items() if now < v.expires_at}

    def _purge_failed(self, now: datetime) -> None:
        """Drop out-of-window failure timestamps and empty per-IP buckets so
        `self._failed` stays bounded no matter how many distinct source IPs have
        ever made a failed attempt (a flood of one-shot junk from spoofed/rotating
        IPs cannot grow the map unboundedly)."""
        cutoff = now - timedelta(seconds=self._attempt_window)
        pruned: dict[str, list[datetime]] = {}
        for ip, stamps in self._failed.items():
            keep = [t for t in stamps if t >= cutoff]
            if keep:
                pruned[ip] = keep
        self._failed = pruned
