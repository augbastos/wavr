"""A question answered in one place is answered everywhere.

## The contradiction

Two top-level tabs ask a household the same thing.

  * **New devices** filters the inventory to `known == False`.
  * **Discoveries** shows the inbox, where `discovery_feed` raises a
    `device_new` item for the same devices.

Two producers, one question. Pressing "That's mine" flips `known`, and the feed
then stops re-observing that MAC — but a row already pending merely stops being
*refreshed*. It kept sitting in the inbox, behind a count badge, asking about a
device the person had already claimed, until the fourteen-day prune finally
removed it.

So the person answers, the badge stays lit, they open the inbox to find the
same question, and they learn that this screen is not to be believed. That is
worse than the badge never existing.

## Why this tests the round trip, not the method

The failure mode is not the SQL. It is the MAC not matching: the inbox keys on
whatever `netinventory` put in `Device.mac`, the route keys on what
`normalize_mac` returns, and a resolution that quietly matches nothing looks
exactly like a resolution that worked. Both happen to produce lowercase
colon-form today. This file goes through the HTTP route so that if they ever
stop agreeing, something fails.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.discovery_feed import feed_devices
from wavr.discovery_inbox import (KIND_CAMERA_FOUND, KIND_DEVICE_NEW,
                                  STATUS_DISMISSED, STATUS_PENDING,
                                  DiscoveryInbox)
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage

MAC = "aa:bb:cc:dd:ee:ff"


def _dev(mac=MAC, **kw):
    base = {"mac": mac, "ip": "192.168.1.44", "vendor": "Acme",
            "hostname": "thing", "device_type": "", "type_confidence": 0.0,
            "known": False}
    base.update(kw)
    return base


@pytest.fixture()
def inbox():
    return DiscoveryInbox(":memory:")


@pytest.fixture()
def client(inbox, monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, discovery_inbox=inbox)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        yield c


# -- the store's own contract --------------------------------------------------

def test_resolving_moves_a_pending_item_out_of_the_way(inbox):
    feed_devices(inbox, [_dev()])
    assert inbox.counts()["pending"] == 1

    assert inbox.resolve(KIND_DEVICE_NEW, MAC) is True
    assert inbox.counts()["pending"] == 0


def test_resolving_something_that_is_not_there_says_so(inbox):
    assert inbox.resolve(KIND_DEVICE_NEW, MAC) is False


def test_resolving_never_overwrites_a_decision_a_person_made(inbox):
    """"Ignore" is an answer. Turning it into "accepted" behind their back
    would rewrite what they said."""
    feed_devices(inbox, [_dev()])
    item = inbox.list_items()[0]
    inbox.decide(item.discovery_id, STATUS_DISMISSED)

    assert inbox.resolve(KIND_DEVICE_NEW, MAC) is False
    assert inbox.get(item.discovery_id).status == STATUS_DISMISSED


def test_resolving_one_kind_leaves_another_kind_for_the_same_thing_alone(inbox):
    """A camera raises `camera_found`, not `device_new`. Claiming the device as
    yours does not mean you have set the camera up, and the inbox should still
    offer to."""
    inbox.observe(KIND_CAMERA_FOUND, MAC, "A camera at 192.168.1.44")
    inbox.observe(KIND_DEVICE_NEW, MAC, "Something new")

    inbox.resolve(KIND_DEVICE_NEW, MAC)
    kinds = {i.kind for i in inbox.list_items(status=STATUS_PENDING)}
    assert kinds == {KIND_CAMERA_FOUND}


# -- the round trip, which is where the MACs have to agree ---------------------

def test_claiming_a_device_stops_the_inbox_asking_about_it(client, inbox):
    feed_devices(inbox, [_dev()])
    assert inbox.counts()["pending"] == 1

    r = client.post("/api/inventory/known", json={"mac": MAC, "known": True})
    assert r.status_code == 200, r.text

    assert inbox.counts()["pending"] == 0, (
        "the person said the device is theirs and the inbox is still asking. "
        "If the counts look right in isolation, check the MAC format on both "
        "sides — a resolution that matches nothing looks like success.")


def test_a_mac_typed_in_a_different_shape_still_resolves(client, inbox):
    """The route normalises; the inbox does not. Somebody posting the uppercase
    hyphen form — which is what Windows prints — must not silently leave the
    inbox asking."""
    feed_devices(inbox, [_dev()])
    r = client.post("/api/inventory/known",
                    json={"mac": "AA-BB-CC-DD-EE-FF", "known": True})
    assert r.status_code == 200, r.text
    assert inbox.counts()["pending"] == 0


def test_unclaiming_a_device_does_not_silence_the_inbox(client, inbox):
    """Un-marking is the person saying they do NOT recognise it. That is a
    reason for the inbox to ask, not to stop."""
    feed_devices(inbox, [_dev()])
    r = client.post("/api/inventory/known", json={"mac": MAC, "known": False})
    assert r.status_code == 200, r.text
    assert inbox.counts()["pending"] == 1


def test_a_broken_inbox_does_not_fail_the_answer(client, inbox, monkeypatch):
    """Marking the device known is the operation. Tidying the inbox is a
    consequence, and a consequence must not cost the operation."""
    def boom(*a, **k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(inbox, "resolve", boom)

    r = client.post("/api/inventory/known", json={"mac": MAC, "known": True})
    assert r.status_code == 200, r.text
