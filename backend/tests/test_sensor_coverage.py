"""What Wavr can honestly see, and the distinctions that change a decision.

Three of them, and every test here exists for one:

  * a room with no sensor       -> buy something
  * a room with a silent sensor -> go fix it
  * a room with a working one   -> believe the answer
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.sensor_coverage import (
    HEALTH_DISABLED, HEALTH_OFFLINE, HEALTH_OK, HEALTH_UNKNOWN,
    KIND_CAMERA, KIND_HOST, KIND_NODE, SensorCoverage, best_precision, by_room,
    collect_coverage, rooms_without_coverage, summarize,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def cam(name, room="sala"):
    return {"name": name, "room": room}


def node(node_id, *, modality="mmwave", room="cozinha", state="active",
         seen_ago_s=5, name=None, sensor_type="ld2450"):
    seen = (NOW - timedelta(seconds=seen_ago_s)).isoformat() if seen_ago_s is not None else None
    return {"node_id": node_id, "name": name or node_id, "modality": modality,
            "room": room, "state": state, "last_seen_ts": seen,
            "sensor_type": sensor_type}


class _Calib:
    """Stands in for CalibrationStore.

    The key is `homography`, because that is what the real store returns.

    This fake used to emit `h` — and so did the consumer, so both agreed and
    both were wrong: `calibrated` was False for every camera in production while
    every test here passed. A fake that invents the producer's shape tests
    nothing but its own invention, which is why
    `test_the_fake_matches_the_real_calibration_store` exists below.
    """

    def __init__(self, solved=(), mounted=()):
        self._solved, self._mounted = set(solved), set(mounted)

    def get(self, name):
        if name in self._solved:
            return {"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                    "img_w": 640, "img_h": 480}
        if name in self._mounted:
            return {"mount": {"height_m": 2.4}}      # pose only, no homography
        return None


# -- The precision ladder is fusion's, not a second copy ----------------------

@pytest.mark.parametrize("modality,expected", [
    ("network", "house"), ("ble", "room"), ("pir", "room"),
    ("mmwave", "count"), ("camera", "count"),
])
def test_precision_comes_from_the_fusion_scope_table(modality, expected):
    c = SensorCoverage(sensor_id="s", kind=KIND_NODE, modality=modality)
    assert c.precision_level == expected


def test_an_unknown_modality_falls_back_down_not_up():
    # Erring toward `house` matches fusion. Erring toward `position` would let a
    # typo in a modality name promote a sensor to placing people on a floor plan.
    c = SensorCoverage(sensor_id="s", kind=KIND_NODE, modality="quantum-radar")
    assert c.precision_level == "house"


def test_the_position_rung_is_earned_by_calibration():
    rows = collect_coverage(cameras=[cam("hall-cam")], calib=_Calib(),
                            cameras_enabled=["c", "hall-cam"])
    assert rows[0].precision_level == "count", "uncalibrated cameras count, not place"

    rows = collect_coverage(cameras=[cam("hall-cam")],
                            calib=_Calib(solved=["hall-cam"]),
                            cameras_enabled=["c", "hall-cam"])
    assert rows[0].calibrated is True
    assert rows[0].precision_level == "position"


def test_a_mount_pose_alone_is_not_calibration():
    # A mount height tells Wavr where the camera IS; the homography is what turns
    # a pixel into a floor coordinate. Counting the first as the second would
    # promise positions the camera cannot produce.
    rows = collect_coverage(cameras=[cam("hall-cam")],
                            calib=_Calib(mounted=["hall-cam"]),
                            cameras_enabled=["c", "hall-cam"])
    assert rows[0].calibrated is False and rows[0].precision_level == "count"


# -- Health: the offline/absent distinction this module exists for ------------

def test_a_node_that_went_quiet_is_offline_not_absent():
    """The failure that motivated the module.

    `nodes.py` has no "offline" state -- an unplugged radar is still `active`.
    Deriving coverage from live room state reported the kitchen as having no
    sensors at all, which reads as "buy a sensor" when the truth is "go plug the
    one you own back in".
    """
    rows = collect_coverage(nodes=[node("n1", seen_ago_s=3600)], now=NOW)
    assert rows[0].health == HEALTH_OFFLINE
    assert rows[0].room == "cozinha", "it is still installed in that room"
    assert rows[0].observing is False
    # And the room is NOT reported as uncovered.
    assert rooms_without_coverage(["cozinha"], rows) == []


def test_a_fresh_node_is_ok():
    rows = collect_coverage(nodes=[node("n1", seen_ago_s=5)], now=NOW)
    assert rows[0].health == HEALTH_OK and rows[0].observing is True


def test_an_approved_node_that_never_reported_is_offline():
    # It was approved, it is meant to be talking, and it is not. "unknown" would
    # be generous about a node that has never once checked in.
    rows = collect_coverage(nodes=[node("n1", seen_ago_s=None)], now=NOW)
    assert rows[0].health == HEALTH_OFFLINE


def test_a_pending_node_is_not_coverage_at_all():
    """It holds no credential, has no room and no sensor type — nobody has
    decided what it is yet. Listing it put an unplaced node in the house-wide
    bucket beside the Wi-Fi scan, which reads as "something is watching the
    house". It is a question, and the Discovery Inbox is where questions live."""
    assert collect_coverage(nodes=[node("n1", state="pending", seen_ago_s=None)],
                            now=NOW) == []


