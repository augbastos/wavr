from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.rules import RulesEngine


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


def test_negative_source_confidence_is_clamped_in_fused_result():
    # A source reporting a negative confidence must not drive the fused
    # confidence negative (e.g. an explanation of "-64% ocupado").
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", True, -3.0))
    assert 0.0 <= rs.confidence <= 1.0


def test_overlarge_source_confidence_is_clamped_in_fused_result():
    # A source reporting confidence far above 1.0 must not drive the fused
    # confidence above 1.0.
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", True, 999.0))
    assert 0.0 <= rs.confidence <= 1.0


def test_malformed_timestamp_does_not_cascade_and_kill_later_good_events():
    # update() must not store an event with an unparseable ts — otherwise
    # every later fuse touching the room (from other, healthy modalities)
    # would raise on that poisoned slot.
    f = FusionEngine()
    bad = SensingEvent(room="sala", modality="network", presence=True, motion=0.0,
                       breathing_bpm=None, heart_bpm=None, confidence=0.6,
                       ts="not-a-timestamp")
    f.update(bad)  # must not raise
    rs = f.update(ev("sala", "camera", True, 0.9))  # later good event, another modality
    assert rs.occupied is True
    assert rs.sources == [{"modality": "camera", "presence": True,
                           "confidence": 0.9, "age_s": 0, "health": "fresh"}]


def test_none_timestamp_does_not_cascade_and_kill_later_good_events():
    f = FusionEngine()
    bad = SensingEvent(room="sala", modality="network", presence=True, motion=0.0,
                       breathing_bpm=None, heart_bpm=None, confidence=0.6, ts=None)
    f.update(bad)  # must not raise
    rs = f.update(ev("sala", "camera", True, 0.9))
    assert rs.occupied is True


# ---------------------------------------------------------------------------
# Occupancy dwell / hysteresis (smartthings.md #1 -- asymmetric wall-clock dwell)
# Fast to occupied, slow/debounced to vacant. Only the boolean is debounced;
# confidence stays continuous and the pending exit is surfaced in `explanation`.
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds):
    """ISO-8601 UTC timestamp `seconds` after the fixed base."""
    return (_BASE + timedelta(seconds=seconds)).isoformat()


def _cam(room, presence, conf, seconds):
    # weight camera=1.0 so fused confidence ~= the source confidence (agreement
    # is 1.0 for a lone source), making the threshold crossings easy to script.
    return SensingEvent(room=room, modality="camera", presence=presence, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf,
                        ts=_at(seconds))


def _lone():
    # Lone-camera engine: with weight 1.0 a present source's fused confidence
    # equals its own confidence, so 0.6 -> occupied, 0.3 -> below threshold.
    return FusionEngine(weights={"camera": 1.0})


def test_occupied_is_immediate_no_dwell_on_the_way_up():
    # Fast to occupied: the very first above-threshold reading flips occupied now.
    f = _lone()
    rs = f.update(_cam("sala", True, 0.6, 0))
    assert rs.occupied is True
    assert "confirmando" not in rs.explanation


def test_single_frame_dip_does_not_flip_room_vacant():
    # A one-frame confidence dip (0.6 -> 0.3 -> 0.6) must NOT flip the room vacant:
    # the dwell holds occupied through the dip. Confidence stays honest (drops).
    f = _lone()
    up = f.update(_cam("sala", True, 0.6, 0))
    dip = f.update(_cam("sala", True, 0.3, 1))     # below threshold, but held
    back = f.update(_cam("sala", True, 0.6, 2))

    assert up.occupied is True
    assert dip.occupied is True                    # HELD, not flipped
    assert dip.confidence < 0.5                     # confidence NOT debounced -- honest
    assert back.occupied is True
    assert "confirmando" not in back.explanation    # pending cancelled on re-cross


def test_pending_vacate_is_surfaced_in_the_explanation():
    # Honesty invariant: while held, the explanation shows the low confidence AND
    # the confirming-exit countdown -- uncertainty is shown, not hidden.
    f = _lone()
    f.update(_cam("sala", True, 0.6, 0))
    held = f.update(_cam("sala", False, 0.0, 1))
    assert held.occupied is True
    assert "ocupado" in held.explanation
    assert "confirmando" in held.explanation


def test_vacate_grace_expires_flips_to_vacant():
    # Once confidence has STAYED below threshold for the full wall-clock grace,
    # occupied finally flips to vacant (default WAVR_ROOM_VACATE_S = 45 s).
    f = _lone()
    f.update(_cam("sala", True, 0.9, 0))
    held = f.update(_cam("sala", False, 0.0, 10))   # 10 s < 45 s -> still held
    gone = f.update(_cam("sala", False, 0.0, 60))   # 60 s since drop -> vacant
    assert held.occupied is True
    assert gone.occupied is False
    assert "confirmando" not in gone.explanation


