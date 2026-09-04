"""The one rule: a healthy-looking answer over a dead Core is worse than none.

Most of this file is the same test written several ways — something that has
stopped producing cannot be reported as fine, whatever else is reporting fine.
That repetition is deliberate. Every other check in this codebase protects data;
this one protects a person's belief that their home is being watched over, and
the way that belief breaks is silently.
"""
from datetime import datetime, timedelta, timezone

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
