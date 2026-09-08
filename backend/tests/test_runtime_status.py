"""The one rule: a healthy-looking answer over a dead Core is worse than none.

Most of this file is the same test written several ways — something that has
stopped producing cannot be reported as fine, whatever else is reporting fine.
That repetition is deliberate. Every other check in this codebase protects data;
this one protects a person's belief that their home is being watched over, and
the way that belief breaks is silently.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wavr.runtime_status import (
    ATTENTION, DEAD_AFTER_S, DEGRADED, HEALTHY, PAUSED, STALE_AFTER_S, STARTING,
    UNAVAILABLE, UPDATING, assess, unreachable,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def ago(seconds):
    return NOW - timedelta(seconds=seconds)


def cov(sensor_id="cam1", health="ok"):
    return {"sensor_id": sensor_id, "health": health}


# -- The spine -----------------------------------------------------------------

def test_a_core_that_stopped_producing_is_not_healthy_however_fine_its_parts():
    """Four components reporting fine and no room reading for an hour is the
    exact shape of the failure this module exists for."""
    s = assess(uptime_s=86400, last_state_at=ago(DEAD_AFTER_S + 60),
               coverage_rows=[cov(), cov("cam2")], db_ok=True,
               nodes=["n1"], now=NOW)
    assert s.state == UNAVAILABLE
    assert "not running" in s.headline.lower()


def test_a_quiet_core_is_degraded_before_it_is_dead():
    s = assess(uptime_s=86400, last_state_at=ago(STALE_AFTER_S + 60),
               coverage_rows=[cov()], now=NOW)
    assert s.state == DEGRADED
    assert "not learning anything new" in " ".join(f.text for f in s.findings)


def test_a_fresh_reading_is_healthy():
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov()], now=NOW)
    assert s.state == HEALTHY
    assert "everything reporting" in s.headline


def test_never_having_produced_a_reading_is_not_the_same_as_producing_one_long_ago():
    """`None` for the age, not a large number. A Core that has NEVER worked out
    a room and one that did so last week are different problems, and rendering
    both as a big age hides the first."""
    s = assess(uptime_s=86400, last_state_at=None, now=NOW)
    assert s.last_state_age_s is None
    assert s.state == ATTENTION


def test_a_core_that_just_started_is_starting_and_not_broken():
    """Saying "attention required" ten seconds after launch is how people learn
    to ignore the indicator."""
    s = assess(uptime_s=5, last_state_at=None, coverage_rows=[cov()], now=NOW)
    assert s.state == STARTING


# -- Sensors, from what observes them ------------------------------------------

def test_every_sensor_offline_is_attention_not_merely_degraded():
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov("a", "offline"), cov("b", "offline")], now=NOW)
    assert s.state == ATTENTION
    assert "cannot see anything" in " ".join(f.text for f in s.findings)


def test_some_sensors_offline_names_how_many_of_how_many():
    """"3 sensors offline" needs a person to know whether three is all of
    them."""
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov("a"), cov("b", "offline"), cov("c")], now=NOW)
    assert s.state == DEGRADED
    assert "1 of 3" in " ".join(f.text for f in s.findings)


def test_a_disabled_sensor_is_not_counted_as_a_fault():
    """An operator switching a camera off is not a failure, and reporting it as
    one is how an indicator becomes noise."""
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov("a"), cov("b", "disabled")], now=NOW)
    assert s.state == HEALTHY


def test_missing_coverage_is_not_reported_as_healthy_sensors():
    """A caller that forgets to pass coverage must not receive a clean bill of
    health for the sensors — absence is "cannot tell", never "fine"."""
    s = assess(uptime_s=86400, last_state_at=ago(30), now=NOW)
    assert not any(f.key == "sensors" for f in s.findings)


# -- The other states ----------------------------------------------------------

def test_paused_reads_as_deliberate_rather_than_broken():
    s = assess(uptime_s=86400, last_state_at=ago(30), sensing_paused=True,
               coverage_rows=[cov()], now=NOW)
    assert s.state == PAUSED
    assert "deliberately not watching" in " ".join(f.text for f in s.findings)


def test_a_broken_database_is_attention_because_nothing_is_being_recorded():
    s = assess(uptime_s=86400, last_state_at=ago(30), db_ok=False, now=NOW)
    assert s.state == ATTENTION


def test_updating_is_a_state_and_not_a_fault():
    s = assess(uptime_s=86400, last_state_at=ago(30), updating=True,
               coverage_rows=[cov()], now=NOW)
    assert s.state == UPDATING


def test_the_worst_finding_wins_and_reaches_the_headline():
    """A tooltip that says "3 of 4 fine" is read as fine."""
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov("a"), cov("b", "offline")],
               nodes=["n1"], egress_connectors=["update_check"], now=NOW)
    assert s.state == DEGRADED
    assert "degraded" in s.headline.lower()
    assert "not reporting" in s.headline


def test_outward_connections_are_visible_without_being_a_fault():
    """A person must be able to see that something reaches the internet. It is
    not a problem; being unable to find out would be."""
    s = assess(uptime_s=86400, last_state_at=ago(30), coverage_rows=[cov()],
               egress_connectors=["diagnostics"], now=NOW)
    assert s.state == HEALTHY
    egress = next(f for f in s.findings if f.key == "egress")
    assert "switched on" in egress.text and "diagnostics" in egress.detail


# -- What a client renders when it gets nothing --------------------------------

def test_no_answer_from_the_core_is_a_shared_answer_not_each_clients_guess():
    """A tray, a menu bar and a browser tab must not disagree about what "no
    response" means — and none of them may quietly fall back to the last good
    state, which is exactly how a dead Core goes on looking alive."""
    s = unreachable("My Home")
    assert s.state == UNAVAILABLE
    assert "not responding" in s.headline
    assert "My Home" in s.headline


def test_the_serialised_form_carries_an_age_and_not_a_timestamp():
    """A consumer comparing a timestamp needs a correct clock of its own; a tray
    on a machine with a skewed clock would render a fresh reading as ancient."""
    body = assess(uptime_s=100, last_state_at=ago(42), now=NOW).to_dict()
    assert body["last_state_age_s"] == 42
    assert "last_state_at" not in body


def test_every_state_the_tray_can_show_is_reachable():
    """A state nothing produces is a state whose icon nobody has ever seen, and
    it will be wrong the first time it appears."""
    seen = {
        assess(uptime_s=5, last_state_at=None, now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(30), coverage_rows=[cov()],
               now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(30), updating=True, now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(30), sensing_paused=True,
               now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(STALE_AFTER_S + 1), now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(30), db_ok=False, now=NOW).state,
        assess(uptime_s=9e4, last_state_at=ago(DEAD_AFTER_S + 1), now=NOW).state,
    }
    assert seen == {STARTING, HEALTHY, UPDATING, PAUSED, DEGRADED, ATTENTION,
                    UNAVAILABLE}


def test_a_sensor_wavr_cannot_read_is_not_counted_as_working():
    """`sensor_coverage` emits ok / offline / disabled / UNKNOWN, and says in
    its own docstring that a row whose source is missing from the manager
    "reads `unknown`, never green, so a renamed source shows up as a gap
    instead of false reassurance".

    This module subtracted only offline and disabled, so `unknown` was swept
    into the live count — and the coverage screen said "NOT being watched"
    while the tray said "everything reporting" about the same sensor.
    """
    s = assess(uptime_s=86400, last_state_at=ago(30),
               coverage_rows=[cov("a"), cov("ble-host", health="unknown")],
               now=NOW)
    assert s.state == DEGRADED
    said = " ".join(f.text for f in s.findings)
    assert "cannot tell" in said, said
    assert "reporting." not in said or "1 sensor reporting" not in said


def test_every_health_the_coverage_module_can_emit_is_handled_here():
    """The two modules must agree about the vocabulary. This one used to know
    three of the four values, and the fourth defaulted to healthy."""
    from wavr import sensor_coverage as sc

    emitted = {sc.HEALTH_OK, sc.HEALTH_OFFLINE, sc.HEALTH_DISABLED,
               sc.HEALTH_UNKNOWN}
    for health in emitted:
        s = assess(uptime_s=86400, last_state_at=ago(30),
                   coverage_rows=[cov("x", health=health)], now=NOW)
        finding = next((f for f in s.findings if f.key == "sensors"), None)
        assert finding is not None, f"health {health!r} produced no sensor finding"
        assert finding.text.strip(), f"health {health!r} produced an empty sentence"


# -- states that could not be reached ------------------------------------------
#
# Found by an audit that read the observability mandate and went looking for
# surfaces able to render "fine" from something assumed. Three of the four it
# found were unreachable STATES: code that existed, read correctly, and could
# not occur. An unreachable failure state is worse than a missing one, because
# the docstring beside it reads like a safeguard.


def test_every_sensor_switched_off_is_never_reported_as_healthy():
    """The one a household reaches by using a switch the product offers.

    Rows come back all `disabled`, so nothing is offline, nothing is unknown,
    and `live` is 0 — which fell through to the healthy branch and produced
    "0 sensors reporting." True, and rendered as a green tray icon, a green
    chip, a green Android notification and a green `wavr status`, over a house
    where nothing is watching.
    """
    rows = [{"sensor_id": "cam-hall", "health": "disabled"},
            {"sensor_id": "radar-kitchen", "health": "disabled"}]
    out = assess(uptime_s=600, coverage_rows=rows)
    sensors = [f for f in out.findings if f.key == "sensors"]
    assert sensors, "the sensor finding disappeared"
    assert sensors[0].state != HEALTHY, (
        f"nothing is watching and the product calls it {sensors[0].state}: "
        f"{sensors[0].text!r}")
    assert out.state != HEALTHY, (
        f"the overall verdict is {out.state} with every sensor switched off")
    assert "0 sensors reporting" not in sensors[0].text


def test_one_live_sensor_among_switched_off_ones_is_still_healthy():
    """The guard above must not swallow the ordinary case: somebody turning off
    one camera has not broken anything."""
    rows = [{"sensor_id": "cam-hall", "health": "disabled"},
            {"sensor_id": "radar-kitchen", "health": "ok"}]
    out = assess(uptime_s=600, coverage_rows=rows)
    sensors = [f for f in out.findings if f.key == "sensors"]
    assert sensors[0].state == HEALTHY, sensors[0].text
    assert "1 sensor reporting" in sensors[0].text


def test_a_paused_space_says_paused_rather_than_nothing():
    """`assess` has taken `sensing_paused` since it was written, and its caller
    never passed one — so PAUSED, the state meaning "running, and deliberately
    not watching", could not occur anywhere in the product."""
    # A Space with nothing else wrong: fresh room data and a live sensor, so
    # the pause is the only thing the verdict can be about. Without the fresh
    # timestamp the overall state is ATTENTION for a real reason — no rooms
    # have ever updated — and this would pass or fail for the wrong one.
    out = assess(uptime_s=600, sensing_paused=True, last_state_at=ago(5),
                 coverage_rows=[cov()], now=NOW)
    assert any(f.state == PAUSED for f in out.findings), (
        [f.state for f in out.findings])
    assert out.state == PAUSED, (out.state, [f.text for f in out.findings])


