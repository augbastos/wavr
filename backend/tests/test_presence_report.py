from datetime import datetime, timedelta, timezone

from wavr.device_meta import DeviceMeta
from wavr.presence_report import build_report

NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> DeviceMeta:
    return DeviceMeta(str(tmp_path / "t.db"))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed(meta: DeviceMeta, mac: str, *, first_seen=None, last_seen=None,
          name=None, device_type=None) -> None:
    """Insert/overwrite a device_meta row with EXPLICIT timestamps -- the
    public DeviceMeta API always stamps "now", so presence-report tests (which
    need deterministic ages relative to a fixed `NOW`) seed rows directly via
    the underlying sqlite connection, same package."""
    meta._conn.execute(
        """INSERT INTO device_meta (mac, name, first_seen, last_seen, device_type)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(mac) DO UPDATE SET
               name = excluded.name, first_seen = excluded.first_seen,
               last_seen = excluded.last_seen, device_type = excluded.device_type""",
        (mac, name, first_seen, last_seen, device_type),
    )
    meta._conn.commit()


# ---- empty store ------------------------------------------------------------

def test_empty_store_yields_empty_report(tmp_path):
    meta = _store(tmp_path)
    report = build_report(meta, now=NOW)
    assert report["device_count"] == 0
    assert report["first_activity_at"] is None
    assert report["last_activity_at"] is None
    assert report["quiet_period_seconds"] is None
    assert report["currently_present"] == []
    assert report["recently_away"] == []
    assert report["stale"] == []
    assert report["most_present"] == []


# ---- bucketing by age --------------------------------------------------------

def test_device_seen_recently_is_currently_present(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:01", first_seen=_iso(NOW - timedelta(days=1)),
          last_seen=_iso(NOW - timedelta(minutes=1)), name="Phone")
    report = build_report(meta, now=NOW)
    macs = [d["mac"] for d in report["currently_present"]]
    assert macs == ["aa:aa:aa:aa:aa:01"]
    assert report["recently_away"] == []
    assert report["stale"] == []
    entry = report["currently_present"][0]
    assert entry["name"] == "Phone"
    assert "quiet_for_seconds" not in entry  # only meaningful for absent devices
    assert entry["tenure_seconds"] == timedelta(days=1, minutes=-1).total_seconds()


def test_device_seen_a_few_hours_ago_is_recently_away(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:02", first_seen=_iso(NOW - timedelta(days=1)),
          last_seen=_iso(NOW - timedelta(hours=2)))
    report = build_report(meta, now=NOW)
    assert report["currently_present"] == []
    assert [d["mac"] for d in report["recently_away"]] == ["aa:aa:aa:aa:aa:02"]
    assert report["recently_away"][0]["quiet_for_seconds"] == 2 * 3600.0
    assert report["stale"] == []


def test_device_not_seen_in_a_week_is_stale(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:03", first_seen=_iso(NOW - timedelta(days=30)),
          last_seen=_iso(NOW - timedelta(days=10)))
    report = build_report(meta, now=NOW)
    assert report["currently_present"] == []
    assert report["recently_away"] == []
    assert [d["mac"] for d in report["stale"]] == ["aa:aa:aa:aa:aa:03"]
    assert report["stale"][0]["quiet_for_seconds"] == 10 * 24 * 3600.0


def test_custom_windows_are_respected(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:04", first_seen=_iso(NOW - timedelta(hours=1)),
          last_seen=_iso(NOW - timedelta(minutes=30)))
    report = build_report(meta, now=NOW, active_window_s=3600, stale_after_s=7200)
    assert [d["mac"] for d in report["currently_present"]] == ["aa:aa:aa:aa:aa:04"]


# ---- device with no sighting yet --------------------------------------------

def test_named_device_never_seen_is_counted_but_unbucketed(tmp_path):
    meta = _store(tmp_path)
    meta.set_name("aa:aa:aa:aa:aa:05", "Fridge")  # named/pinned, never scanned
    report = build_report(meta, now=NOW)
    assert report["device_count"] == 1
    assert report["currently_present"] == []
    assert report["recently_away"] == []
    assert report["stale"] == []


def test_malformed_timestamp_is_ignored_not_fatal(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:06", first_seen="not-a-timestamp",
          last_seen="also-garbage")
    report = build_report(meta, now=NOW)
    assert report["device_count"] == 1
    assert report["currently_present"] == []
    assert report["recently_away"] == []
    assert report["stale"] == []
    assert report["first_activity_at"] is None
    assert report["last_activity_at"] is None


# ---- house-wide first/last activity + quiet period --------------------------

def test_first_and_last_activity_span_all_devices(tmp_path):
    meta = _store(tmp_path)
    _seed(meta, "aa:aa:aa:aa:aa:07", first_seen=_iso(NOW - timedelta(days=5)),
          last_seen=_iso(NOW - timedelta(days=3)))
    _seed(meta, "aa:aa:aa:aa:aa:08", first_seen=_iso(NOW - timedelta(days=2)),
          last_seen=_iso(NOW - timedelta(minutes=5)))
    report = build_report(meta, now=NOW)
    assert report["first_activity_at"] == _iso(NOW - timedelta(days=5))
    assert report["last_activity_at"] == _iso(NOW - timedelta(minutes=5))
    assert report["quiet_period_seconds"] == 5 * 60.0


# ---- most_present ranking -----------------------------------------------------

def test_most_present_ranks_by_tenure_among_currently_present_only(tmp_path):
    meta = _store(tmp_path)
    # Present, long tenure.
    _seed(meta, "aa:aa:aa:aa:aa:09", first_seen=_iso(NOW - timedelta(days=10)),
          last_seen=_iso(NOW - timedelta(minutes=1)))
    # Present, short tenure.
    _seed(meta, "aa:aa:aa:aa:aa:10", first_seen=_iso(NOW - timedelta(hours=1)),
          last_seen=_iso(NOW - timedelta(minutes=1)))
    # Long tenure but NOT currently present (stale) -- must be excluded.
    _seed(meta, "aa:aa:aa:aa:aa:11", first_seen=_iso(NOW - timedelta(days=60)),
          last_seen=_iso(NOW - timedelta(days=20)))
    report = build_report(meta, now=NOW)
    assert [d["mac"] for d in report["most_present"]] == [
        "aa:aa:aa:aa:aa:09", "aa:aa:aa:aa:aa:10",
    ]


def test_top_n_limits_most_present_list(tmp_path):
    meta = _store(tmp_path)
    for i in range(10):
        _seed(meta, f"aa:aa:aa:aa:aa:{i:02x}",
              first_seen=_iso(NOW - timedelta(days=i + 1)),
              last_seen=_iso(NOW - timedelta(minutes=1)))
    report = build_report(meta, now=NOW, top_n=3)
    assert len(report["most_present"]) == 3
    # Highest tenure (oldest first_seen) first.
    assert report["most_present"][0]["mac"] == "aa:aa:aa:aa:aa:09"
