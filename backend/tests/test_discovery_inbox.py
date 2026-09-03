"""Discovery Inbox: dedupe, the anti-nag rule, and honest phrasing."""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.discovery_inbox import (
    KIND_CAMERA_FOUND, KIND_DEVICE_NEW, KIND_NODE_PENDING, MAX_PENDING,
    STATUS_ACCEPTED, STATUS_DISMISSED, STATUS_PENDING, DiscoveryError,
    DiscoveryInbox, describe_device,
)


@pytest.fixture
def inbox():
    i = DiscoveryInbox(":memory:")
    yield i
    i.close()


# -- Dedupe ------------------------------------------------------------------

def test_the_same_subject_is_one_item_however_often_it_is_seen(inbox):
    for _ in range(50):
        inbox.observe(KIND_CAMERA_FOUND, "aa:bb:cc:dd:ee:ff", "Tapo C210",
                      detail={"ip": "192.168.1.50"}, confidence=0.8)
    assert len(inbox.list_items()) == 1
    assert inbox.counts()[STATUS_PENDING] == 1


def test_last_seen_advances_while_first_seen_does_not(inbox):
    stamps = iter(["2026-09-03T10:00:00+00:00", "2026-09-03T11:00:00+00:00"])
    inbox._now = lambda: next(stamps)                     # noqa: SLF001
    inbox.observe(KIND_DEVICE_NEW, "mac1", "A phone")
    again = inbox.observe(KIND_DEVICE_NEW, "mac1", "A phone")
    assert again.first_seen == "2026-09-03T10:00:00+00:00"
    assert again.last_seen == "2026-09-03T11:00:00+00:00"


def test_different_kinds_of_the_same_subject_are_separate_items(inbox):
    inbox.observe(KIND_DEVICE_NEW, "mac1", "A device")
    inbox.observe(KIND_CAMERA_FOUND, "mac1", "Maybe a camera")
    assert len(inbox.list_items()) == 2


# -- The anti-nag rule -------------------------------------------------------

def test_a_dismissed_item_does_not_come_back_on_re_sighting(inbox):
    d = inbox.observe(KIND_CAMERA_FOUND, "mac1", "Tapo C210", detail={"ip": "10.0.0.5"})
    inbox.decide(d.discovery_id, STATUS_DISMISSED)
    inbox.observe(KIND_CAMERA_FOUND, "mac1", "Tapo C210", detail={"ip": "10.0.0.5"})
    assert inbox.get(d.discovery_id).status == STATUS_DISMISSED
    assert inbox.list_items(status=STATUS_PENDING) == []


def test_a_dismissed_item_DOES_come_back_when_the_thing_actually_changed(inbox):
    d = inbox.observe(KIND_CAMERA_FOUND, "mac1", "Tapo C210", detail={"ip": "10.0.0.5"})
    inbox.decide(d.discovery_id, STATUS_DISMISSED)
    inbox.observe(KIND_CAMERA_FOUND, "mac1", "Tapo C210", detail={"ip": "10.0.0.99"})
    reopened = inbox.get(d.discovery_id)
    assert reopened.status == STATUS_PENDING and reopened.decided_ts is None


def test_volatile_fields_do_not_count_as_a_change(inbox):
    # RSSI and counters move on every sweep. If they reopened items, every
    # dismissal would last exactly one scan cycle.
    d = inbox.observe(KIND_DEVICE_NEW, "aa:bb", "Unknown beacon",
                      detail={"vendor": "Apple", "rssi": -70, "count": 3})
    inbox.decide(d.discovery_id, STATUS_DISMISSED)
    inbox.observe(KIND_DEVICE_NEW, "aa:bb", "Unknown beacon",
                  detail={"vendor": "Apple", "rssi": -41, "count": 900})
    assert inbox.get(d.discovery_id).status == STATUS_DISMISSED


def test_an_accepted_item_also_reopens_on_a_real_change(inbox):
    d = inbox.observe(KIND_DEVICE_NEW, "mac1", "Laptop", detail={"vendor": "Acer"})
    inbox.decide(d.discovery_id, STATUS_ACCEPTED)
    inbox.observe(KIND_DEVICE_NEW, "mac1", "Laptop", detail={"vendor": "Dell"})
    assert inbox.get(d.discovery_id).status == STATUS_PENDING


# -- Bounds ------------------------------------------------------------------

def test_unknown_kind_is_rejected(inbox):
    with pytest.raises(DiscoveryError, match="unknown discovery kind"):
        inbox.observe("aliens_landed", "x", "Hello")


def test_blank_subject_and_title_are_rejected(inbox):
    with pytest.raises(DiscoveryError):
        inbox.observe(KIND_DEVICE_NEW, "  ", "A device")
    with pytest.raises(DiscoveryError):
        inbox.observe(KIND_DEVICE_NEW, "mac1", "   ")


