from wavr.roomstate import RoomState

def test_roomstate_to_dict_has_exact_keys():
    rs = RoomState(room="quarto", occupied=True, confidence=0.72,
                   vitals={"breathing_bpm": 14.2, "heart_bpm": 68.0},
                   sources=[{"modality": "wifi_csi", "presence": True, "confidence": 0.61}],
                   explanation="wifi: respiração → 72% ocupado",
                   ts="2026-07-01T16:20:01+00:00")
    d = rs.to_dict()
    assert set(d.keys()) == {"room", "occupied", "confidence", "vitals", "sources", "targets", "explanation", "ts"}
    assert d["occupied"] is True and d["confidence"] == 0.72
