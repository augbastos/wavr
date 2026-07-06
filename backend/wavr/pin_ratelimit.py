"""In-memory brute-force throttle for the Core Panel PIN verify route.

Mirrors wavr.pairing.PairingManager's failed-attempt window (same rationale:
only FAILED attempts count, so a legitimate unlock is never throttled by its
own success). Purely in-memory and per-process -- it resets on restart, an
acceptable tradeoff for a local kiosk unlock (the threat model is a same-
LAN/loopback guesser hammering the endpoint while the process is up, not a
persistent account-lockout across reboots). Time is injectable (`now_fn`) so
the window is deterministic under test with zero real waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PinAttemptLimiter:
    """Tracks recent FAILED PIN attempts in a rolling window. `locked()` is
    checked BEFORE touching the PinStore, so a caller under lockout never even
    reaches the (slow, by design) pbkdf2 compare."""

    def __init__(self, max_failed: int = MAX_FAILED_ATTEMPTS,
                 window: float = ATTEMPT_WINDOW_SECONDS, now_fn=_utcnow):
        self._max_failed = max_failed
        self._window = window
        self._now = now_fn
        self._failed: list[datetime] = []

    def locked(self) -> bool:
        now = self._now()
        self._failed = [t for t in self._failed
                        if t >= now - timedelta(seconds=self._window)]
        return len(self._failed) >= self._max_failed

    def record_failure(self) -> None:
        self._failed.append(self._now())

    def record_success(self) -> None:
        # A correct unlock clears the counter -- the legitimate operator isn't
        # penalized for earlier mistyped digits.
        self._failed.clear()
