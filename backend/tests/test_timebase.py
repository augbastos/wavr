"""Timestamps from other people's clocks.

The tests worth reading are the ones about what Wavr REFUSES to do: it will not
correct a skew big enough to be a misconfiguration, it will not accept a time in
the future, and it will not move a timestamp without saying so.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.timebase import (
    MAX_CORRECTION_S, SRC_ASSUMED_UTC, SRC_CORRECTED, SRC_RECEIVED, SRC_STATED,
    TimeBase, TimeError, looks_like_a_timezone, parse,
)

T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def at(**kw):
    return (T0 + timedelta(**kw)).isoformat()


def tb(now=T0):
    return TimeBase(now_fn=lambda: now)


# -- Parsing -------------------------------------------------------------------

def test_a_zoned_timestamp_survives_the_round_trip():
    assert parse("2026-09-04T13:00:00+01:00") == T0


def test_a_naive_timestamp_is_read_as_utc_the_way_fusion_always_did():
    assert parse("2026-09-04T12:00:00") == T0


def test_junk_is_refused_rather_than_becoming_a_moment():
    with pytest.raises(TimeError):
        parse("last tuesday")


# -- The estimate --------------------------------------------------------------

def test_a_first_party_source_is_taken_at_its_word():
    """It calls datetime.now(timezone.utc) in this very process. Measuring an
    offset would measure the cost of a function call."""
    s = tb().normalize(at(), source_id="camera", trusted=True)
    assert s.source == SRC_STATED and s.correction_s == 0.0


def test_a_slow_clock_is_corrected_forward():
    """Two minutes slow used to mean every reading looked stale on arrival, so
    the provider's evidence was quietly discarded and nothing said why."""
    t = tb()
    for _ in range(5):
        t.normalize(at(seconds=-30), source_id="ha")
    s = t.normalize(at(seconds=-30), source_id="ha")
    assert s.source == SRC_CORRECTED
    assert s.at == T0
    assert s.correction_s == pytest.approx(30.0)


def test_the_minimum_lag_is_used_so_one_slow_delivery_does_not_shift_everything():
    """Apparent lag is offset PLUS transit and the two cannot be separated. The
    fastest delivery seen had the least transit mixed in."""
    t = tb()
    t.normalize(at(seconds=-5), source_id="p")     # 5s lag
    t.normalize(at(seconds=-40), source_id="p")    # 40s: a slow delivery
    assert t.estimate("p").offset_s == pytest.approx(5.0)


def test_under_correcting_is_the_direction_it_errs_in():
    """`min` can only ever under-correct. That costs a source a little trust;
    over-correcting would make stale evidence look current, which is the failure
    that actually matters."""
    t = tb()
    for lag in (10, 12, 15, 30):
        t.normalize(at(seconds=-lag), source_id="p")
    assert t.estimate("p").offset_s <= 10.0


def test_the_original_is_always_kept():
    """A pipeline that quietly moved timestamps would make every downstream
    "why did it think that?" unanswerable."""
    t = tb()
    t.normalize(at(seconds=-30), source_id="ha")
    s = t.normalize(at(seconds=-30), source_id="ha")
    assert s.stated == T0 - timedelta(seconds=30)
    assert s.to_dict()["stated"] == s.stated.isoformat()


# -- The refusals --------------------------------------------------------------

def test_a_timezone_sized_skew_is_reported_not_absorbed():
    """Correcting it would paper over a misconfiguration the operator has to fix,
    and the correction would rest on an estimate far outside the range the
    reasoning supports."""
    t = tb()
    s = t.normalize(at(seconds=-3600), source_id="ha")
    assert s.source == SRC_RECEIVED
    assert s.at == T0, "the receipt time, which is wrong by at most transit"
    assert "timezone" in t.estimate("ha").note
    assert t.estimate("ha").usable is False


def test_an_unusable_clock_is_named_in_the_summary():
    t = tb()
    t.normalize(at(seconds=-7200), source_id="ha")
    assert t.to_dict()["unusable"] == ["ha"]


def test_a_skew_just_inside_the_bound_is_still_corrected():
    t = tb()
    s = t.normalize(at(seconds=-(MAX_CORRECTION_S - 1)), source_id="p")
    assert s.source == SRC_CORRECTED


