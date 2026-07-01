from wavr.roomstate import RoomState
from wavr.storage import Storage


def rs(room, occupied, ts):
    return RoomState(room=room, occupied=occupied, confidence=0.8,
                     vitals={"breathing_bpm": 13.0, "heart_bpm": 65.0},
                     sources=[{"modality": "wifi_csi", "presence": occupied, "confidence": 0.7}],
                     explanation="x", ts=ts)


def test_insert_and_recent_roundtrips_chronologically():
    st = Storage(":memory:")
    st.insert_state(rs("sala", True, "2026-07-01T10:00:00+00:00"))
    st.insert_state(rs("quarto", False, "2026-07-01T10:00:01+00:00"))
    rows = st.recent()
    assert [r["room"] for r in rows] == ["sala", "quarto"]
    assert rows[0]["occupied"] is True and rows[1]["occupied"] is False
    assert rows[0]["sources"][0]["modality"] == "wifi_csi"   # JSON columns round-trip
    assert set(rows[0].keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}
    st.close()


def test_recent_limit_keeps_newest():
    st = Storage(":memory:")
    for i in range(5):
        st.insert_state(rs("sala", True, f"2026-07-01T10:00:0{i}+00:00"))
    rows = st.recent(limit=2)
    assert [r["ts"] for r in rows] == ["2026-07-01T10:00:03+00:00", "2026-07-01T10:00:04+00:00"]
    st.close()
