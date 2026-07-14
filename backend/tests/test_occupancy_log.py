from datetime import datetime, timedelta, timezone

from wavr.occupancy_log import OccupancyLog


def _store(tmp_path=None):
    return OccupancyLog(str(tmp_path / "t.db") if tmp_path else ":memory:")


# ---- append_if_changed() -- edge-triggered dedup -------------------------------

def test_first_append_always_inserts():
    log = _store()
    inserted = log.append_if_changed("sala", True, 0.9, 2, "2026-07-01T10:00:00+00:00")
    assert inserted is True
    assert log.timeline("sala") == [
        {"room": "sala", "occupied": True, "person_count": 2, "confidence": 0.9,
         "ts": "2026-07-01T10:00:00+00:00"}
    ]


def test_unchanged_repeat_is_a_noop():
    log = _store()
    log.append_if_changed("sala", True, 0.9, 2, "2026-07-01T10:00:00+00:00")
    inserted = log.append_if_changed("sala", True, 0.9, 2, "2026-07-01T10:00:05+00:00")
    assert inserted is False
    assert len(log.timeline("sala")) == 1  # the repeat never landed a second row


def test_occupied_flip_inserts():
    log = _store()
    log.append_if_changed("sala", True, 0.9, 2, "2026-07-01T10:00:00+00:00")
    inserted = log.append_if_changed("sala", False, 0.1, None, "2026-07-01T10:05:00+00:00")
    assert inserted is True
    assert len(log.timeline("sala")) == 2


def test_person_count_change_inserts_even_if_occupied_and_confidence_unchanged():
    log = _store()
    log.append_if_changed("sala", True, 0.9, 1, "2026-07-01T10:00:00+00:00")
    inserted = log.append_if_changed("sala", True, 0.9, 2, "2026-07-01T10:05:00+00:00")
    assert inserted is True


def test_small_confidence_drift_is_a_noop_but_large_drift_inserts():
    log = _store()
    log.append_if_changed("sala", True, 0.90, 1, "2026-07-01T10:00:00+00:00")
    # < 1% drift: no-op
    assert log.append_if_changed("sala", True, 0.905, 1, "2026-07-01T10:00:05+00:00") is False
    # >= 1% drift: real change
    assert log.append_if_changed("sala", True, 0.92, 1, "2026-07-01T10:00:10+00:00") is True


def test_rooms_are_independent():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    log.append_if_changed("quarto", False, 0.1, None, "2026-07-01T10:00:00+00:00")
    assert len(log.timeline("sala")) == 1
    assert len(log.timeline("quarto")) == 1
    assert set(log.rooms()) == {"sala", "quarto"}


def test_dedup_cache_survives_restart_against_the_same_file(tmp_path):
    p = str(tmp_path / "t.db")
    OccupancyLog(p).append_if_changed("sala", True, 0.9, 1, "2026-07-01T10:00:00+00:00")
    # A brand-new instance against the SAME file must warm its cache from disk, so an
    # identical repeat right after "restart" is still correctly a no-op.
    log2 = OccupancyLog(p)
    inserted = log2.append_if_changed("sala", True, 0.9, 1, "2026-07-01T10:05:00+00:00")
    assert inserted is False
    assert len(log2.timeline("sala")) == 1


def test_in_memory_store_for_tests():
    log = OccupancyLog(":memory:")
    assert log.append_if_changed("sala", True, 0.9, 1, "2026-07-01T10:00:00+00:00") is True


# ---- retention pruning ----------------------------------------------------------

def test_prune_deletes_rows_older_than_retention_but_keeps_recent():
    log = OccupancyLog(":memory:", retention_days=1)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    log.append_if_changed("sala", True, 0.9, None, old_ts)
    # A genuinely different state triggers the real insert -> the post-insert prune runs.
    log.append_if_changed("sala", False, 0.1, None,
                          datetime.now(timezone.utc).isoformat())
    rows = log.timeline("sala")
    assert len(rows) == 1
    assert rows[0]["occupied"] is False  # the 100-day-old row was pruned away


def test_retention_disabled_when_non_positive():
    log = OccupancyLog(":memory:", retention_days=0)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=9999)).isoformat()
    log.append_if_changed("sala", True, 0.9, None, old_ts)
    log.append_if_changed("sala", False, 0.1, None,
                          datetime.now(timezone.utc).isoformat())
    assert len(log.timeline("sala")) == 2  # nothing pruned


# ---- timeline() -------------------------------------------------------------------

def test_timeline_orders_oldest_to_newest():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    log.append_if_changed("sala", False, 0.1, None, "2026-07-01T09:00:00+00:00")  # inserted out of order
    log.append_if_changed("sala", True, 0.8, None, "2026-07-01T11:00:00+00:00")
    ts_order = [r["ts"] for r in log.timeline("sala")]
    assert ts_order == sorted(ts_order)


def test_timeline_filters_by_start_and_end():
    log = _store()
    for h in range(5):
        log.append_if_changed("sala", h % 2 == 0, 0.9, None, f"2026-07-01T{10+h:02d}:00:00+00:00")
    rows = log.timeline("sala", start="2026-07-01T11:00:00+00:00",
                        end="2026-07-01T13:00:00+00:00")
    assert [r["ts"][11:13] for r in rows] == ["11", "12"]  # half-open [start, end)


