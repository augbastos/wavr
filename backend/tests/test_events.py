from wavr.events import SensingEvent, normalize_ruview

RUVIEW_FRAME = {
    "type": "sensing_update",
    "classification": {"presence": True, "confidence": 0.43},
    "features": {"motion_band_power": 9.7758},
    "vital_signs": {"breathing_rate_bpm": 9.707, "heart_rate_bpm": 46.22},
    "timestamp": 1782924055.636,
}

def test_normalize_sets_wifi_csi_modality_and_maps_fields():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert ev.room == "sala"
    assert ev.modality == "wifi_csi"
    assert ev.presence is True
    assert ev.motion == 9.7758
    assert ev.breathing_bpm == 9.707
    assert ev.heart_bpm == 46.22
    assert ev.confidence == 0.43
    assert ev.ts.startswith("2026-") and ev.ts.endswith("+00:00")

def test_to_dict_has_exact_canonical_keys():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert set(ev.to_dict().keys()) == {
        "room", "modality", "presence", "motion",
        "breathing_bpm", "heart_bpm", "confidence", "ts",
    }

def test_missing_vitals_and_confidence_default():
    frame = {"type": "sensing_update", "classification": {"presence": False},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    ev = normalize_ruview(frame, room="quarto")
    assert ev.presence is False and ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.confidence == 0.0