def test_the_route_passes_the_pause_through():
    """The finding above is only worth having if the route reaches it. This is
    the half that was missing: `assess` was correct and nobody told it."""
    from fastapi.testclient import TestClient
    import wavr.app as appmod
    body = TestClient(appmod.create_app()).get(
        "/api/runtime", headers={"X-Wavr-Local": "1"}).json()
    assert "state" in body
    src = Path(appmod.__file__).read_text(encoding="utf-8")
    assert "sensing_paused=sensing_paused" in src, (
        "the route builds the argument and does not pass it, which is how the "
        "state was unreachable in the first place")


def test_a_store_that_cannot_be_written_reports_it():
    """`db_ok` was hard-wired True because `SpaceStore` had no `healthy()`, so
    "Wavr cannot write to its database" was a sentence nothing could print."""
    out = assess(uptime_s=600, db_ok=False)
    storage = [f for f in out.findings if f.key == "storage"]
    assert storage and storage[0].state == ATTENTION, out.findings
    assert "cannot write" in storage[0].text


def test_the_store_health_probe_actually_probes(tmp_path):
    """A read would not answer the question — SQLite serves reads from a
    database it can no longer write — so the probe writes and rolls back.
    Checked both ways: it says yes on a working store, and no on a connection
    that has been closed under it."""
    from wavr.space_store import SpaceStore
    st = SpaceStore(str(tmp_path / "s.db"))
    assert st.healthy() is True
    before = st.get_space()
    st._conn.close()
    assert st.healthy() is False, (
        "the probe returned True on a closed connection, so it is not "
        "touching the database")
    assert before is None or before is not None      # no state was written