def test_oversized_detail_is_rejected(inbox):
    with pytest.raises(DiscoveryError, match="too large"):
        inbox.observe(KIND_DEVICE_NEW, "mac1", "A device", detail={"x": "y" * 9000})


def test_confidence_is_clamped(inbox):
    assert inbox.observe(KIND_DEVICE_NEW, "a", "t", confidence=9.0).confidence == 1.0
    assert inbox.observe(KIND_DEVICE_NEW, "b", "t", confidence=-3.0).confidence == 0.0


def test_title_is_length_bounded(inbox):
    assert len(inbox.observe(KIND_DEVICE_NEW, "a", "x" * 900).title) <= 160


def test_pending_items_are_capped_and_evict_the_oldest(inbox):
    stamp = [0]

    def _clock():
        stamp[0] += 1
        return f"2026-09-03T{stamp[0] // 3600 % 24:02d}:{stamp[0] // 60 % 60:02d}:{stamp[0] % 60:02d}+00:00"

    inbox._now = _clock                                   # noqa: SLF001
    for i in range(MAX_PENDING + 25):
        inbox.observe(KIND_DEVICE_NEW, f"mac{i:05d}", f"Device {i}")
    assert inbox.counts()[STATUS_PENDING] <= MAX_PENDING
    # The most recent sighting survived; a noisy LAN must not push out the news.
    assert any(d.subject == f"mac{MAX_PENDING + 24:05d}" for d in inbox.list_items())


def test_decided_items_are_not_evicted_by_the_cap(inbox):
    # They are the memory that stops an item nagging; losing them un-dismisses it.
    keep = inbox.observe(KIND_DEVICE_NEW, "keep-me", "Dismissed once")
    inbox.decide(keep.discovery_id, STATUS_DISMISSED)
    for i in range(MAX_PENDING + 10):
        inbox.observe(KIND_DEVICE_NEW, f"noise{i:05d}", "Noise")
    assert inbox.get(keep.discovery_id).status == STATUS_DISMISSED


# -- Listing and decisions ---------------------------------------------------

def test_list_filters_by_status_and_kind(inbox):
    a = inbox.observe(KIND_DEVICE_NEW, "1", "A")
    inbox.observe(KIND_CAMERA_FOUND, "2", "B")
    inbox.decide(a.discovery_id, STATUS_ACCEPTED)
    assert len(inbox.list_items(status=STATUS_PENDING)) == 1
    assert len(inbox.list_items(status=STATUS_ACCEPTED)) == 1
    assert len(inbox.list_items(status=None)) == 2
    assert len(inbox.list_items(status=None, kind=KIND_CAMERA_FOUND)) == 1


def test_decide_rejects_a_bogus_status(inbox):
    d = inbox.observe(KIND_DEVICE_NEW, "1", "A")
    with pytest.raises(DiscoveryError):
        inbox.decide(d.discovery_id, "maybe")


def test_decide_on_an_unknown_id_fails(inbox):
    with pytest.raises(DiscoveryError, match="unknown discovery"):
        inbox.decide("nope", STATUS_ACCEPTED)


def test_items_carry_their_actions(inbox):
    d = inbox.observe(KIND_NODE_PENDING, "node1", "A node wants to join").to_dict()
    ids = {a["id"] for a in d["actions"]}
    assert "approve_node" in ids and "dismiss" in ids


def test_confidence_survives_to_the_ui(inbox):
    d = inbox.observe(KIND_CAMERA_FOUND, "1", "Maybe a camera", confidence=0.72)
    assert d.to_dict()["confidence"] == 0.72


def test_prune_drops_only_stale_pending(inbox):
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    inbox._now = lambda: (now - timedelta(days=30)).isoformat()   # noqa: SLF001
    old = inbox.observe(KIND_DEVICE_NEW, "old", "Long gone")
    decided = inbox.observe(KIND_DEVICE_NEW, "decided", "Handled")
    inbox.decide(decided.discovery_id, STATUS_DISMISSED)
    inbox._now = lambda: now.isoformat()                          # noqa: SLF001
    inbox.observe(KIND_DEVICE_NEW, "fresh", "Here now")

    assert inbox.prune(days=14, now=now) == 1
    assert inbox.get(old.discovery_id) is None
    assert inbox.get(decided.discovery_id) is not None
    assert len(inbox.list_items()) == 1


# -- Honest phrasing (SS16) ---------------------------------------------------

def test_high_confidence_states_low_confidence_hedges():
    assert describe_device("Tapo C210", "TP-Link", "a camera", 0.9).endswith(
        "looks like a camera.")
    assert "possibly a camera" in describe_device("Tapo C210", "TP-Link", "a camera", 0.6)


def test_below_half_confidence_wavr_says_it_does_not_know():
    text = describe_device("Unknown", "Espressif", "a camera", 0.3)
    assert "camera" not in text
    assert "can't tell" in text


def test_no_information_at_all_is_stated_plainly():
    assert describe_device("", "", "", 0.0) == "A device — we can't tell what this is yet."