def test_reoccupy_during_grace_resets_the_dwell():
    # A re-cross above threshold during the grace cancels the pending vacate, so
    # a later drop must serve the FULL grace again (not the leftover from before).
    f = _lone()
    f.update(_cam("sala", True, 0.9, 0))
    f.update(_cam("sala", False, 0.0, 30))          # 30 s into a 45 s grace
    f.update(_cam("sala", True, 0.9, 31))           # re-occupied -> cancel pending
    # Drop again; only 20 s later -> must still be held (grace restarted at 31 s).
    still = f.update(_cam("sala", False, 0.0, 51))
    assert still.occupied is True
    assert "confirmando" in still.explanation


def test_vacate_s_zero_disables_the_dwell():
    # Opt-out: WAVR_ROOM_VACATE_S=0 restores the pre-dwell raw threshold crossing.
    f = FusionEngine(weights={"camera": 1.0}, vacate_s=0)
    f.update(_cam("sala", True, 0.9, 0))
    gone = f.update(_cam("sala", False, 0.0, 1))
    assert gone.occupied is False
    assert "confirmando" not in gone.explanation


def test_single_frame_dip_emits_no_vacant_mqtt_edge():
    # End-to-end (trap #1): the dwell must stop a one-frame dip from firing a
    # `vacant` edge event downstream (which would kill a real occupant's lights).
    # Drive the fused RoomStates through RulesEngine and assert no vacant edge.
    msgs = []
    rules = RulesEngine(lambda t, p, r: msgs.append((t, p)))
    f = _lone()
    for i, (pres, conf) in enumerate([(True, 0.6), (True, 0.3), (True, 0.6)]):
        rules.handle(f.update(_cam("sala", pres, conf, i)).to_dict())
    edge_events = [p for t, p in msgs if t.endswith("/event")]
    assert "vacant" not in edge_events


def test_dwell_is_per_room_independent():
    # Two rooms debounce independently -- one going vacant must not disturb the
    # other holding occupied (the transition state is keyed per room).
    f = _lone()
    f.update(_cam("sala", True, 0.9, 0))
    f.update(_cam("quarto", True, 0.9, 0))
    f.update(_cam("sala", False, 0.0, 1))           # sala drops (held)
    quarto = f.update(_cam("quarto", True, 0.9, 2))  # quarto still present
    sala = f.state("sala")
    assert quarto.occupied is True and "confirmando" not in quarto.explanation
    assert sala.occupied is True and "confirmando" in sala.explanation


# ---------------------------------------------------------------------------
# Wall-clock ageing (fake-presence-on-disconnect fix). With an injected wall
# clock, a source that STOPS reporting decays to zero and the room fades to
# unoccupied via the periodic re-fuse tick -- instead of freezing its last
# reading forever (the "occupied 82%" ghost of an unplugged camera).
# ---------------------------------------------------------------------------

def test_single_source_decays_to_unoccupied_with_wallclock():
    # A lone camera reports occupied ~82%, then goes dark (no more events). Re-fusing
    # against an advancing wall clock must DECAY its confidence to 0 (not freeze it)
    # and, once the vacate dwell elapses, flip the room to unoccupied.
    clock = {"t": _BASE}
    f = FusionEngine(weights={"camera": 1.0}, now_fn=lambda: clock["t"])
    up = f.update(_cam("sala", True, 0.82, 0))
    assert up.occupied is True and up.confidence >= 0.5   # live: occupied

    # Camera unplugged: no new events, only the clock advances. First stale re-fuse
    # (age 300s > stale_s=90) drives confidence to 0 immediately -- the frozen
    # reading is GONE -- and starts the vacate dwell.
    clock["t"] = _BASE + timedelta(seconds=300)
    held = f.state("sala")
    assert held.confidence == 0.0                          # NOT frozen at 0.82
    assert held.occupied is True                           # dwell still holding

    # A later re-fuse, past the vacate window, confirms the room vacant.
    clock["t"] = _BASE + timedelta(seconds=400)
    gone = f.state("sala")
    assert gone.confidence == 0.0
    assert gone.occupied is False                          # honestly unoccupied


def test_fresh_source_value_unchanged_by_now_fn():
    # No-regression: while a source is fresh (age <= freshness_s) the wall clock
    # must not change the fused value at all -- byte-identical to the now_fn=None
    # baseline. The tick can only fade a DEAD source, never alter a live one.
    baseline = FusionEngine(weights={"camera": 1.0})       # now_fn=None
    b = baseline.update(_cam("sala", True, 0.82, 0))
    clock = {"t": _BASE + timedelta(seconds=5)}            # 5s <= freshness_s(30)
    f = FusionEngine(weights={"camera": 1.0}, now_fn=lambda: clock["t"])
    r = f.update(_cam("sala", True, 0.82, 0))
    assert r.confidence == b.confidence
    assert r.occupied == b.occupied


def test_rooms_getter_lists_known_rooms():
    f = FusionEngine()
    assert f.rooms() == []
    f.update(ev("sala", "camera", True, 0.9))
    f.update(ev("quarto", "network", False, 0.4))
    assert set(f.rooms()) == {"sala", "quarto"}
