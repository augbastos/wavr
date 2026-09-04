"""One list of things waiting for a person, and the rules that keep it honest.

The failure mode this file guards is not a wrong item. It is a list that becomes
a feed: once people stop reading it, the one time it matters they will not look.
So most of these tests are about what must NOT appear.
"""
from wavr.attention import BLOCKING, DEGRADED, INFO, collect, summarise


def cov(sensor_id="kitchen-radar", health="offline", room="kitchen",
        precision="count", modality="mmwave", last_seen="2026-09-04T10:00:00+00:00"):
    return {"sensor_id": sensor_id, "health": health, "room": room,
            "precision_level": precision, "modality": modality,
            "last_seen": last_seen}


# -- What belongs -------------------------------------------------------------

def test_a_waiting_pairing_request_blocks_and_says_so():
    """Built from what `PairApprovalManager.list_pending()` REALLY emits.

    The first version of this module read `device_name` and `created_ts`;
    the producer emits `requester_name` and `created_at`. Every request would
    have rendered as "A device wants to join" with no timestamp — wrong text
    AND wrong order — and every test would have passed, because the test
    invented the same shape the code did.
    """
    from wavr.pair_requests import PairApprovalManager

    class _Store:
        def create_device(self, *a, **k):
            return None

    mgr = PairApprovalManager(_Store())
    mgr.create("Ana's phone")
    real = mgr.list_pending()
    assert real and "requester_name" in real[0], real
    items = collect(pending_pairings=real)
    assert len(items) == 1
    assert items[0].band == BLOCKING
    assert "Ana's phone" in items[0].title, items[0].title
    assert items[0].since, "no timestamp, so it cannot be ranked by how long it waited"
    assert items[0].action


def test_an_offline_sensor_says_what_the_household_LOSES_not_just_its_status():
    """"Offline" is a status. "You can no longer count people in the kitchen" is
    the reason to get up and fix it — and it is the sentence Wavr is uniquely
    able to write, because it knows what that sensor was contributing."""
    items = collect(coverage_rows=[cov(precision="position")])
    assert items[0].band == DEGRADED
    assert "place people within kitchen" in items[0].detail

    counted = collect(coverage_rows=[cov(precision="count")])
    assert "count people in kitchen" in counted[0].detail


def test_a_camera_missing_its_address_explains_why_it_is_missing():
    """The one thing a restored backup cannot bring back. A person who does not
    know that reads it as Wavr having lost their camera."""
    items = collect(cameras_needing_url=[{"name": "hall-cam"}])
    assert "hall-cam" in items[0].title
    assert "password" in items[0].detail


# -- What must NOT belong -----------------------------------------------------

def test_a_healthy_sensor_is_not_an_item():
    assert collect(coverage_rows=[cov(health="ok")]) == []


def test_a_switched_off_sensor_is_not_a_problem_to_solve():
    """An operator turning a camera off is a decision, not a fault. Listing it
    teaches people that this list contains things they already know."""
    assert collect(coverage_rows=[cov(health="disabled")]) == []


def test_a_decided_discovery_leaves_the_list():
    assert collect(discoveries=[
        {"discovery_id": "d1", "title": "New camera", "status": "accepted"}]) == []
    assert collect(discoveries=[
        {"discovery_id": "d2", "title": "New camera", "status": "dismissed"}]) == []


def test_a_noisy_alert_appears_once_with_its_count_and_not_eighty_times():
    """An alert firing every thirty seconds is ONE thing wrong. Rendering each
    occurrence turns the list into a log, which is the failure this module is
    written to avoid."""
    items = collect(alerts=[
        {"kind": "rogue_dhcp", "severity": "high", "title": "Rogue DHCP server",
         "ts": f"2026-09-04T10:{m:02d}:00+00:00"} for m in range(40)])
    assert len(items) == 1
    assert items[0].count == 40
    assert items[0].since.endswith("10:00:00+00:00"), "kept the OLDEST, not the newest"


def test_low_severity_alerts_do_not_reach_the_action_list():
    assert collect(alerts=[{"kind": "info", "severity": "low",
                            "title": "Something happened"}]) == []


# -- Ranking ------------------------------------------------------------------

def test_blocking_beats_degraded_beats_information():
    items = collect(
        discoveries=[{"discovery_id": "d", "title": "A new device",
                      "status": "pending"}],
        coverage_rows=[cov()],
        pending_pairings=[{"request_id": "r", "requester_name": "Phone"}])
    assert [i.band for i in items] == [BLOCKING, DEGRADED, INFO]


def test_the_oldest_waits_first_within_a_band():
    """Sorting by newest — the default everywhere — buries the request that has
    been waiting three days under the one from a minute ago."""
    items = collect(pending_pairings=[
        {"request_id": "new", "requester_name": "New",
         "created_at": "2026-09-04T10:00:00+00:00"},
        {"request_id": "old", "requester_name": "Old",
         "created_at": "2026-09-01T10:00:00+00:00"}])
    assert [i.title for i in items] == ["Old wants to join", "New wants to join"]


def test_an_item_with_no_timestamp_does_not_jump_to_the_front():
    """A missing `since` is not "from 1970". Treating it as oldest would invent
    an urgency nothing measured."""
    items = collect(pending_pairings=[
        {"request_id": "dated", "requester_name": "Dated",
         "created_at": "2026-09-01T10:00:00+00:00"},
        {"request_id": "undated", "requester_name": "Undated"}])
    assert items[0].title.startswith("Dated")


# -- The summary every surface renders ----------------------------------------

def test_nothing_waiting_is_a_sentence_produced_by_the_same_code():
    """"Nothing needs you" and "three things need you" must come from one
    place, or an emptiness check somewhere drifts and a surface goes quiet while
    three things wait."""
    assert summarise([])["headline"] == "Nothing needs your attention"
    assert summarise([])["total"] == 0


def test_the_summary_counts_by_band_so_a_badge_can_be_honest():
    body = summarise(collect(
        pending_pairings=[{"request_id": "r", "requester_name": "Phone"}],
        coverage_rows=[cov()]))
    assert body["total"] == 2
    assert body["blocking"] == 1 and body["degraded"] == 1
    assert body["headline"] == "2 things need your attention"


def test_one_thing_is_singular():
    body = summarise(collect(coverage_rows=[cov()]))
    assert body["headline"] == "1 thing needs your attention"


def test_a_caller_that_passes_nothing_gets_nothing_rather_than_reassurance():
    """A source that failed upstream should leave its argument out — and the
    caller must then say it could not check, rather than letting this print
    "nothing needs you" over three waiting requests."""
    assert summarise(collect())["total"] == 0
