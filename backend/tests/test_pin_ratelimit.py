"""Unit tests for wavr.pin_ratelimit.PinAttemptLimiter -- injected clock, same
style as test_multidevice.py's PairingManager TTL tests (no real waiting)."""
from datetime import datetime, timedelta, timezone

from wavr.pin_ratelimit import PinAttemptLimiter


class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def test_not_locked_before_any_failures():
    limiter = PinAttemptLimiter(max_failed=3, window=60)
    assert limiter.locked() is False


def test_locks_after_max_failed_attempts():
    limiter = PinAttemptLimiter(max_failed=3, window=60)
    for _ in range(3):
        assert limiter.locked() is False
        limiter.record_failure()
    assert limiter.locked() is True


def test_unlocks_after_window_elapses():
    clock = Clock()
    limiter = PinAttemptLimiter(max_failed=2, window=60, now_fn=clock)
    limiter.record_failure()
    limiter.record_failure()
    assert limiter.locked() is True
    clock.advance(61)
    assert limiter.locked() is False   # old failures aged out of the window


def test_still_locked_just_before_window_elapses():
    clock = Clock()
    limiter = PinAttemptLimiter(max_failed=2, window=60, now_fn=clock)
    limiter.record_failure()
    limiter.record_failure()
    clock.advance(59)
    assert limiter.locked() is True


def test_success_clears_the_counter():
    limiter = PinAttemptLimiter(max_failed=3, window=60)
    limiter.record_failure()
    limiter.record_failure()
    limiter.record_success()
    assert limiter.locked() is False
    limiter.record_failure()
    limiter.record_failure()
    assert limiter.locked() is False   # only 2 failures since the reset (cap is 3)
