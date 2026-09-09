"""A person must always be able to tell WHICH Space they are looking at.

A Space is the first-class container this product is built on — a home, an
apartment, an office, a shop, a restaurant, a school, a warehouse, a workshop,
a hotel, a laboratory. Until now its name and kind were read in exactly
one place: the settings form where somebody types them. Everywhere else, the
shell showed a room map with no statement of whose place it is.

That is harmless on the machine standing in the hall and actively misleading on
a phone, which is the device most likely to be paired to more than one Core.
Acting on the wrong house is a real outcome: unlocking a door, dismissing an
intrusion alert, telling somebody nobody is home.

## Why /api/status and not /api/space

`/api/space` sits behind the admin gate, so an ordinary paired companion gets
403 from it — the surface that needs the answer most is the one that cannot
ask. `/api/status` carries `presence:read`, which every companion already
holds, and the shell fetches it anyway. So the identity rides a payload already
in flight, and the admin route keeps sole ownership of the parts that are
nobody else's business: the people list and the Core topology.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.space_store import SpaceStore
from wavr.storage import Storage


def _app(space_store=None):
    return create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                      fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                      health_resolvers={}, space_store=space_store)


def _status(c):
    r = c.get("/api/status")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _no_local_token(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)


def test_status_carries_a_space_key_even_before_setup():
    """Absent is not the same as unnamed. A consumer that has to tell "no Space
    yet" from "this build predates the field" cannot, if the key simply is not
    there — so the key is always present, and null means the honest thing."""
    with TestClient(_app(SpaceStore(":memory:"))) as c:
        body = _status(c)
    assert "space" in body, "a consumer cannot tell 'no Space' from 'old build'"
    assert body["space"] is None


def test_a_named_space_reaches_the_status_payload():
    store = SpaceStore(":memory:")
    store.create_space("Alex's flat", kind="apartment")
    with TestClient(_app(store)) as c:
        space = _status(c)["space"]
    assert space is not None, "the Space exists but the shell cannot see it"
    assert space["name"] == "Alex's flat"
    # "flat" is the English word; "apartment" is the stored kind. SPACE_KINDS is
    # the vocabulary, and anything outside it is coerced to "other" rather than
    # rejected -- a second Core joining from a newer build must not fail on a
    # kind this one has never heard of.
    assert space["kind"] == "apartment"


def test_a_space_that_is_not_a_home_says_so():
    """The product supports shops and offices. If the payload flattened every
    kind to "home", nothing downstream could ever stop calling it one."""
    store = SpaceStore(":memory:")
    store.create_space("The corner shop", kind="shop")
    with TestClient(_app(store)) as c:
        assert _status(c)["space"]["kind"] == "shop"


def test_the_identity_projection_carries_nothing_else():
    """The admin route owns the people list and the topology. If either ever
    leaks into this projection, every paired phone in the house can read it."""
    store = SpaceStore(":memory:")
    store.create_space("The shop", kind="shop")
    with TestClient(_app(store)) as c:
        space = _status(c)["space"]
    assert set(space) == {"name", "kind"}, (
        f"the Space projection grew: {sorted(space)}. Anything beyond name and "
        f"kind belongs on /api/space, behind the admin gate.")


def test_a_broken_store_costs_the_name_not_the_dashboard():
    """Status is the payload the whole shell hangs off. A Space store that
    cannot be read must cost the Space name, not the entire screen."""
    class Broken(SpaceStore):
        def __init__(self):
            super().__init__(":memory:")

        def get_space(self):
            raise sqlite3.Error("disk I/O error")

    with TestClient(_app(Broken())) as c:
        body = _status(c)
    assert body["space"] is None
    assert "house" in body, "the rest of status must survive a broken store"
