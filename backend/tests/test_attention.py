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
    mgr.create("Sam's phone")
    real = mgr.list_pending()
    assert real and "requester_name" in real[0], real
    items = collect(pending_pairings=real)
    assert len(items) == 1
    assert items[0].band == BLOCKING
    assert "Sam's phone" in items[0].title, items[0].title
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
        {"kind": "rogue_dhcp", "severity": "alert", "title": "Rogue DHCP server",
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


# -- Severity: the ladder, not a word somebody remembered ----------------------

def test_every_severity_a_producer_emits_is_classified_by_this_module():
    """The filter used to keep `("high", "critical")`. "high" is not a tier and
    no producer emits it, so the entire `alert` band was discarded — a rogue
    DHCP server, a gateway-identity change, every fall and every intrusion.

    The tray had the same concept right, which is how two surfaces came to
    disagree about the same event: one raised a notification while the other
    said "nothing needs your attention".

    This compares against the LADDER rather than a list retyped here, so the
    two cannot drift apart again.
    """
    from wavr.alert_severity import SEVERITY_LADDER
    from wavr.attention import ACTIONABLE_SEVERITIES

    assert ACTIONABLE_SEVERITIES <= set(SEVERITY_LADDER), (
        f"these are not tiers: {sorted(ACTIONABLE_SEVERITIES - set(SEVERITY_LADDER))}")
    # And it is the top of the ladder, not an arbitrary subset.
    top = set(SEVERITY_LADDER[-len(ACTIONABLE_SEVERITIES):])
    assert ACTIONABLE_SEVERITIES == top


def test_a_rogue_dhcp_alert_reaches_the_list():
    """Built from the REAL producer's dict, not a fixture that agrees with the
    consumer — that agreement is what hid this for as long as it existed."""
    from wavr.dhcp_monitor import DhcpRogueAlert

    alert = DhcpRogueAlert(ts="2026-09-04T10:00:00+00:00",
                           extra_server="10.0.0.9",
                           known_servers=("10.0.0.1",),
                           observed_servers=("10.0.0.1", "10.0.0.9")).to_dict()
    items = collect(alerts=[alert])
    assert len(items) == 1, f"the real producer's alert was dropped: {alert}"
    assert items[0].band == DEGRADED

    # And it reads as an errand, not as a kind name. The producer carries no
    # `title` or `detail` at all — reading those gave "Rogue dhcp" with nothing
    # under it, which tells a household nothing about what to do.
    assert "network addresses" in items[0].title.lower()
    assert "10.0.0.9" in items[0].detail, "the producer's own evidence was lost"


def test_a_watch_level_alert_stays_ambient():
    """Everything below `alert` is information, not an errand. Promoting it
    turns the list into a feed, and a feed is not read."""
    assert collect(alerts=[{"kind": "rogue_device", "severity": "note",
                            "title": "A new device"}]) == []
    assert collect(alerts=[{"kind": "x", "severity": "watch", "title": "y"}]) == []


# -- The wording is a template, and a household's words are never the key ------

def _one_of_everything():
    """One item of every kind this module can produce."""
    return collect(
        pending_pairings=[{"requester_name": "Sam's phone", "request_id": "p1",
                           "created_at": "2026-01-01T00:00:00Z"}],
        node_requests=[{"label": "kitchen-radar", "request_id": "n1",
                        "created_ts": "2026-01-01T00:00:00Z"}],
        coverage_rows=[
            {"sensor_id": "radar-1", "room": "sala", "health": "offline",
             "precision_level": "count"},
            {"sensor_id": "radar-2", "room": "", "health": "silent",
             "precision_level": "position"},
            {"sensor_id": "radar-3", "room": "escritório", "health": "failed",
             "precision_level": ""},
        ],
        cameras_needing_url=[{"name": "hall-cam"}],
        discoveries=[{"discovery_id": "d1", "status": "pending",
                      "title": "Camera found at 10.0.0.5",
                      "detail": {"room": "sala"}}],
        alerts=[{"kind": "rogue_dhcp", "severity": "alert",
                 "extra_server": "10.0.0.9", "ts": "2026-01-01T00:00:00Z"},
                {"kind": "something_new", "severity": "critical",
                 "ip": "10.0.0.5", "ts": "2026-01-01T00:00:00Z"}],
        update={"behind": True})


def test_every_sentence_a_row_can_show_is_declared():
    """A row added with an inline literal must fail here.

    `TEMPLATES` is what `test_the_attention_inbox_is_translated` requires a
    translation for. That check is only worth something while the list is
    complete, and a list maintained by hand beside the code it describes is
    exactly the kind that quietly stops being complete. So this drives the
    collector over one of every kind of item and holds the result against it.
    """
    from wavr.attention import TEMPLATES

    undeclared = sorted({t for i in _one_of_everything()
                         for t in (i.title_template, i.detail_template) if t}
                        - set(TEMPLATES))
    assert not undeclared, (
        "these sentences reach a screen and are not in TEMPLATES, so nothing "
        "requires them to be translated:\n  "
        + "\n  ".join(repr(u) for u in undeclared))


def test_no_row_makes_a_household_word_part_of_its_key():
    """The defect this shape exists to prevent.

    The dashboard translates a row by looking up its template. If the values —
    a camera's name, a room's name, a phone's name — are inside that template
    rather than beside it, the key is unique to one household and can never be
    in any catalogue: the row stays English, and the private name is recorded
    as a missing translation.

    Wording this module did not write goes through as `*_text` and is never
    looked up at all, which is the other half of the same rule.
    """
    private = ("Sam's phone", "kitchen-radar", "hall-cam", "sala",
               "escritório", "radar-1", "10.0.0.9", "10.0.0.5")
    leaked = []
    for i in _one_of_everything():
        for t in (i.title_template, i.detail_template):
            for word in private:
                if word in t:
                    leaked.append(f"{word!r} in {t!r}")
    assert not leaked, "household data inside a lookup key:\n  " + "\n  ".join(leaked)


def test_only_wording_from_elsewhere_bypasses_translation():
    """`*_text` is rendered verbatim and never looked up. That is right for
    wording this module did not write, and it is a hole if anything else uses
    it.

    A row that composes its own sentence into `title_text` passes every other
    check in this file: nothing leaks into the catalogue, because nothing is
    looked up at all. It simply stays English for ever, in silence. That is not
    hypothetical — it is the mutation that found this test missing.

    Two sources may speak for themselves. A discovery card's title was composed
    by `discovery_feed` out of what is on the network, and an install's update
    instructions were written by whoever packaged the build. An alert kind Wavr
    has no words for falls back to the identifier, which is not a sentence.
    Everything else must be a template.
    """
    from wavr.attention import _ALERT_WORDS

    rows = list(_one_of_everything())
    # The other half of the update item: an install that DID supply its own
    # instructions. `_one_of_everything` covers the branch that does not.
    rows += collect(update={"behind": True,
                            "instructions": "Run `apt upgrade wavr`."})

    offenders = []
    for i in rows:
        for slot, tpl, text in (("title", i.title_template, i.title_text),
                                ("detail", i.detail_template, i.detail_text)):
            if tpl or not text:
                continue
            if slot == "title" and i.key.startswith("discovery:"):
                continue
            if slot == "detail" and i.key == "update":
                continue
            if (slot == "title" and i.key.startswith("alert:")
                    and i.key.split(":", 1)[1] not in _ALERT_WORDS):
                continue
            offenders.append(f"{i.key} {slot}: {text!r}")
    assert not offenders, (
        "these rows skip the catalogue without being wording from elsewhere, "
        "so they stay English in every language:\n  " + "\n  ".join(offenders))


def test_the_composed_english_still_reads_as_a_sentence():
    """`wavr status` and the tray read `title`/`detail` and translate nothing.
    Handing either a literal `{name}` is the same bug pointing the other way.
    """
    for i in _one_of_everything():
        assert "{" not in i.title and "}" not in i.title, i.title
        assert "{" not in i.detail and "}" not in i.detail, i.detail
        assert i.title.strip(), f"a row with no title: {i.key}"


def test_a_discovery_card_keeps_its_own_words():
    """A discovery title is written by `discovery_feed` out of what is on the
    network. This module cannot translate it and must not pretend to: it goes
    through as text, so no lookup happens and no address is recorded as a
    missing translation."""
    item = next(i for i in _one_of_everything() if i.key.startswith("discovery:"))
    assert item.title_template == ""
    assert item.title_text == "Camera found at 10.0.0.5"
    assert item.title == "Camera found at 10.0.0.5"


def test_an_alert_keeps_its_evidence_outside_the_sentence():
    """"10.0.0.9" alone is not an errand and the sentence alone is not
    evidence — but joined, the sentence carries an IP address into the lookup
    key. Beside it, both survive."""
    item = next(i for i in _one_of_everything() if i.key == "alert:rogue_dhcp")
    assert item.evidence == "extra server: 10.0.0.9"
    assert "10.0.0.9" not in item.detail_template
    assert "10.0.0.9" in item.detail, "the producer's own evidence was lost"
