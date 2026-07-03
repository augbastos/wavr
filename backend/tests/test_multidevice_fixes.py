"""Regression tests for the multi-device security-audit fixes (ADR-0006).

Covers the module-testable fixes: pairing brute-force rate-limit (audit H1) and the
wider code space. The app.py wiring fixes (C1 role gate, M2 WS subnet, M3 pair
exemption, M1 revoke re-check) are enforced in create_app and covered by the audit +
the wiring integration test.
"""
from datetime import datetime, timezone

from wavr.pairing import PairingManager


class FakeStore:
    def add(self, name, role):
        return ("dev-1", "token-1")


def _fixed_clock(seconds):
    return lambda: datetime.fromtimestamp(seconds[0], timezone.utc)


def test_pairing_code_is_8_digits():
    code = PairingManager(FakeStore()).mint_code("user")
    assert len(code) == 8 and code.isdigit()


def test_pairing_rate_limits_brute_force():
    t = [1000.0]
    pm = PairingManager(FakeStore(), now_fn=_fixed_clock(t), max_failed=3, attempt_window=60)
    good = pm.mint_code("user")
    for _ in range(3):                       # exhaust the failure budget
        assert pm.redeem("00000000", "x") is None
    assert pm.redeem(good, "x") is None      # even the CORRECT code is now throttled


def test_rate_limit_window_recovers():
    t = [1000.0]
    pm = PairingManager(FakeStore(), now_fn=_fixed_clock(t), max_failed=3, attempt_window=60)
    good = pm.mint_code("user")
    for _ in range(3):
        pm.redeem("00000000", "x")
    t[0] += 61                                # window passes -> failures purged
    assert pm.redeem(good, "x") == ("dev-1", "token-1")


def test_successful_redeems_are_not_throttled():
    pm = PairingManager(FakeStore(), max_failed=2)
    for _ in range(5):                        # only FAILED attempts count
        c = pm.mint_code("user")
        assert pm.redeem(c, "x") == ("dev-1", "token-1")
