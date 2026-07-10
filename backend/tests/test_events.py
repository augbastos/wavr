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
        "breathing_bpm", "heart_bpm", "confidence", "ts", "targets", "identities",
        "count",
    }

def test_missing_vitals_and_confidence_default():
    frame = {"type": "sensing_update", "classification": {"presence": False},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    ev = normalize_ruview(frame, room="quarto")
    assert ev.presence is False and ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.confidence == 0.0

def test_normalize_ruview_reads_optional_targets():
    raw = {"classification": {"presence": True, "confidence": 0.8},
           "features": {"motion_band_power": 2.0},
           "targets": [{"id": 1, "x": 1.2, "y": 0.8, "posture": "standing"},
                       {"junk": True},          # tolerated, skipped
                       "not-a-dict"]}
    e = normalize_ruview(raw, room="sala")
    assert len(e.targets) == 1
    assert e.targets[0].x == 1.2 and e.targets[0].posture == "standing"


def test_normalize_ruview_no_targets_key_unchanged():
    e = normalize_ruview({"classification": {"presence": True}}, room="sala")
    assert e.targets == ()


def test_normalize_ruview_clamps_negative_confidence():
    frame = {"classification": {"presence": True, "confidence": -3.0},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    e = normalize_ruview(frame, room="sala")
    assert e.confidence == 0.0


def test_normalize_ruview_clamps_overlarge_confidence():
    frame = {"classification": {"presence": True, "confidence": 999.0},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    e = normalize_ruview(frame, room="sala")
    assert e.confidence == 1.0
