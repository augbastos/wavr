from wavr.roomstate import RoomState

def test_roomstate_to_dict_has_exact_keys():
    rs = RoomState(room="quarto", occupied=True, confidence=0.72,
                   vitals={"breathing_bpm": 14.2, "heart_bpm": 68.0},
                   sources=[{"modality": "wifi_csi", "presence": True, "confidence": 0.61}],
                   explanation="wifi: respiração → 72% ocupado",
                   ts="2026-07-01T16:20:01+00:00")
    d = rs.to_dict()
    assert set(d.keys()) == {"room", "occupied", "confidence", "vitals", "sources",
                             "targets", "identities", "person_count", "explanation", "ts",
                             "precision_level", "precision_pct", "precision_next"}
    assert d["occupied"] is True and d["confidence"] == 0.72


def test_roomstate_identities_default_empty_and_round_trip():
    # Defaults to an empty list (opt-in identity), so every existing construction
    # is unaffected; a populated list round-trips through to_dict unchanged.
    assert RoomState(room="casa", occupied=False, confidence=0.0).to_dict()["identities"] == []
    # Precision axis defaults: pre-ladder 'none'/0/None so every old construction is unaffected.
    _d = RoomState(room="casa", occupied=False, confidence=0.0).to_dict()
    assert _d["precision_level"] == "none" and _d["precision_pct"] == 0 and _d["precision_next"] is None
    rs = RoomState(room="casa", occupied=True, confidence=0.7,
                   identities=[{"person": "alice", "source": "ble", "rssi": -55}])
    assert rs.to_dict()["identities"] == [{"person": "alice", "source": "ble", "rssi": -55}]