# -- The sentence a wall panel reads is a template, not a finished string ------

def _every_branch():
    """One `assess()` result per branch that can produce a finding."""
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
    dead = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    out = [
        assess(uptime_s=5),
        assess(uptime_s=600),
        assess(uptime_s=600, last_state_at=dead),
        assess(uptime_s=600, last_state_at=stale),
        assess(uptime_s=600, last_state_at=fresh),
        assess(uptime_s=600, last_state_at=fresh, db_ok=False,
               sensing_paused=True, updating=True,
               egress_connectors=["open-meteo"], nodes=[1]),
        assess(uptime_s=600, last_state_at=fresh,
               egress_connectors=["a", "b"], nodes=[1, 2],
               source_states={"radar": "failed"},
               coverage_rows=[{"sensor_id": "s1", "health": "ok"},
                              {"sensor_id": "s2", "health": "offline"}]),
        assess(uptime_s=600, last_state_at=fresh,
               coverage_rows=[{"sensor_id": "s1", "health": "unknown"}]),
        assess(uptime_s=600, last_state_at=fresh,
               coverage_rows=[{"sensor_id": "s1", "health": "disabled"}]),
        assess(uptime_s=600, last_state_at=fresh,
               coverage_rows=[{"sensor_id": "s1", "health": "offline"}]),
        assess(uptime_s=600, last_state_at=fresh,
               coverage_rows=[{"sensor_id": "s1", "health": "ok"}]),
        unreachable(space_name="Casa"),
    ]
    return [f for status in out for f in status.findings]