def test_a_disabled_node_is_a_choice_not_a_fault():
    rows = collect_coverage(nodes=[node("n1", state="disabled")], now=NOW)
    assert rows[0].health == HEALTH_DISABLED


def test_a_revoked_node_is_gone_entirely():
    assert collect_coverage(nodes=[node("n1", state="revoked")], now=NOW) == []


def test_a_corrupt_timestamp_is_not_evidence_of_health():
    rows = collect_coverage(
        nodes=[{"node_id": "n1", "state": "active", "modality": "mmwave",
                "room": "sala", "last_seen_ts": "not-a-date"}], now=NOW)
    assert rows[0].health == HEALTH_UNKNOWN


def test_a_naive_timestamp_is_read_as_utc_not_rejected():
    # SQLite rows written by older code carry no offset. Treating those as
    # unparseable would report every one of them as unknown.
    rows = collect_coverage(
        nodes=[{"node_id": "n1", "state": "active", "modality": "mmwave",
                "room": "sala",
                "last_seen_ts": NOW.replace(tzinfo=None).isoformat()}], now=NOW)
    assert rows[0].health == HEALTH_OK


def test_a_camera_that_is_off_is_disabled_not_offline():
    # ADR-0002: cameras boot OFF. That is a settings click away from working,
    # which is a different errand from a broken camera.
    rows = collect_coverage(cameras=[cam("hall-cam")], cameras_enabled=[])
    assert rows[0].health == HEALTH_DISABLED and rows[0].observing is False


# -- Rooms, and what is honestly not in one ----------------------------------

def test_house_wide_sensors_are_never_pinned_to_a_room():
    rows = collect_coverage(ble=True, network=True)
    assert all(r.room == "" for r in rows)
    assert by_room(rows) == {}, "a roomless sensor must not become a room called ''"
    kinds = {r.kind for r in rows}
    assert kinds == {KIND_HOST}


def test_an_uncovered_room_is_named():
    rows = collect_coverage(cameras=[cam("c", room="sala")], cameras_enabled=["c", "hall-cam"])
    assert rooms_without_coverage(["sala", "cozinha", "garagem"], rows) == [
        "cozinha", "garagem"]


def test_best_precision_ignores_sensors_that_are_not_observing():
    """A switched-off camera cannot place anyone.

    Letting it raise the room's precision would restate the exact overclaim this
    module exists to prevent -- and it would do it in the one number a UI is
    most likely to render large.
    """
    rows = collect_coverage(cameras=[cam("c", room="sala")],
                            calib=_Calib(solved=["c"]), cameras_enabled=[],
                            nodes=[node("n", modality="ble", room="sala")],
                            now=NOW)
    assert best_precision(rows) == "room", "the ble node is the only live one"


def test_best_precision_of_nothing_is_none_not_house():
    assert best_precision([]) == "none"


# -- The serialized shape all three surfaces render ---------------------------

def test_summarize_separates_unobserved_from_uncovered():
    rows = collect_coverage(
        cameras=[cam("hall-cam", room="hall")], cameras_enabled=[],
        nodes=[node("n1", room="cozinha", seen_ago_s=5)],
        ble=True, now=NOW)
    out = summarize(["hall", "cozinha", "garagem"], rows)

    rooms = {r["room"]: r for r in out["rooms"]}
    assert rooms["hall"]["observing"] is False, "camera present but switched off"
    assert rooms["cozinha"]["observing"] is True
    assert out["uncovered"] == ["garagem"]
    assert [s["modality"] for s in out["house_wide"]] == ["ble"]


