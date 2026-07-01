from wavr.events import SensingEvent
from wavr.fusion import FusionEngine


def ev(room, modality, presence, conf, br=None, hr=None):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=br, heart_bpm=hr, confidence=conf,
                        ts="2026-07-01T10:00:00+00:00")


def test_single_present_modality_makes_room_occupied():
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", True, 0.9))   # strength = 0.85 * 0.9 = 0.765
    assert rs.room == "sala"
    assert rs.occupied is True
    assert 0.0 < rs.confidence <= 1.0
    assert rs.sources[0]["modality"] == "wifi_csi"


def test_high_weight_camera_overrides_low_weight_network():
    f = FusionEngine(weights={"camera": 1.0, "network": 0.3})
    f.update(ev("quarto", "network", False, 0.5))
    rs = f.update(ev("quarto", "camera", True, 0.95))
    assert rs.occupied is True          # camera (present, heavy) beats network (absent, light)
    assert len(rs.sources) == 2


def test_vitals_surface_from_wifi_csi():
    f = FusionEngine()
    rs = f.update(ev("quarto", "wifi_csi", True, 0.9, br=14.0, hr=66.0))
    assert rs.vitals == {"breathing_bpm": 14.0, "heart_bpm": 66.0}


def test_all_absent_makes_room_empty_with_zero_confidence():
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", False, 0.4))
    assert rs.occupied is False
    assert rs.confidence == 0.0


def test_explanation_lists_modalities():
    f = FusionEngine()
    f.update(ev("quarto", "network", False, 0.4))
    rs = f.update(ev("quarto", "camera", True, 0.9))
    assert "network" in rs.explanation and "camera" in rs.explanation


def test_weak_lone_source_scores_below_strong_lone_source():
    # A lone coarse source (network) must not report the same confidence as a
    # lone precise source (camera) — the old num/den made both 100%.
    f = FusionEngine()
    net = f.update(ev("casa", "network", True, 0.6))    # strength 0.5 * 0.6 = 0.30
    cam = f.update(ev("quintal", "camera", True, 0.9))  # strength 1.0 * 0.9 = 0.90
    assert net.confidence < cam.confidence
    assert cam.confidence > 0.5