def test_timeline_without_room_returns_every_room():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    log.append_if_changed("quarto", True, 0.9, None, "2026-07-01T10:00:01+00:00")
    assert {r["room"] for r in log.timeline()} == {"sala", "quarto"}


def test_timeline_limit_is_clamped_defensively():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    # A negative limit means "no LIMIT" to SQLite -- must clamp to >= 1, never a raw
    # unbounded `LIMIT ?`. An absurdly large one must not raise either.
    assert log.timeline("sala", limit=-5) != []
    assert log.timeline("sala", limit=999999)


# ---- routine() -- time-weighted hourly baseline ------------------------------------

def _dt(days_from_d0: int, hh: int, mm: int = 0) -> str:
    d0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return (d0 + timedelta(days=days_from_d0, hours=hh, minutes=mm)).isoformat()


def test_routine_time_weights_across_three_days():
    """3 days of data at hour 10 (room 'sala'):
      D0: occupied the WHOLE hour (10:00 True -> 11:00 False)         -> 3600/3600 occupied
      D1: occupied HALF the hour (10:00 True -> 10:30 False)          -> 1800/3600 occupied
      D2: occupied the WHOLE hour again (10:00 True -> 11:00 False)   -> 3600/3600 occupied
    Total occupied/total across the 3 days = (3600+1800+3600)/10800 = 0.8333...
    """
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, _dt(0, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(0, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(1, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(1, 10, 30))
    log.append_if_changed("sala", True, 0.9, None, _dt(2, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(2, 11, 0))

    now = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=2, hours=12)
    base = log.routine("sala", weeks=1, now=now)
    hour10 = base["hours"][10]
    assert hour10["samples"] == 3
    assert hour10["trusted"] is True
    assert hour10["probability"] == round(9000 / 10800, 3)  # implementation rounds to 3dp

    # Hour 3 is never occupied in this dataset -- it's covered (state HOLDS from the
    # previous day's vacate through the night) on D1 and D2 but not D0 (nothing precedes
    # the very first-ever row), so it honestly reads "always vacant, 2 days of evidence".
    hour3 = base["hours"][3]
    assert hour3["samples"] == 2
    assert hour3["probability"] == 0.0


def test_routine_empty_log_returns_all_24_hours_as_no_data():
    log = _store()
    base = log.routine("sala", now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert len(base["hours"]) == 24
    assert all(h["probability"] is None and h["samples"] == 0 for h in base["hours"])


def test_routine_hours_before_the_very_first_ever_row_are_honestly_no_data():
    """A single log entry, ever: hours strictly before its time-of-day have NOTHING to
    hold their state from (no `prior` row exists at all) -- unlike the hold-over case
    above, this is a genuine "no data yet" gap."""
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, "2026-06-01T14:00:00+00:00")
    now = datetime(2026, 6, 1, 20, 0, 0, tzinfo=timezone.utc)
    base = log.routine("sala", weeks=1, now=now)
    assert base["hours"][5]["samples"] == 0
    assert base["hours"][5]["probability"] is None
    assert base["hours"][15]["samples"] == 1
    assert base["hours"][15]["probability"] == 1.0


# ---- is_unusual() -------------------------------------------------------------------

def test_is_unusual_reports_insufficient_data_with_no_history():
    log = _store()
    result = log.is_unusual("sala", True, at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert result["unusual"] is None
    assert result["baseline_probability"] is None
    assert result["samples"] == 0


def test_is_unusual_false_when_current_matches_baseline():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, _dt(0, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(0, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(1, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(1, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(2, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(2, 11, 0))
    now = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=2, hours=10, minutes=30)
    # Room usually occupied at 10h (baseline 1.0) and IS occupied now -> not unusual.
    result = log.is_unusual("sala", True, at=now, weeks=1)
    assert result["samples"] == 3
    assert result["baseline_probability"] == 1.0
    assert result["unusual"] is False


def test_is_unusual_true_when_current_contradicts_baseline():
    log = _store()
    log.append_if_changed("sala", True, 0.9, None, _dt(0, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(0, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(1, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(1, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(2, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(2, 11, 0))
    now = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=2, hours=10, minutes=30)
    # Room is ALWAYS occupied at 10h (baseline 1.0) but is reported VACANT right now.
    result = log.is_unusual("sala", False, at=now, weeks=1)
    assert result["baseline_probability"] == 1.0
    assert result["unusual"] is True


def test_is_unusual_insufficient_when_hour_has_fewer_than_min_samples_days():
    log = _store()
    # Only 2 distinct days of coverage at hour 10 -- below the default min_samples=3.
    log.append_if_changed("sala", True, 0.9, None, _dt(0, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(0, 11, 0))
    log.append_if_changed("sala", True, 0.9, None, _dt(1, 10, 0))
    log.append_if_changed("sala", False, 0.1, None, _dt(1, 11, 0))
    now = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=1, hours=10, minutes=30)
    result = log.is_unusual("sala", True, at=now, weeks=1)
    assert result["unusual"] is None
    assert result["samples"] == 2
