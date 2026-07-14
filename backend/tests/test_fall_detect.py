"""A9 -- fall/no-motion suspicion (RESEARCH-GRADE, ADR-0003): unit tests for the pure
geometry rule (`lying_outside_zone` over `wavr.housemap.in_rest_zone`), the edge-triggered
dwell/flicker-debounce timer (`FallDetector`), the `/api/alerts` merge shape, and the
`wavr.house_status` physical-layer reason. App-level end-to-end wiring (a real camera-like
source through the real create_app pipeline) lives in test_fall_detect_wiring.py, mirroring
test_watch.py / test_house_status_wiring.py's own split.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.alert_severity import SEVERITY_ALERT
from wavr.fall_detect import DISCLAIMER, FallAlert, FallDetector, lying_outside_zone
from wavr.housemap import DEFAULT_MAP

# ------------------------------------------------------------------------------------- #
# lying_outside_zone: pure geometry, no dwell/timing involved.
# ------------------------------------------------------------------------------------- #

REST_ZONE_HOUSE = {
    "version": 2, "units": "m", "floors": [{
        "id": "f0", "name": "T", "level": 0, "walls": [], "features": [], "backdrop": None,
        "rooms": [{"id": "r1", "name": "quarto", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
        "zones": [{"id": "z1", "name": "bed", "kind": "rest",
                   "polygon": [[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0]]}],
    }],
}


def test_lying_outside_zone_true_when_no_zone_covers_the_room():
    targets = [{"id": 1, "x": 1.0, "y": 1.0, "posture": "lying", "confidence": 0.9}]
    assert lying_outside_zone(DEFAULT_MAP, "quarto", targets) is True


def test_lying_inside_bed_zone_never_flags():
    targets = [{"id": 1, "x": 1.0, "y": 1.0, "posture": "lying", "confidence": 0.9}]
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is False


def test_lying_just_outside_bed_zone_still_flags():
    targets = [{"id": 1, "x": 3.5, "y": 2.5, "posture": "lying", "confidence": 0.9}]
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is True


def test_standing_or_sitting_never_flags_regardless_of_position():
    for posture in ("standing", "sitting", None):
        targets = [{"id": 1, "x": 3.5, "y": 2.5, "posture": posture, "confidence": 0.9}]
        assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is False


def test_lying_with_unknown_position_never_flags_honesty_gate():
    # An uncalibrated camera can't tell in/out of a zone -- it must never manufacture a
    # verdict either way (this is exactly what would make the feature fire on ordinary
    # sleep in every unpositioned bedroom camera).
    targets = [{"id": 1, "x": None, "y": None, "posture": "lying", "confidence": 0.9}]
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is False


def test_lying_outside_zone_any_of_multiple_targets():
    targets = [
        {"id": 1, "x": 1.0, "y": 1.0, "posture": "lying", "confidence": 0.9},   # in the bed
        {"id": 2, "x": 3.5, "y": 2.5, "posture": "lying", "confidence": 0.9},   # NOT in the bed
    ]
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is True


def test_lying_outside_zone_accepts_target_objects_not_only_dicts():
    from wavr.events import Target
    targets = [Target(id=1, x=3.5, y=2.5, posture="lying", confidence=0.9)]
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", targets) is True


def test_lying_outside_zone_empty_targets_never_flags():
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", []) is False
    assert lying_outside_zone(REST_ZONE_HOUSE, "quarto", None) is False


# ------------------------------------------------------------------------------------- #
# FallDetector: edge-triggered dwell + flicker debounce (posture noise tolerance).
# ------------------------------------------------------------------------------------- #

def test_no_alert_before_dwell_elapses():
    d = FallDetector(dwell_s=60.0)
    assert d.record("quarto", True, "2026-07-10T00:00:00+00:00") is None
    assert d.record("quarto", True, "2026-07-10T00:00:30+00:00") is None   # 30s < 60s


def test_alert_fires_once_dwell_elapses_carries_room_and_duration_only():
    d = FallDetector(dwell_s=60.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    alert = d.record("quarto", True, "2026-07-10T00:01:05+00:00")   # 65s >= 60s
    assert isinstance(alert, FallAlert)
    dd = alert.to_dict()
    assert dd["kind"] == "fall_suspected" and dd["severity"] == SEVERITY_ALERT
    assert dd["room"] == "quarto" and dd["duration_s"] == 65.0
    assert dd["disclaimer"] == DISCLAIMER
    assert set(dd) == {"kind", "severity", "room", "duration_s", "disclaimer", "ts"}
    # never a target position/posture/confidence field, ever
    blob = repr(dd)
    assert '"x"' not in blob and '"y"' not in blob and "posture" not in blob


def test_alert_edge_triggered_no_repeat_while_still_at_risk():
    d = FallDetector(dwell_s=10.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    a1 = d.record("quarto", True, "2026-07-10T00:00:15+00:00")
    assert a1 is not None
    a2 = d.record("quarto", True, "2026-07-10T00:00:30+00:00")   # still at risk -> no re-fire
    assert a2 is None
    assert len(d.recent_alerts()) == 1


def test_episode_clears_and_rearms_after_a_real_gap():
    d = FallDetector(dwell_s=10.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    assert d.record("quarto", True, "2026-07-10T00:00:15+00:00") is not None   # fires
    # they got up for well over the flicker-grace window (6s) -> episode fully clears
    d.record("quarto", False, "2026-07-10T00:00:30+00:00")
    d.record("quarto", True, "2026-07-10T00:01:00+00:00")     # new episode starts
    assert d.record("quarto", True, "2026-07-10T00:01:05+00:00") is None       # only 5s in
    a2 = d.record("quarto", True, "2026-07-10T00:01:15+00:00")                 # 15s -> re-fires
    assert a2 is not None
    assert len(d.recent_alerts()) == 2


def test_posture_flicker_within_grace_does_not_reset_dwell():
    # A single missed/ambiguous frame (a brief False reading under FLICKER_GRACE_S) must
    # NOT reset the dwell clock -- the episode is treated as continuous.
    d = FallDetector(dwell_s=20.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    d.record("quarto", True, "2026-07-10T00:00:08+00:00")
    d.record("quarto", False, "2026-07-10T00:00:10+00:00")   # 2s flicker gap -- tolerated
    alert = d.record("quarto", True, "2026-07-10T00:00:22+00:00")   # 22s since 00:00 -> fires
    assert alert is not None
    assert alert.duration_s == 22.0


def test_flicker_beyond_grace_resets_the_dwell_clock():
    d = FallDetector(dwell_s=20.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    # 10s gap since the last True reading already exceeds FLICKER_GRACE_S (6s) -> this
    # False call clears the episode immediately (a second False call is a no-op on an
    # already-cleared room, so it doesn't matter that we also send one below).
    d.record("quarto", False, "2026-07-10T00:00:10+00:00")
    d.record("quarto", False, "2026-07-10T00:00:20+00:00")
    # dwell restarts from here even though 22s already passed since the original start
    assert d.record("quarto", True, "2026-07-10T00:00:25+00:00") is None
    alert = d.record("quarto", True, "2026-07-10T00:00:46+00:00")   # 21s since the NEW start
    assert alert is not None and alert.duration_s == 21.0


def test_rooms_are_independent():
    d = FallDetector(dwell_s=10.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    d.record("sala", True, "2026-07-10T00:00:00+00:00")
    a_quarto = d.record("quarto", True, "2026-07-10T00:00:15+00:00")
    a_sala_early = d.record("sala", True, "2026-07-10T00:00:05+00:00")
    assert a_quarto is not None
    assert a_sala_early is None


def test_reset_clears_edge_state_but_not_the_ring():
    d = FallDetector(dwell_s=5.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    d.record("quarto", True, "2026-07-10T00:00:06+00:00")
    assert len(d.recent_alerts()) == 1
    d.reset()
    assert len(d.recent_alerts()) == 1               # ring untouched
    assert d.active_alerts() == []                    # latch cleared
    # re-arms: the very next dwell-elapsed reading fires again
    d.record("quarto", True, "2026-07-10T00:01:00+00:00")
    assert d.record("quarto", True, "2026-07-10T00:01:06+00:00") is not None


def test_active_alerts_only_lists_currently_latched_rooms():
    d = FallDetector(dwell_s=5.0)
    d.record("quarto", True, "2026-07-10T00:00:00+00:00")
    d.record("quarto", True, "2026-07-10T00:00:06+00:00")   # fires, latches
    assert [a.room for a in d.active_alerts()] == ["quarto"]
    d.record("quarto", False, "2026-07-10T00:00:20+00:00")  # well past grace -> clears
    assert d.active_alerts() == []


def test_malformed_ts_never_crashes():
    d = FallDetector(dwell_s=5.0)
    assert d.record("quarto", True, "not-a-timestamp") is None
    assert d.recent_alerts() == []


def test_alert_ring_is_bounded():
    d = FallDetector(dwell_s=0.0, max_alerts=3)
    for i in range(10):
        room = f"r{i}"
        d.record(room, True, "2026-07-10T00:00:00+00:00")   # starts the episode
        d.record(room, True, "2026-07-10T00:00:00+00:00")   # elapsed 0 >= dwell 0 -> fires
    assert len(d.recent_alerts()) == 3


# ------------------------------------------------------------------------------------- #
# GET /api/alerts merge shape (mirrors test_watch.py's own intrusion merge test).
# ------------------------------------------------------------------------------------- #

class _FakeInvService:
    def latest_inventory(self):
        return []

    def recent_alerts(self):
        return []


def test_alerts_stream_merges_fall_suspected():
    from wavr.api_inventory import build_inventory_router
    log = FallDetector(dwell_s=0.0, now_fn=lambda: "2026-07-10T00:00:00+00:00")
    log.record("quarto", True, "2026-07-10T00:00:00+00:00")   # starts the episode
    log.record("quarto", True, "2026-07-10T00:00:00+00:00")   # elapsed 0 >= dwell 0 -> fires
    app = FastAPI()
    app.include_router(build_inventory_router(_FakeInvService(), fall_log=log))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    kinds = [a["kind"] for a in body["alerts"]]
    assert "fall_suspected" in kinds
    fa = next(a for a in body["alerts"] if a["kind"] == "fall_suspected")
    assert fa["severity"] == "alert" and fa["room"] == "quarto"
    assert fa["disclaimer"] == DISCLAIMER
    assert "x" not in fa and "y" not in fa and "targets" not in fa and "posture" not in fa


def test_alerts_stream_omits_fall_when_unwired():
    from wavr.api_inventory import build_inventory_router
    app = FastAPI()
    app.include_router(build_inventory_router(_FakeInvService()))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    assert body["alerts"] == []