def test_no_percentage_is_ever_reported():
    """§8: a coverage percentage needs floor geometry Wavr does not have.

    This asserts the absence deliberately -- it is the kind of number that gets
    added later because it looks good on a dashboard.
    """
    out = summarize(["sala"], collect_coverage(
        cameras=[cam("c", room="sala")], calib=_Calib(solved=["c"]),
        cameras_enabled=["c", "hall-cam"]))

    # Check the DATA, not the prose: `note` legitimately contains the word
    # "percentage" because it explains why there isn't one.
    def keys(d):
        return set(d)

    assert not any(k for k in keys(out) if "pct" in k or "percent" in k)
    for room in out["rooms"]:
        assert not any(k for k in keys(room) if "pct" in k or "percent" in k)
        for s in room["sensors"]:
            assert not any(k for k in keys(s) if "pct" in k or "percent" in k)
        # And no bare number that a UI would be tempted to render as one.
        assert not any(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in room.values())


def test_geometry_stays_absent_until_it_is_real():
    c = SensorCoverage(sensor_id="c", kind=KIND_CAMERA, modality="camera")
    assert c.geometry is None
    assert "geometry" not in c.to_dict(), "an absent polygon is not an empty one"


# -- Wiring robustness, without hiding wiring bugs ---------------------------

def test_absent_stores_yield_no_sensors_rather_than_crashing():
    assert collect_coverage() == []


def test_a_callable_store_is_accepted():
    rows = collect_coverage(cameras=lambda: [cam("c")], cameras_enabled=["c", "hall-cam"])
    assert rows[0].sensor_id == "c"


def test_a_wrong_method_name_is_not_swallowed():
    """The bug shape that has recurred four times in this codebase.

    `_safe_list` must not catch an AttributeError from calling something that
    does not exist: reporting "no sensors" for a wiring bug renders as "your
    house has no coverage", which is the worst possible way to hide it.
    """
    class Broken:
        def __call__(self):
            raise AttributeError("'CameraStore' object has no attribute 'list_cameras'")

    with pytest.raises(AttributeError):
        collect_coverage(cameras=Broken())


def test_a_calibration_read_failure_does_not_delete_the_camera():
    class Angry:
        def get(self, name):
            raise RuntimeError("db locked")

    rows = collect_coverage(cameras=[cam("c")], calib=Angry(), cameras_enabled=["c", "hall-cam"])
    assert len(rows) == 1 and rows[0].calibrated is False


def test_rows_without_an_id_are_skipped_not_rendered_blank():
    rows = collect_coverage(cameras=[{"room": "sala"}],
                            nodes=[{"state": "active"}], cameras_enabled=["c", "hall-cam"])
    assert rows == []


def test_the_fake_matches_the_real_calibration_store():
    """The fake above and `CalibrationStore` must agree about their key names.

    They did not, for as long as this file has existed. `sensor_coverage` read
    `h`, the fake emitted `h`, every test passed — and the real store has only
    ever emitted `homography`, so no camera in production was ever reported as
    calibrated and the `position` rung was unreachable for all of them.

    Comparing the fake against the REAL producer is the only version of this
    test that can catch that, so it does exactly that and nothing else.
    """
    import tempfile
    from pathlib import Path

    from wavr.calib_store import CalibrationStore

    store = CalibrationStore(str(Path(tempfile.mkdtemp()) / "calib.db"))
    store.set_homography("hall-cam", [1, 0, 0, 0, 1, 0, 0, 0, 1], 640, 480,
                         quality=0.9)
    real = store.get("hall-cam")
    fake = _Calib(solved=["hall-cam"]).get("hall-cam")

    assert set(fake) <= set(real), (
        f"the fake emits keys the real store does not: {sorted(set(fake) - set(real))}")

    # And the consumer reads a key BOTH of them actually have.
    rows = collect_coverage(cameras=[cam("hall-cam")], calib=store,
                            cameras_enabled=["hall-cam"])
    assert rows[0].calibrated is True, (
        "a camera calibrated through the real store still reports uncalibrated")
    assert rows[0].precision_level == "position"
