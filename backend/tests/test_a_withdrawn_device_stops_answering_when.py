"""Withdrawing consent has to stop the running commentary, not just the naming.

`last_seen_ts` is a live oracle. It says "this device contacted the Core just
now", `GET /api/devices` returns it verbatim, and `verify()` advanced it on every
authenticated request regardless of what the device's owner had asked for.

On a RED device — consent withdrawn — that timestamp answers "is that person
around?" for somebody who explicitly asked not to be observed, and it answers it
again every time their phone reassociates with the Wi-Fi. On YELLOW it
re-identifies a device the interface deliberately leaves unnamed: the row has no
name, but it has a clock.

The route is central/root-only, so this is an admin-visible oracle rather than a
peer-visible one. That bounds the severity. It does not change what the consent
tier is for.

## The part that is easy to get wrong

`consent` is NULLABLE, and NULL is not withdrawal. Every row that predates the
column reads back `None`, and this codebase resolves that to green everywhere
else it consumes consent — `Device.to_dict` returns `self.consent or "green"`,
and so does the app's `_consent_of`. A verbatim "only advance when consent ==
'green'" would freeze `last_seen` for every legacy device and quietly break the
ordinary device list, while looking like a privacy improvement.

So the predicate is `(consent or "green") == "green"`, and the legacy case has a
test of its own below rather than being left to inference.

## What does NOT change

The device keeps working. Its token verifies, its requests succeed, its role and
scopes are untouched. Withdrawal is not revocation — there is a separate,
destructive action for that. What stops is the observation.
"""
from __future__ import annotations

import pytest

from wavr.devices import DeviceStore


@pytest.fixture()
def store(tmp_path):
    return DeviceStore(str(tmp_path / "devices.db"))


def _seen(store: DeviceStore, device_id: str):
    return next(d.last_seen_ts for d in store.list()
                if d.device_id == device_id)


def test_a_green_device_is_still_watched(store):
    """The positive control. Everything below is worthless without it: a store
    that never advanced `last_seen` would pass every refusal test here."""
    did, token = store.add("phone", "user", consent="green")
    assert store.verify(token) is not None
    first = _seen(store, did)
    assert first is not None, "a green device's last-seen must advance"

    store._now = lambda: "2099-01-01T00:00:00+00:00"
    assert store.verify(token) is not None
    assert _seen(store, did) == "2099-01-01T00:00:00+00:00"


def test_a_legacy_row_with_no_consent_is_treated_as_green(store):
    """NULL means "never asked", not "said no".

    This is the port trap. Reading NULL as non-green freezes `last_seen` for
    every device that existed before the consent column, which looks like a
    privacy win and is actually a broken device list.
    """
    did, token = store.add("older phone", "user")          # consent left unset
    assert next(d.consent for d in store.list()
                if d.device_id == did) is None, "fixture no longer legacy-shaped"

    assert store.verify(token) is not None
    assert _seen(store, did) is not None, (
        "a legacy NULL-consent row was treated as withdrawn; NULL resolves to "
        "green everywhere else this codebase reads consent")


@pytest.mark.parametrize("level", ["red", "yellow"])
def test_a_non_green_device_stops_advancing_its_clock(store, level):
    did, token = store.add("phone", "user", consent="green")
    assert store.verify(token) is not None
    before = _seen(store, did)
    assert before is not None

    assert store.set_consent(did, level) is True
    store._now = lambda: "2099-01-01T00:00:00+00:00"

    assert store.verify(token) is not None, (
        f"consent {level} must not break the device -- withdrawal is not "
        f"revocation, and there is a separate destructive action for that")
    assert _seen(store, did) == before, (
        f"a {level} device kept publishing when its owner was contactable")


def test_the_returned_device_does_not_leak_the_fresh_timestamp(store):
    """The row is frozen; the object handed back must be too.

    Freezing only the database while `verify()` returns a live `last_seen_ts`
    would move the oracle from the device list into every caller of `verify()` --
    the same leak, one layer along, and harder to see.
    """
    did, token = store.add("phone", "user", consent="green")
    store.verify(token)
    before = _seen(store, did)

    store.set_consent(did, "red")
    store._now = lambda: "2099-01-01T00:00:00+00:00"
    dev = store.verify(token)

    assert dev is not None
    assert dev.last_seen_ts == before
    assert dev.to_dict()["last_seen_ts"] == before


def test_consent_restored_resumes_the_clock(store):
    """Reversible. A tier is a setting, not a one-way door."""
    did, token = store.add("phone", "user", consent="green")
    store.verify(token)
    store.set_consent(did, "red")
    store._now = lambda: "2099-01-01T00:00:00+00:00"
    store.verify(token)
    frozen = _seen(store, did)

    store.set_consent(did, "green")
    store._now = lambda: "2099-06-06T00:00:00+00:00"
    store.verify(token)
    assert _seen(store, did) == "2099-06-06T00:00:00+00:00" != frozen


def test_a_revoked_device_is_refused_regardless_of_consent(store):
    """Ordering check: the existing refusal still comes first."""
    did, token = store.add("phone", "user", consent="green")
    store.revoke(did)
    assert store.verify(token) is None
