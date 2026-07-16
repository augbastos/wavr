"""Guest mode (feature #8) DATA LAYER: the guest role's minimal scopes, the
expires_at time-based credential death in DeviceStore.verify, and the pairing
mint/redeem that stamps a guest invite's deadline. The app.py wiring (the
/api/guest/invite endpoint, the register-companion force-anonymous, and the
_consent_of revoked/expired fix) is covered separately once wired -- these tests
prove the reusable pieces in isolation, no FastAPI app needed.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr import auth
from wavr.devices import DeviceStore, VALID_ROLES, _is_expired
from wavr.pairing import PairingManager


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# _is_expired -- the verify() expiry gate
# --------------------------------------------------------------------------- #
def test_is_expired_none_never_expires():
    assert _is_expired(None) is False, "no deadline -> never expires (every non-guest device)"


def test_is_expired_past_true_future_false():
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    assert _is_expired(_iso(now - timedelta(minutes=1)), now=now) is True
    assert _is_expired(_iso(now + timedelta(minutes=1)), now=now) is False


def test_is_expired_malformed_fails_closed():
    # A junk timestamp must NEVER read as a valid unbounded credential.
    assert _is_expired("not-a-timestamp") is True
    assert _is_expired("") is True


def test_is_expired_naive_is_coerced_to_utc():
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    # a tz-naive stored value can't dodge the check by lacking an offset
    assert _is_expired("2026-07-16T11:59:00", now=now) is True
    assert _is_expired("2026-07-16T12:01:00", now=now) is False


# --------------------------------------------------------------------------- #
# DeviceStore -- expires_at is a time-based revoke; NULL = never expires
# --------------------------------------------------------------------------- #
def test_guest_role_is_valid_and_min_scoped():
    assert "guest" in VALID_ROLES
    s = DeviceStore(":memory:")
    did, token = s.add("visitor", "guest",
                       expires_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1)))
    dev = s.verify(token)
    assert dev is not None and dev.role == "guest"
    assert dev.expires_at is not None
    s.close()


def test_expired_guest_token_stops_verifying():
    s = DeviceStore(":memory:")
    _, token = s.add("visitor", "guest",
                     expires_at=_iso(datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert s.verify(token) is None, "past the deadline -> token dead, like revoked"
    s.close()


def test_non_guest_device_never_expires_and_is_additive():
    s = DeviceStore(":memory:")
    did, token = s.add("laptop", "user")            # no expires_at -> NULL
    dev = s.verify(token)
    assert dev is not None and dev.expires_at is None, "existing pairings unchanged (never expire)"
    assert s.get(did).to_dict()["expires_at"] is None, "expires_at surfaces (None) for the device list"
    s.close()


# --------------------------------------------------------------------------- #
# auth -- guest is strictly-less-than-user and fails closed on the role tiers
# --------------------------------------------------------------------------- #
def test_guest_default_scope_is_presence_write_only():
    assert auth.DEFAULT_SCOPES["guest"] == frozenset({"presence:write"})
    assert auth.effective_scopes("guest", None) == frozenset({"presence:write"})
    # the house-read/manage scopes a guest must NEVER hold by default
    for denied in ("presence:read", "network:read", "camera:view", "control", "admin", "mcp"):
        assert denied not in auth.DEFAULT_SCOPES["guest"], denied


def test_guest_can_view_but_never_change_state():
    # Guest IS in can_view so it can pass require_authenticated for the presence:write
    # register-companion route (its only purpose); every house-read route's own
    # require_scope still denies it. It is NEVER in can_change_state, so the central-only
    # state-changing tier (require_local fallback) denies it by construction.
    assert auth.can_view("guest") is True
    assert auth.can_change_state("guest") is False


# --------------------------------------------------------------------------- #
# PairingManager -- a guest code stamps its session deadline onto the device
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def test_guest_invite_redeem_stamps_the_deadline():
    clock = _Clock()
    store = DeviceStore(":memory:")
    mgr = PairingManager(store, now_fn=clock)
    code = mgr.mint_guest_code(hours=4)
    did, token = mgr.redeem(code, "guest phone")
    dev = store.get(did)
    assert dev.role == "guest"
    assert dev.expires_at == _iso(clock.t + timedelta(hours=4)), \
        "the device carries the host-chosen deadline, 4h from mint"
    store.close()


def test_normal_pairing_leaves_no_deadline():
    clock = _Clock()
    store = DeviceStore(":memory:")
    mgr = PairingManager(store, now_fn=clock)
    did, token = mgr.redeem(mgr.mint_code("user"), "laptop")
    assert store.get(did).expires_at is None, "a normal pairing never gets an expiry"
    store.close()


def test_guest_invite_hours_must_be_positive():
    mgr = PairingManager(DeviceStore(":memory:"))
    with pytest.raises(ValueError):
        mgr.mint_guest_code(hours=0)
    with pytest.raises(ValueError):
        mgr.mint_guest_code(hours=-3)
