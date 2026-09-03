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
    """The wire shape, pinned. Adding a key here must be a deliberate act —
    this dict is what gets stored, replayed and read by other surfaces."""
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert set(ev.to_dict().keys()) == {
        "room", "modality", "presence", "motion",
        "breathing_bpm", "heart_bpm", "confidence", "ts", "targets", "identities",
        "count", "sensor_id",
    }


def test_a_source_with_no_instance_identity_reports_an_empty_sensor_id():
    """Empty, not absent and not invented.

    The CSI/RuView normalizer has no per-instance identity to give: there is one
    such sense in the house. Reliability keyed on an empty id falls back to the
    modality constant, which is the pre-existing behaviour."""
    assert normalize_ruview(RUVIEW_FRAME, room="sala").sensor_id == ""

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
