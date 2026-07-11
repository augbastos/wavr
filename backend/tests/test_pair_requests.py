"""PairApprovalManager (pair_requests.py) -- the "approve on the Core"
pairing-request lifecycle. Pure in-memory, injectable-clock, no I/O -- same
discipline as PairingManager/NodeEnroller (see test_multidevice.py).

Locks the invariants from the module's own docstring:
  - create() mints a per-request compare_code but NEVER a token.
  - approve() is the ONLY mint site, and only fires on a correct
    (operator-confirmed) compare_code echo -- factor 2 (loopback-root) alone
    is never enough.
  - the compare_code is single-use (consumed by the first successful
    approve()) and TTL'd with its parent record.
  - a second approve() of an already-decided request is a no-op/refused,
    never a second token.
  - deny() removes the pending record; an already-decided request can't be
    denied either.
  - the pending map is BOUNDED: flood (global or per-source-IP) evicts the
    OLDEST record, never a hard refuse.
  - compare_code is per-request: a code that matches request A can never
    unlock request B (cross-request confusion), and poll() -- the endpoint
    an in-subnet attacker can reach unauthenticated -- never returns any
    request's compare_code.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.pair_requests import PairApprovalManager


# --------------------------------------------------------------------------- #
# Injectable clock -- same shape as test_multidevice.py's Clock.
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# Fake device_store -- mirrors DeviceStore.add(name, role) -> (device_id, token)
# without touching the real (sqlite-backed) store; records every call so tests
# can assert exactly-once minting.
# --------------------------------------------------------------------------- #
class _FakeDeviceStore:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def add(self, name: str, role: str) -> tuple[str, str]:
        self._n += 1
        self.calls.append((name, role))
        return f"dev-{self._n}", f"tok-{self._n}"


def _mgr(clock: Clock | None = None, store: "_FakeDeviceStore | None" = None, **kwargs):
    store = store if store is not None else _FakeDeviceStore()
    now_fn = clock if clock is not None else Clock()
    return PairApprovalManager(store, now_fn=now_fn, **kwargs), store


# --------------------------------------------------------------------------- #
# create(): mints a compare_code, mints NOTHING else.
# --------------------------------------------------------------------------- #
def test_create_happy_path_mints_no_token():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone", source_ip="192.168.1.50", platform="ios")
    assert request_id and code
    assert store.calls == []                      # create() mints no token
    pending = mgr.list_pending()
    assert len(pending) == 1
    assert pending[0]["request_id"] == request_id
    assert pending[0]["compare_code"] == code
    assert pending[0]["status"] == "pending"


def test_compare_code_is_six_digit_numeric():
    mgr, _store = _mgr()
    _rid, code = mgr.create("phone")
    assert len(code) == 6 and code.isdigit()


def test_create_rejects_empty_or_whitespace_name():
    mgr, _store = _mgr()
    with pytest.raises(ValueError):
        mgr.create("")
    with pytest.raises(ValueError):
        mgr.create("   ")


def test_create_truncates_untrusted_oversized_fields():
    mgr, _store = _mgr()
    request_id, _code = mgr.create(
        "a" * 500, platform="b" * 500, reported_fp="c" * 500,
    )
    rec = mgr._requests[request_id]
    assert len(rec.requester_name) <= 64
    assert len(rec.platform) <= 32
    assert len(rec.reported_fp) <= 128


def test_create_optional_fields_default_none():
    mgr, _store = _mgr()
    request_id, _code = mgr.create("phone")
    rec = mgr._requests[request_id]
    assert rec.platform is None and rec.reported_fp is None


def test_to_dict_never_leaks_token_or_reported_fp():
    # reported_fp is attacker-controllable and must never be surfaced as if it
    # were trustworthy, even to the loopback-root operator list view.
    mgr, _store = _mgr()
    mgr.create("phone", reported_fp="attacker-spoofed-fingerprint")
    d = mgr.list_pending()[0]
    assert "reported_fp" not in d
    assert "token" not in d


def test_compare_code_unique_among_concurrently_pending():
    mgr, _store = _mgr(max_pending=50)
    codes = set()
    for i in range(15):
        _rid, code = mgr.create(f"dev{i}", source_ip=f"10.0.0.{i}")
        assert code not in codes
        codes.add(code)


# --------------------------------------------------------------------------- #
# poll(): the companion-facing, unauthenticated-in-subnet surface.
# --------------------------------------------------------------------------- #
def test_poll_unknown_request_is_expired():
    mgr, _store = _mgr()
    assert mgr.poll("does-not-exist") == {"status": "expired"}


def test_poll_pending_status():
    mgr, _store = _mgr()
    request_id, _code = mgr.create("phone")
    assert mgr.poll(request_id) == {"status": "pending"}


def test_poll_never_returns_compare_code():
    # poll() is reachable unauthenticated in-subnet; the numeric-comparison
    # anchor must never leak there, for this request OR any other.
    mgr, _store = _mgr()
    request_id, _code = mgr.create("phone")
    result = mgr.poll(request_id)
    assert "compare_code" not in result


# --------------------------------------------------------------------------- #
# approve(): the ONLY mint site, gated on a correctly-echoed compare_code.
# --------------------------------------------------------------------------- #
def test_approve_without_confirm_code_mints_nothing():
    mgr, store = _mgr()
    request_id, _code = mgr.create("phone")
    assert mgr.approve(request_id, confirm_code=None) is None
    assert store.calls == []
    assert mgr.poll(request_id) == {"status": "pending"}   # untouched


def test_approve_with_empty_confirm_code_mints_nothing():
    mgr, store = _mgr()
    request_id, _code = mgr.create("phone")
    assert mgr.approve(request_id, confirm_code="") is None
    assert store.calls == []


def test_approve_with_wrong_confirm_code_mints_nothing():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone")
    wrong = "000000" if code != "000000" else "111111"
    assert mgr.approve(request_id, confirm_code=wrong) is None
    assert store.calls == []
    assert mgr.poll(request_id) == {"status": "pending"}   # still pending, retryable


def test_approve_unknown_request_is_none():
    mgr, store = _mgr()
    assert mgr.approve("nope", confirm_code="123456") is None
    assert store.calls == []


def test_approve_with_correct_confirm_code_mints_exactly_one_token():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone")
    device_id = mgr.approve(request_id, confirm_code=code)
    assert device_id is not None
    assert store.calls == [("phone", "user")]              # exactly one mint call
    result = mgr.poll(request_id)
    assert result == {"status": "approved", "device_id": device_id, "token": "tok-1"}


def test_approve_forwards_role_to_device_store():
    mgr, store = _mgr()
    request_id, code = mgr.create("laptop")
    mgr.approve(request_id, role="central", confirm_code=code)
    assert store.calls == [("laptop", "central")]


# --------------------------------------------------------------------------- #
# Single-use / no-double-mint: a second approve of a decided request refuses.
# --------------------------------------------------------------------------- #
def test_second_approve_of_same_request_is_refused_not_double_minted():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone")
    first = mgr.approve(request_id, confirm_code=code)
    assert first is not None
    second = mgr.approve(request_id, confirm_code=code)     # same correct code again
    assert second is None
    assert store.calls == [("phone", "user")]                # still exactly one mint


def test_approve_after_deny_mints_nothing():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone")
    assert mgr.deny(request_id) is True
    assert mgr.approve(request_id, confirm_code=code) is None
    assert store.calls == []


# --------------------------------------------------------------------------- #
# deny(): removes the pending record; idempotent-safe (no double effects).
# --------------------------------------------------------------------------- #
def test_deny_happy_path_removes_pending():
    mgr, store = _mgr()
    request_id, _code = mgr.create("phone")
    assert mgr.deny(request_id) is True
    assert mgr.poll(request_id) == {"status": "denied"}
    assert mgr.list_pending() == []
    assert store.calls == []


def test_deny_unknown_request_is_false():
    mgr, _store = _mgr()
    assert mgr.deny("nope") is False


def test_deny_already_approved_request_is_refused():
    mgr, store = _mgr()
    request_id, code = mgr.create("phone")
    mgr.approve(request_id, confirm_code=code)
    assert mgr.deny(request_id) is False                     # can't undo a mint via deny
    assert mgr.poll(request_id)["status"] == "approved"       # unaffected
    assert store.calls == [("phone", "user")]


def test_deny_twice_is_a_noop_second_time():
    mgr, _store = _mgr()
    request_id, _code = mgr.create("phone")
    assert mgr.deny(request_id) is True
    assert mgr.deny(request_id) is False


# --------------------------------------------------------------------------- #
# TTL: request_ttl (pending window) via injected clock.
# --------------------------------------------------------------------------- #
def test_request_ttl_expires_a_pending_request():
    clock = Clock()
    mgr, store = _mgr(clock=clock, request_ttl=180)
    request_id, _code = mgr.create("phone")
    clock.advance(181)
    assert mgr.poll(request_id) == {"status": "expired"}
    assert mgr.list_pending() == []
    assert store.calls == []


def test_request_valid_just_before_ttl_expiry():
    clock = Clock()
    mgr, store = _mgr(clock=clock, request_ttl=180)
    request_id, _code = mgr.create("phone")
    clock.advance(179)
    assert mgr.poll(request_id) == {"status": "pending"}


def test_expired_pending_request_cannot_be_approved():
    clock = Clock()
    mgr, store = _mgr(clock=clock, request_ttl=180)
    request_id, code = mgr.create("phone")
    clock.advance(181)
    assert mgr.approve(request_id, confirm_code=code) is None
    assert store.calls == []


# --------------------------------------------------------------------------- #
# TTL: approval_ttl (token pickup window) -- fresh window from Approve, never
# shorter than whatever was left on the original request TTL.
# --------------------------------------------------------------------------- #
def test_approval_ttl_gives_a_fresh_pickup_window_past_the_original_ttl():
    clock = Clock()
    mgr, store = _mgr(clock=clock, request_ttl=180, approval_ttl=120)
    request_id, code = mgr.create("phone")
    clock.advance(170)                              # close to the 180s request TTL
    device_id = mgr.approve(request_id, confirm_code=code)
    assert device_id is not None
    clock.advance(15)                                # now t=185s: past original TTL(180)...
    result = mgr.poll(request_id)                    # ...but inside the fresh 120s pickup window
    assert result == {"status": "approved", "device_id": device_id, "token": "tok-1"}


def test_approval_pickup_window_eventually_expires():
    clock = Clock()
    mgr, store = _mgr(clock=clock, request_ttl=180, approval_ttl=120)
    request_id, code = mgr.create("phone")
    clock.advance(179)                               # approve right before the request TTL,
    mgr.approve(request_id, confirm_code=code)        # so the fresh 120s window is the binding one
    clock.advance(121)                                # past 120s from the approval moment (t=179+120=299)
    assert mgr.poll(request_id) == {"status": "expired"}


# --------------------------------------------------------------------------- #
# Flood bounds: eviction of the OLDEST record, never a hard refuse.
# --------------------------------------------------------------------------- #
def test_max_pending_global_evicts_oldest_not_refuse():
    clock = Clock()
    mgr, store = _mgr(clock=clock, max_pending=3, max_pending_per_ip=10)
    ids = []
    for i in range(5):
        rid, _code = mgr.create(f"dev{i}", source_ip=f"10.0.0.{i}")
        ids.append(rid)
        clock.advance(1)
    assert len(mgr.list_pending()) == 3               # capped, never grew past 3
    assert mgr.poll(ids[0]) == {"status": "expired"}   # oldest two evicted...
    assert mgr.poll(ids[1]) == {"status": "expired"}
    assert mgr.poll(ids[2]) == {"status": "pending"}   # ...newest three survive
    assert mgr.poll(ids[3]) == {"status": "pending"}
    assert mgr.poll(ids[4]) == {"status": "pending"}


def test_max_pending_per_ip_evicts_only_the_flooding_ip():
    clock = Clock()
    mgr, store = _mgr(clock=clock, max_pending=100, max_pending_per_ip=2)
    a_ids = []
    for i in range(4):
        rid, _code = mgr.create(f"a{i}", source_ip="1.1.1.1")
        a_ids.append(rid)
        clock.advance(1)
    b_id, _code = mgr.create("b0", source_ip="2.2.2.2")
    assert mgr.poll(a_ids[0]) == {"status": "expired"}  # IP A's own oldest two evicted
    assert mgr.poll(a_ids[1]) == {"status": "expired"}
    assert mgr.poll(a_ids[2]) == {"status": "pending"}
    assert mgr.poll(a_ids[3]) == {"status": "pending"}
    assert mgr.poll(b_id) == {"status": "pending"}      # a DIFFERENT IP is never touched


# --------------------------------------------------------------------------- #
# Per-request compare_code: never crosses over to a different request/requester.
# --------------------------------------------------------------------------- #
def test_compare_code_never_unlocks_a_different_request():
    mgr, store = _mgr()
    id_a, code_a = mgr.create("phoneA", source_ip="1.1.1.1")
    id_b, code_b = mgr.create("phoneB", source_ip="2.2.2.2")
    assert code_a != code_b
    # A's compare_code must never approve B's request, even though both are
    # simultaneously pending on the same Core.
    assert mgr.approve(id_b, confirm_code=code_a) is None
    assert store.calls == []
    assert mgr.poll(id_b) == {"status": "pending"}
    # B's own code still works.
    device_id = mgr.approve(id_b, confirm_code=code_b)
    assert device_id is not None
    assert store.calls == [("phoneB", "user")]
    # A is untouched throughout.
    assert mgr.poll(id_a) == {"status": "pending"}


def test_list_pending_maps_compare_code_to_the_correct_request_id():
    mgr, _store = _mgr()
    id_a, code_a = mgr.create("phoneA", source_ip="1.1.1.1")
    id_b, code_b = mgr.create("phoneB", source_ip="2.2.2.2")
    by_id = {p["request_id"]: p["compare_code"] for p in mgr.list_pending()}
    assert by_id[id_a] == code_a
    assert by_id[id_b] == code_b


def test_list_pending_excludes_decided_requests():
    mgr, _store = _mgr()
    approved_id, code = mgr.create("phone")
    denied_id, _code2 = mgr.create("laptop", source_ip="9.9.9.9")
    mgr.approve(approved_id, confirm_code=code)
    mgr.deny(denied_id)
    assert mgr.list_pending() == []