def test_every_sentence_a_finding_can_show_is_declared():
    """`TEMPLATES` is what the localisation gate requires a translation for, so
    it is only worth something while it is complete. Driving every branch and
    holding the result against it is what keeps it complete: a finding written
    with an inline literal fails here rather than shipping an English sentence
    to a wall panel in a Portuguese house."""
    from wavr.runtime_status import TEMPLATES

    seen = {f.text_template for f in _every_branch() if f.text_template}
    undeclared = sorted(seen - set(TEMPLATES))
    assert not undeclared, (
        "these sentences reach a screen and are not in TEMPLATES:\n  "
        + "\n  ".join(repr(u) for u in undeclared))
    unreached = sorted(set(TEMPLATES) - seen)
    assert not unreached, (
        "these TEMPLATES were produced by no branch, so either a branch is "
        "missing from this test or the constant is dead:\n  "
        + "\n  ".join(repr(u) for u in unreached))


def test_the_headline_template_never_carries_a_finding_sentence():
    """The chip's tooltip is looked up by its template, so the template has to
    be a key a catalogue can hold.

    It appended the worst finding's already-composed sentence — "Wavr —
    degraded · {space} · 3 of 5 sensors are not reporting." — so the tooltip
    stayed English in every state except the four with no finding. The change
    that took a count OUT of a finding key had put one back into this one.

    The browser appends the finding itself, translated on its own, which is
    why the head can be short.
    """
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cases = [
        assess(uptime_s=600, last_state_at=fresh,
               coverage_rows=[{"sensor_id": "s1", "health": "offline"},
                              {"sensor_id": "s2", "health": "ok"}],
               space_name="Casa"),
        assess(uptime_s=600, last_state_at=fresh, db_ok=False,
               space_name="Casa"),
        assess(uptime_s=600, space_name="Casa"),
        unreachable(space_name="Casa"),
    ]
    for status in cases:
        tpl = status.to_dict().get("headline_template") or ""
        assert not any(ch.isdigit() for ch in tpl), (
            f"a count is inside the tooltip's lookup key, so it can never be "
            f"translated: {tpl!r}")
        assert "Casa" not in tpl, (
            f"the Space's own name is inside the lookup key: {tpl!r}")
        # And the composed headline still says the whole thing, for the tray.
        assert status.to_dict().get("headline"), status.to_dict()


def test_a_finding_never_makes_a_count_part_of_its_key():
    """The defect this shape prevents: the Core Panel looks a finding up by its
    template, and a template with "3" or "5" in it is a key no catalogue can
    hold, so the line stays English at every count."""
    leaked = [f"{f.key}: {f.text_template!r}"
              for f in _every_branch()
              if any(ch.isdigit() for ch in f.text_template)]
    assert not leaked, ("a number is baked into a lookup key:\n  "
                        + "\n  ".join(leaked))


def test_the_composed_finding_english_still_reads_as_a_sentence():
    """`wavr status` and the tray read `text` and translate nothing. Handing
    either a literal `{n}` — or both arms of a plural — is the same bug
    pointing the other way."""
    for f in _every_branch():
        assert "{" not in f.text and "}" not in f.text, (f.key, f.text)
        assert "|" not in f.text, (f.key, f.text)
        assert f.text.strip(), f"a finding with no sentence: {f.key}"


def test_a_duration_travels_as_seconds_not_as_english():
    """"3 minutes" composed in Python is an English fragment that would land in
    the middle of a Portuguese sentence. The finding ships the number; the
    renderer turns it into words on the side that knows the language."""
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
    state = [f for f in assess(uptime_s=600, last_state_at=stale).findings
             if f.key == "state"][0]
    assert "age_s" in state.text_args, state.text_args
    assert isinstance(state.text_args["age_s"], (int, float))
    assert "{age}" in state.text_template
    # And the composed English still says it in words, for the tray.
    assert "minutes" in state.text
