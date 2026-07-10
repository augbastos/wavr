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


# -- per-source-IP rate limiting (sweep [4]/[13]) --------------------------------

def test_rate_limit_is_per_source_ip():
    # One host saturating its failure budget must NOT lock out a DIFFERENT host's
    # legitimate redeem -- the whole point of keying the limiter per source IP.
    t = [1000.0]
    pm = PairingManager(FakeStore(), now_fn=_fixed_clock(t), max_failed=3, attempt_window=60)
    good = pm.mint_code("user")
    for _ in range(5):                        # attacker at .50 blows past the cap
        assert pm.redeem("00000000", "x", source_ip="192.168.1.50") is None
    # ...and is now throttled even on a correct guess (its OWN bucket is saturated).
    assert pm.redeem(good, "x", source_ip="192.168.1.50") is None
    # A different host redeeming the SAME live code still succeeds -- not collateral-blocked.
    assert pm.redeem(good, "y", source_ip="192.168.1.77") == ("dev-1", "token-1")


def test_per_ip_buckets_do_not_leak_across_hosts():
    # Failures from many distinct IPs must not aggregate into one global cap: each
    # host gets its own independent budget.
    t = [1000.0]
    pm = PairingManager(FakeStore(), now_fn=_fixed_clock(t), max_failed=2, attempt_window=60)
    good = pm.mint_code("user")
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"):
        assert pm.redeem("00000000", "x", source_ip=ip) is None   # 1 failure each, under cap
    # None of them tripped a shared global limit -> a fresh host still redeems the good code.
    assert pm.redeem(good, "z", source_ip="10.0.0.9") == ("dev-1", "token-1")


def test_source_ip_none_shares_a_bucket_backward_compatible():
    # Callers that don't pass source_ip (pre-existing behavior) share one "" bucket,
    # so the old global-limit semantics still hold for them.
    t = [1000.0]
    pm = PairingManager(FakeStore(), now_fn=_fixed_clock(t), max_failed=3, attempt_window=60)
    good = pm.mint_code("user")
    for _ in range(3):
        assert pm.redeem("00000000", "x") is None
    assert pm.redeem(good, "x") is None       # throttled on the shared default bucket
