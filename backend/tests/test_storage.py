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
    assert set(rows[0].keys()) == {"room", "occupied", "confidence", "sources", "explanation", "ts"}
    assert "vitals" not in rows[0]   # ADR-0002: vitals are live-only, never persisted
    st.close()


def test_recent_limit_keeps_newest():
    st = Storage(":memory:")
    for i in range(5):
        st.insert_state(rs("sala", True, f"2026-07-01T10:00:0{i}+00:00"))
    rows = st.recent(limit=2)
    assert [r["ts"] for r in rows] == ["2026-07-01T10:00:03+00:00", "2026-07-01T10:00:04+00:00"]
    st.close()


# --- Item 3: event-based history (transitions, not per-tick) -------------------

def test_first_sighting_persists_one_baseline_row():
    # (a) The very first time a room is seen persists exactly ONE baseline row,
    # regardless of whether it starts occupied or empty.
    st = Storage(":memory:")
    assert st.insert_if_transition(rs("sala", True, "2026-07-01T10:00:00+00:00")) is True
    assert st.insert_if_transition(rs("quarto", False, "2026-07-01T10:00:00+00:00")) is True
    rows = st.recent()
    assert [(r["room"], r["occupied"]) for r in rows] == [("sala", True), ("quarto", False)]
    st.close()


def test_steady_ticks_after_first_sighting_persist_nothing():
    # (b) N steady ticks with UNCHANGED occupancy (only confidence jittering)
    # write NO new row -- history is events, not per-tick noise.
    st = Storage(":memory:")
    assert st.insert_if_transition(rs("sala", True, "2026-07-01T10:00:00+00:00")) is True
    for i in range(1, 6):
        # same occupancy, different ts (confidence is fixed by the helper -- the
        # point is that occupancy did not flip, so nothing is written)
        assert st.insert_if_transition(rs("sala", True, f"2026-07-01T10:00:0{i}+00:00")) is False
    assert len(st.recent()) == 1   # still just the one baseline row
    st.close()


def test_occupied_empty_occupied_persists_exactly_transitions_in_order():
    # (c) occupied -> empty -> occupied (with steady ticks between) persists
    # exactly the three transition rows, in order.
    st = Storage(":memory:")
    seq = [
        (True,  "t0"),   # first sighting  -> WRITE
        (True,  "t1"),   # steady          -> skip
        (False, "t2"),   # flip to empty   -> WRITE
        (False, "t3"),   # steady          -> skip
        (True,  "t4"),   # flip to occupied-> WRITE
        (True,  "t5"),   # steady          -> skip
    ]
    wrote = [st.insert_if_transition(rs("sala", occ, ts)) for occ, ts in seq]
    assert wrote == [True, False, True, False, True, False]
    rows = st.recent()
    assert [(r["occupied"], r["ts"]) for r in rows] == [
        (True, "t0"), (False, "t2"), (True, "t4"),
    ]
    st.close()


def test_transition_rows_keep_the_persisted_shape():
    # (d) event-based rows carry the SAME byte-for-byte shape as insert_state rows.
    st = Storage(":memory:")
    st.insert_if_transition(rs("sala", True, "2026-07-01T10:00:00+00:00"))
    row = st.recent()[0]
    assert set(row.keys()) == {"room", "occupied", "confidence", "sources", "explanation", "ts"}
    assert "vitals" not in row          # ADR-0002: vitals never persisted
    assert row["sources"][0]["modality"] == "wifi_csi"   # JSON column round-trips
    st.close()


def test_transition_memory_is_per_room_independent():
    # Two rooms tracked independently: a flip in one must not suppress the other's
    # first sighting.
    st = Storage(":memory:")
    assert st.insert_if_transition(rs("sala", True, "t0")) is True
    assert st.insert_if_transition(rs("quarto", True, "t0")) is True   # different room -> baseline
    assert st.insert_if_transition(rs("sala", True, "t1")) is False    # sala steady
    assert st.insert_if_transition(rs("quarto", False, "t1")) is True  # quarto flips
    rows = st.recent()
    assert [(r["room"], r["occupied"]) for r in rows] == [
        ("sala", True), ("quarto", True), ("quarto", False),
    ]
    st.close()