def test_a_timestamp_is_never_in_the_future():
    """A clock running ahead would produce evidence whose decay window has not
    started, so it would stay fresh forever and keep voting long after the room
    changed."""
    s = tb().normalize(at(seconds=45), source_id="fast")
    assert s.at <= T0


# `normalize` has THREE returns, and "a timestamp is never in the future" has to
# hold on all of them. It was tested on one. Deleting the `min()` from the
# trusted/no-source path passed the whole suite, which is the definition of an
# untested guarantee: the code was right and nothing was holding it there.
#
# The two below are the paths that were unguarded by any test. They matter more
# than the one that was covered, because they are the paths a FIRST-PARTY sensor
# takes — a camera or a node whose clock runs fast, whose evidence would then
# never leave the freshness window and would vote for an empty room forever.

def test_a_trusted_source_with_a_fast_clock_is_still_not_in_the_future():
    """`trusted=True` means "do not estimate an offset for this source", not
    "believe a timestamp that has not happened yet"."""
    s = tb().normalize(at(seconds=45), source_id="camera", trusted=True)
    assert s.at <= T0, "a trusted source was allowed to stamp the future"


def test_a_source_with_no_identity_and_a_fast_clock_is_not_in_the_future():
    """No `source_id` means no per-source estimate is possible, so the timestamp
    is taken as given — as given, but still not from the future."""
    s = tb().normalize(at(seconds=45))
    assert s.at <= T0, "an unidentified source was allowed to stamp the future"


def test_a_fast_clock_is_pulled_back_by_its_own_estimate():
    t = tb()
    for _ in range(4):
        t.normalize(at(seconds=20), source_id="fast")
    s = t.normalize(at(seconds=20), source_id="fast")
    assert s.at <= T0
    assert t.estimate("fast").offset_s == pytest.approx(-20.0)


def test_a_naive_timestamp_from_an_external_provider_is_flagged():
    """The assumption is correct by construction for a first-party source and a
    guess for anything else. An hour-shaped error downstream is much easier to
    diagnose when something recorded that a guess was made."""
    s = tb().normalize("2026-09-04T12:00:00", source_id="ha")
    assert s.assumed_utc is True
    assert s.source == SRC_ASSUMED_UTC
    assert s.to_dict()["assumed_utc"] is True


def test_a_zoned_timestamp_is_not_flagged_as_assumed():
    s = tb().normalize("2026-09-04T12:00:00+00:00", source_id="ha")
    assert s.assumed_utc is False


def test_a_bare_date_is_not_mistaken_for_a_zoned_timestamp():
    """Its own hyphens would otherwise read as a negative offset."""
    s = tb().normalize("2026-09-04", source_id="ha")
    assert s.assumed_utc is True


def test_an_anonymous_source_gets_no_estimate():
    """Pooling two providers' samples into one average would produce a number
    that describes neither of them."""
    t = tb()
    t.normalize(at(seconds=-30))
    assert t.to_dict()["clocks"] == []


# -- The plain-language read ---------------------------------------------------

def test_a_whole_hour_reads_as_a_timezone():
    assert "timezone" in looks_like_a_timezone(3600.0)
    assert "timezone" in looks_like_a_timezone(-7200.0)


def test_the_half_hour_zones_are_recognised_too():
    assert looks_like_a_timezone(1800.0)


def test_ordinary_drift_does_not_read_as_a_timezone():
    assert looks_like_a_timezone(37.0) == ""
    assert looks_like_a_timezone(-90.0) == ""


def test_the_summary_says_what_cannot_be_measured():
    """A screen full of offsets with no caveat invites more confidence than
    one-way samples can support."""
    note = tb().to_dict()["note"]
    assert "slow clock" in note and "slow network" in note


def test_the_sample_count_travels_with_the_estimate():
    t = tb()
    for _ in range(3):
        t.normalize(at(seconds=-5), source_id="p")
    assert t.estimate("p").to_dict()["samples"] == 3


def test_forgetting_a_source_drops_its_estimate():
    t = tb()
    t.normalize(at(seconds=-5), source_id="p")
    t.forget("p")
    assert t.to_dict()["clocks"] == []
