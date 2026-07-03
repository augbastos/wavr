from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine


def _ev(modality, presence=True, targets=(), conf=0.9):
    return SensingEvent(room="sala", modality=modality, presence=presence,
                        motion=1.0, breathing_bpm=None, heart_bpm=None,
                        confidence=conf, ts="2026-07-02T00:00:00+00:00",
                        targets=targets)


def test_target_to_dict_roundtrip():
    t = Target(id=1, x=1.5, y=2.0, posture="sitting", velocity=0.0, confidence=0.8)
    d = t.to_dict()
    assert d["x"] == 1.5 and d["posture"] == "sitting" and d["z"] is None


def test_event_targets_default_empty_and_serializes():
    e = _ev("network")
    assert e.targets == ()
    assert e.to_dict()["targets"] == []          # JSON-friendly list


def test_fusion_passes_through_targets_from_best_source():
    f = FusionEngine()
    t_csi = (Target(id=1, x=1.0, y=1.0, confidence=0.7),)
    t_cam = (Target(id=1, x=2.0, y=2.0, posture="standing", confidence=0.9),)
    f.update(_ev("wifi_csi", targets=t_csi))
    rs = f.update(_ev("camera", targets=t_cam))
    assert rs.targets == [t_cam[0].to_dict()]     # camera (1.0) beats wifi_csi (0.85)


def test_fusion_targets_empty_when_no_source_has_them():
    f = FusionEngine()
    rs = f.update(_ev("network"))
    assert rs.targets == []


def test_fusion_ignores_targets_of_absent_source():
    f = FusionEngine()
    rs = f.update(_ev("camera", presence=False,
                      targets=(Target(id=1, x=0.0, y=0.0),)))
    assert rs.targets == []


def test_posture_only_target_allowed():
    # camera gives posture without position (no homography yet)
    t = Target(id=1, x=None, y=None, posture="lying", confidence=0.9)
    assert t.to_dict()["x"] is None and t.to_dict()["posture"] == "lying"


def test_dead_sources_targets_are_not_ghosted_into_an_empty_room():
    # A dead source's stale (presence=True) targets must not pass through the
    # target selection just because it's the only source that ever reported
    # any — that ghosts a phantom person into a room a fresh vacant reading
    # says is empty.
    T = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    f = FusionEngine(now_fn=lambda: T)

    dead_ts = (T - timedelta(seconds=200)).isoformat()  # > default stale_s (90) -> dead
    dead = SensingEvent(room="sala", modality="camera", presence=True, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=0.9,
                        ts=dead_ts, targets=(Target(id=1, x=1.0, y=1.0, confidence=0.9),))
    f.update(dead)

    fresh_vacant = SensingEvent(room="sala", modality="network", presence=False, motion=0.0,
                                breathing_bpm=None, heart_bpm=None, confidence=0.8,
                                ts=T.isoformat(), targets=())
    rs = f.update(fresh_vacant)

    assert rs.occupied is False
    assert rs.confidence == 0.0
    assert rs.targets == []
