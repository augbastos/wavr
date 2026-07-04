# Regression guard for the safety-alarm taxonomy fix (unifi.md #1 / aqara-home.md).
# A life-safety hazard sensor (smoke / water-leak / flood) must NOT be tagged as a
# plain "environmental" device -- that mislabel made the UI claim the alarm does not
# count toward occupancy confidence, which is misleading for a life-safety class.
# Smoke/leak/flood rows carry category "safety-alarm" and detects ["hazard"].
from wavr.app import _load_device_catalog

_HAZARD_TERMS = ("smoke", "leak", "flood")


def _blob(row):
    return (str(row.get("name", "")) + " " + str(row.get("modality", ""))).lower()


def test_catalog_loads_as_nonempty_list():
    cat = _load_device_catalog()
    assert isinstance(cat, list) and len(cat) > 0


def test_no_environmental_row_is_actually_a_hazard_sensor():
    # the bug: smoke/water-leak sensors sat in category "environmental" alongside
    # plain temp/humidity/air sensors, so the fusion sentence dismissed them.
    cat = _load_device_catalog()
    offenders = [r.get("id") for r in cat
                 if r.get("category") == "environmental"
                 and any(t in _blob(r) for t in _HAZARD_TERMS)]
    assert offenders == [], offenders


def test_safety_alarm_rows_detect_hazard_and_never_environment():
    cat = _load_device_catalog()
    sa = [r for r in cat if r.get("category") == "safety-alarm"]
    assert len(sa) >= 4, "expected the smoke + water-leak/flood rows retagged"
    for row in sa:
        assert row.get("detects") == ["hazard"], (row.get("id"), row.get("detects"))


def test_known_hazard_ids_are_retagged():
    cat = _load_device_catalog()
    by_id = {r.get("id"): r for r in cat}
    for rid in ("aqara-water-leak-sensor", "fibaro-flood-sensor",
                "aqara-smoke-detector", "sonoff-snzb-05p-temp-alarm"):
        row = by_id.get(rid)
        assert row is not None, rid
        assert row.get("category") == "safety-alarm", rid
        assert row.get("detects") == ["hazard"], rid
