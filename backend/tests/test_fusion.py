from datetime import datetime, timedelta, timezone

from wavr.events import Identity, SensingEvent, Target
from wavr.fusion import FusionEngine, house_person_count
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
                           "confidence": 0.9, "age_s": 0, "health": "fresh",
                           "count": None}]


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


# --- Identity ("who is home") pass-through -----------------------------------------

def _ident_ev(room, modality, presence, conf, identities=()):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=0.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf,
                        ts="2026-07-01T10:00:00+00:00", identities=identities)


def test_identity_surfaces_from_present_fresh_event():
    f = FusionEngine()
    rs = f.update(_ident_ev("casa", "ble", True, 0.7,
                            (Identity("alice", "ble", -55),)))
    assert rs.identities == [{"person": "alice", "source": "ble", "rssi": -55}]


def test_identity_dropped_when_source_is_dead_stale():
    # Aged well past stale_s -> decay 0 -> the identity is dropped exactly like a
    # dead source's targets (present flag alone must not leak a stale name).
    base = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    f = FusionEngine(now_fn=lambda: base + timedelta(seconds=1000))
    rs = f.update(_ident_ev("casa", "ble", True, 0.7,
                            (Identity("alice", "ble", -55),)))
    assert rs.identities == []


def test_identity_deduped_by_person_keeps_stronger_rssi():
    f = FusionEngine()
    f.update(_ident_ev("casa", "network", True, 0.8,
                       (Identity("alice", "network", None),)))
    rs = f.update(_ident_ev("casa", "ble", True, 0.7,
                            (Identity("alice", "ble", -55),)))
    # One person -> one entry; the ble entry with a real rssi wins over rssi=None.
    assert rs.identities == [{"person": "alice", "source": "ble", "rssi": -55}]


def test_confidence_byte_identical_with_and_without_identities():
    # The core invariant: attaching identities must not move the fused confidence
    # by a single ulp. Same events, one carrying an Identity, one not.
    plain = FusionEngine()
    p = plain.update(_ident_ev("casa", "ble", True, 0.7))
    withid = FusionEngine()
    w = withid.update(_ident_ev("casa", "ble", True, 0.7,
                                (Identity("alice", "ble", -55),)))
    assert w.confidence == p.confidence
    assert w.occupied == p.occupied
    assert w.sources == p.sources


# ---- A1: first-class person_count -----------------------------------------------

def _camc(room, present, conf, count, ts="2026-07-01T10:00:00+00:00"):
    return SensingEvent(room=room, modality="camera", presence=present, motion=0.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf, ts=ts,
                        count=count)


def _mmc(room, targets, ts="2026-07-01T10:00:00+00:00"):
    tg = tuple(Target(id=i + 1, x=float(i), y=0.0) for i in range(targets))
    return SensingEvent(room=room, modality="mmwave", presence=bool(tg), motion=0.0,
                        breathing_bpm=None, heart_bpm=None,
                        confidence=0.9 if tg else 0.0, ts=ts, targets=tg,
                        count=len(tg))


def _pir(room, present, ts="2026-07-01T10:00:00+00:00"):
    """Presence-only node (not in COUNTING_MODALITIES) -- knows "someone", never "how many"."""
    return SensingEvent(room=room, modality="pir", presence=present, motion=1.0,
                        breathing_bpm=None, heart_bpm=None,
                        confidence=0.6 if present else 0.0, ts=ts)


def test_camera_count_flows_to_person_count():
    f = FusionEngine()
    rs = f.update(_camc("sala", True, 0.9, 3))
    assert rs.person_count == 3
    src = [s for s in rs.sources if s["modality"] == "camera"][0]
    assert src["count"] == 3


def test_mmwave_count_is_target_len():
    f = FusionEngine()
    rs = f.update(_mmc("sala", 2))
    assert rs.person_count == 2
    src = [s for s in rs.sources if s["modality"] == "mmwave"][0]
    assert src["count"] == 2


def test_count_disagreement_prefers_highest_weight_source():
    # camera (weight 1.0) says 2, mmwave (0.9) says 1 -> deterministic precedence: camera.
    f = FusionEngine()
    f.update(_mmc("sala", 1))
    rs = f.update(_camc("sala", True, 0.9, 2))
    assert rs.person_count == 2
    counts = {s["modality"]: s["count"] for s in rs.sources}
    assert counts["camera"] == 2 and counts["mmwave"] == 1   # both surfaced, conflict visible


def test_presence_only_source_leaves_count_unknown():
    # network is presence-only: it must never assert a number.
    f = FusionEngine()
    rs = f.update(ev("casa", "network", True, 0.6))
    assert rs.person_count is None
    assert rs.sources[0]["count"] is None
    # known-presence invariant #1: a network-only casa event never FABRICATES a headcount
    # (network isn't in COUNTING_MODALITIES) -- person_count stays None above. It DOES, per
    # FUSION-C, pull `occupied` True from the honest presence signal even though the blended
    # confidence (weight 0.5 * confidence 0.6 = 0.30) sits below the 0.50 threshold: "someone
    # is here" is exactly what a presence-only source knows. `confidence` stays the honest low
    # value; only the boolean flips (same shape as FUSION-A's count-pull + the vacate-dwell).
    assert rs.occupied is True
    assert rs.confidence == 0.3


def test_fusion_c_presence_only_below_threshold_still_occupied():
    # FUSION-C / empty-home fix: the privacy-default "Presence" tier (network + Bluetooth,
    # cameras OFF) blends BELOW the occupancy threshold (network 0.5*0.8 = 0.40, BLE
    # 0.7*0.7 = 0.49, both < 0.50), so before this fix a camera-less home could NEVER read
    # occupied -- a person's own phone/laptop on the LAN showed "Empty home" forever. A
    # fresh, PRESENT presence-only source now pulls occupied True while keeping the honest
    # sub-threshold confidence and never fabricating a headcount.
    for modality, conf in [("network", 0.8), ("ble", 0.7), ("pir", 0.6), ("wifi_csi", 0.5)]:
        f = FusionEngine()
        rs = f.update(ev("casa", modality, True, conf))
        assert rs.occupied is True, f"{modality} present should pull occupied True"
        assert rs.confidence < f._threshold, f"{modality} confidence should stay honest sub-threshold"
        assert rs.person_count is None, f"{modality} must never fabricate a headcount"


def test_fusion_c_absent_presence_only_is_not_occupied():
    # The pull is PRESENCE-gated: a presence-only source reporting present=False (nobody
    # seen) must NOT pull occupied -- the house reads empty when everyone's devices leave.
    f = FusionEngine()
    rs = f.update(ev("casa", "network", False, 0.0))
    assert rs.occupied is False


def test_wifi_csi_with_targets_is_not_counted():
    # wifi_csi is NOT counting-capable even if it carries targets/count -- honesty gate.
    f = FusionEngine()
    e = SensingEvent(room="sala", modality="wifi_csi", presence=True, motion=1.0,
                     breathing_bpm=None, heart_bpm=None, confidence=0.9,
                     ts="2026-07-01T10:00:00+00:00",
                     targets=(Target(id=1, x=1.0, y=1.0),), count=1)
    rs = f.update(e)
    assert rs.person_count is None
    assert rs.sources[0]["count"] is None


def test_stale_counting_source_drops_person_count():
    # A dead (decayed-to-zero) camera must not keep vouching for a count.
    later = lambda: datetime(2026, 7, 1, 10, 2, 0, tzinfo=timezone.utc)  # +120s > stale 90s
    f = FusionEngine(now_fn=later)
    rs = f.update(_camc("sala", True, 0.9, 2))
    assert rs.sources[0]["health"] == "dead"
    assert rs.person_count is None


def test_vacant_counting_source_yields_no_count():
    f = FusionEngine()
    rs = f.update(_camc("sala", False, 0.0, 0))   # camera present=False
    assert rs.occupied is False
    assert rs.person_count is None


def test_person_count_does_not_move_confidence():
    with_count = FusionEngine().update(_camc("x", True, 0.9, 4)).confidence
    e = SensingEvent(room="x", modality="camera", presence=True, motion=0.0,
                     breathing_bpm=None, heart_bpm=None, confidence=0.9,
                     ts="2026-07-01T10:00:00+00:00")   # count defaults None
    no_count = FusionEngine().update(e).confidence
    assert with_count == no_count


def test_person_count_in_to_dict():
    rs = FusionEngine().update(_camc("sala", True, 0.9, 1))
    assert rs.to_dict()["person_count"] == 1


def test_house_person_count_sums_known_rooms():
    a = FusionEngine().update(_camc("sala", True, 0.9, 2)).to_dict()
    b = FusionEngine().update(_mmc("quarto", 1)).to_dict()
    c = FusionEngine().update(ev("casa", "network", True, 0.6)).to_dict()   # unknown
    assert house_person_count([a, b, c]) == 3


def test_house_person_count_none_when_all_unknown():
    c = FusionEngine().update(ev("casa", "network", True, 0.6)).to_dict()
    assert house_person_count([c]) is None
    assert house_person_count([]) is None


# ---------------------------------------------------------------------------
# FUSION-A: a fresh, PRESENT counting source (camera/mmwave) asserting count>=1
# pulls `occupied` True even when blended confidence sits below threshold --
# eliminates the incoherent `occupied=False ∧ person_count>0` state, which also
# blinds watch.py's room_unrecognized/house_unrecognized (they read person_count
# regardless of `occupied`).
# ---------------------------------------------------------------------------

def test_present_counting_source_pulls_occupied_when_confidence_low():
    f = FusionEngine(weights={"camera": 1.0})
    rs = f.update(_camc("sala", True, 0.3, 2))
    assert rs.occupied is True
    assert rs.person_count == 2
    assert rs.confidence < 0.5                     # honest: confidence itself untouched
    assert not (rs.occupied is False and (rs.person_count or 0) > 0)   # the invariant


def test_zero_count_present_source_does_not_pull_occupied():
    f = FusionEngine(weights={"camera": 1.0})
    rs = f.update(_camc("sala", True, 0.3, 0))
    assert rs.occupied is False
    assert rs.person_count is None


# ---------------------------------------------------------------------------
# FUSION-B: latch person_count/targets across a single/multi-frame presence
# dropout of a STILL counting source while `occupied` is held by the vacate
# dwell -- stops person_count flickering N -> None -> N (and the occupancy_log
# churn that flicker would cause, since person_count is an exact-match field
# there). Bounded by `self._stale_s` so a long-dead counting source can never
# keep asserting a headcount off a presence-only source holding the room.
# ---------------------------------------------------------------------------

def test_still_person_count_latched_across_single_frame_dropout():
    f = FusionEngine()
    f.update(_mmc("sala", 1, ts=_at(0)))
    held = f.update(_mmc("sala", 0, ts=_at(1)))     # single-frame dropout, held by dwell
    assert held.occupied is True
    assert held.person_count == 1
    assert "contagem mantida" in held.explanation

    back = f.update(_mmc("sala", 1, ts=_at(2)))     # source reports again -> latch refreshed
    assert back.person_count == 1
    assert "contagem mantida" not in back.explanation


def test_camera_live_negative_releases_latch_despite_presence_only_holding_occupied():
    # FUSION-C REGRESSION (found + fixed 2026-07-15). FUSION-C holds `occupied` True off ANY
    # present presence-only source -- which silently removed the `not occupied` branch that was
    # the count latch's only everyday release. A camera that is FRESH and reporting empty is
    # SKIPPED by the live_count loop (`if not (e.presence and decay>0): continue`), so it lands
    # in the latch branch looking exactly like a dropout, and kept vouching for the stale
    # headcount -- WITH the stale exact targets, which the precision ladder then promotes back
    # to position/100% -- for the whole stale_s window. Phantom people on the map of a room the
    # camera says is empty, and (via watch.room_unrecognized reading person_count) a false
    # intrusion alert with nobody home.
    f = FusionEngine()
    f.update(_pir("sala", True, ts=_at(0)))
    seen = f.update(_camc("sala", True, 0.9, 2, ts=_at(0)))
    assert seen.person_count == 2                      # camera counted 2 -> latched

    # Both people leave. The camera is AWAKE and says empty; the PIR false-trips and holds
    # present, so FUSION-C keeps the room occupied for the whole window.
    for t in (5, 30, 60, 80):
        f.update(_pir("sala", True, ts=_at(t)))
        rs = f.update(_camc("sala", False, 0.0, 0, ts=_at(t)))
        assert rs.occupied is True, f"t={t}: the PIR honestly vouches that someone is home"
        assert rs.person_count is None, f"t={t}: PHANTOM -- the camera is awake saying empty"
        assert rs.targets == [], f"t={t}: phantom targets would draw markers at exact coords"
        assert "contagem mantida" not in rs.explanation, f"t={t}: latch must not be held"
        assert rs.precision_level != "position", f"t={t}: stale targets must not earn position"


def test_trusted_absence_is_a_subset_of_counting():
    # Drift guard. TRUSTED_ABSENCE_MODALITIES only means anything for sources that VOUCH for a
    # count -- a presence-only source has no latch to release. More importantly this is the 4th
    # modality-keyed set in fusion.py and the "keep in sync" comments are convention, not
    # enforcement: a new counting modality inherits absence-DISTRUST by default, i.e. it fails
    # toward the phantom rather than toward honesty. If this fails, someone added a modality
    # without deciding its absence semantics on purpose.
    from wavr.fusion import COUNTING_MODALITIES, TRUSTED_ABSENCE_MODALITIES
    assert TRUSTED_ABSENCE_MODALITIES <= COUNTING_MODALITIES
    assert "mmwave" not in TRUSTED_ABSENCE_MODALITIES, (
        "mmwave infers presence from motion -- a still person vanishes from it, which is the "
        "dropout FUSION-B exists to bridge. Trusting its absence re-introduces the count flicker."
    )


def test_mmwave_live_negative_still_latches_a_still_person():
    # The CONTROL for the fix above, and the reason it is modality-gated. A fresh mmwave
    # presence=False is NOT "the room is empty" -- radar infers presence from motion, so a very
    # still person VANISHES from it. That is precisely the dropout FUSION-B exists to bridge.
    # Only TRUSTED_ABSENCE_MODALITIES (camera: it SEES the room, a still person stays visible)
    # may release the latch. If this ever fails, the count flicker None->N->None is back.
    f = FusionEngine()
    f.update(_mmc("sala", 1, ts=_at(0)))
    held = f.update(_mmc("sala", 0, ts=_at(1)))    # fresh mmwave, presence=False = still person
    assert held.occupied is True
    assert held.person_count == 1, "a still person must not un-count themselves on mmwave"
    assert "contagem mantida" in held.explanation


def test_latch_cleared_when_room_confirmed_vacant():
    f = FusionEngine(weights={"camera": 1.0})
    f.update(_camc("sala", True, 0.9, 2, ts=_at(0)))
    f.update(_camc("sala", False, 0.0, 0, ts=_at(1)))            # vacate dwell starts, held
    gone = f.update(_camc("sala", False, 0.0, 0, ts=_at(60)))    # past vacate_s(45) -> vacant
    assert gone.occupied is False
    assert gone.person_count is None


def test_held_count_expires_after_stale_window():
    # A room held occupied by a FRESH presence-only source (wifi_csi) must not go on
    # asserting a person_count off a counting source (camera) that died long ago -- the
    # latch's staleness bound (self._stale_s) stops it overclaiming a headcount
    # indefinitely off a dead counting source.
    clock = {"t": _BASE}
    f = FusionEngine(now_fn=lambda: clock["t"])
    f.update(_camc("sala", True, 0.9, 2, ts=_at(0)))             # camera counts 2, occupied True
    clock["t"] = _BASE + timedelta(seconds=100)                  # camera now DEAD (> stale_s=90)
    csi = SensingEvent(room="sala", modality="wifi_csi", presence=True, motion=0.0,
                       breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=_at(100))
    rs = f.update(csi)                                            # held occupied by wifi_csi alone
    assert rs.occupied is True
    assert rs.person_count is None


def _csi(room, ts):
    return SensingEvent(room=room, modality="wifi_csi", presence=True, motion=0.0,
                        breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=ts)


def test_latch_bound_measures_from_the_count_not_from_the_last_fuse():
    # The latch docstring promises the bound is stale_s. It stamped with `ref` (the fusion
    # time), but a counting source may be up to stale_s old and STILL vouch (decay>0) -- so
    # every fuse that saw the same aging count event re-dated the latch. The bound then
    # measured from the last FUSE instead of from the last real COUNT, and a dead camera plus
    # a chatty presence-only source kept vouching for a headcount for ~2x stale_s.
    # test_held_count_expires_after_stale_window cannot catch this: it JUMPS the clock 0->100
    # with no intermediate fuse. The bug only appears when you fuse THROUGH the window.
    clock = {"t": _BASE}
    f = FusionEngine(now_fn=lambda: clock["t"])
    f.update(_camc("sala", True, 0.9, 2, ts=_at(0)))    # the ONLY real count, at t=0

    # A chatty presence-only source fuses repeatedly INSIDE the window. Each of these fuses
    # still sees the t=0 camera event at decay>0 -- exactly what used to re-date the latch.
    for t in range(10, 90, 10):
        clock["t"] = _BASE + timedelta(seconds=t)
        held = f.update(_csi("sala", ts=_at(t)))
        assert held.person_count == 2, f"t={t}: a camera inside stale_s still legitimately vouches"

    # Past stale_s measured from the COUNT (t=0) the headcount must be gone, no matter how
    # many fuses happened in between.
    clock["t"] = _BASE + timedelta(seconds=95)
    rs = f.update(_csi("sala", ts=_at(95)))
    assert rs.occupied is True, "wifi_csi honestly still holds the room occupied"
    assert rs.person_count is None, (
        "the latch must expire stale_s after the COUNT, not after the last fuse -- "
        "stamping with `ref` let a dead counting source vouch for ~2x stale_s"
    )


def test_count_latch_is_per_room_never_leaks_across_rooms():
    # The latch dict is keyed by room -- a count held for "sala" must never bleed
    # into "quarto" just because they're fused by the same engine instance.
    f = FusionEngine()
    f.update(_mmc("sala", 3, ts=_at(0)))          # sala latches count=3
    rs_quarto = f.update(_mmc("quarto", 0, ts=_at(1)))   # quarto: no counting source ever present
    assert rs_quarto.occupied is False
    assert rs_quarto.person_count is None


def test_latch_does_not_resurrect_a_stale_count_after_a_genuine_vacate_and_reoccupy():
    # Once a room is confirmed vacant the latch is cleared (test_latch_cleared_when_
    # confirmed_vacant above). If the room later becomes occupied again through a
    # PRESENCE-ONLY source (no counting source has fired this cycle), person_count
    # must stay None -- not resurrect the OLD, now-stale latched number from before
    # the vacate.
    f = FusionEngine(weights={"camera": 1.0, "wifi_csi": 0.85})
    f.update(_camc("sala", True, 0.9, 2, ts=_at(0)))              # occupied, count 2, latched
    f.update(_camc("sala", False, 0.0, 0, ts=_at(1)))             # dwell starts
    gone = f.update(_camc("sala", False, 0.0, 0, ts=_at(60)))     # past vacate_s -> confirmed vacant
    assert gone.occupied is False and gone.person_count is None

    csi = SensingEvent(room="sala", modality="wifi_csi", presence=True, motion=0.0,
                       breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=_at(61))
    back = f.update(csi)                                           # occupied again, no counting source
    assert back.occupied is True
    assert back.person_count is None                               # honest "unknown", NOT the old 2


# ---------------------------------------------------------------------------
# FUSION-A consistency invariant, swept across many source combinations:
# `occupied=False` and a POSITIVE `person_count` must be structurally impossible.
# The implementation actually guarantees something even stronger -- person_count
# is ALWAYS None while vacant -- both are asserted below.
# ---------------------------------------------------------------------------

def _assert_fusion_a_invariant(rs):
    assert not (rs.occupied is False and rs.person_count is not None and rs.person_count > 0), (
        f"FUSION-A invariant broken: {rs.to_dict()}")
    if rs.occupied is False:
        assert rs.person_count is None, f"vacant room leaked a person_count: {rs.to_dict()}"


def test_fusion_a_invariant_swept_across_camera_and_network_combinations():
    import itertools

    presences = (True, False)
    confidences = (0.0, 0.15, 0.4, 0.6, 0.9, 1.0)   # spans below/at/above the 0.5 threshold
    counts = (0, 1, 2, 5)
    net_presences = (True, False)
    checked = 0
    for cam_p, cam_c, cam_n, net_p in itertools.product(presences, confidences, counts, net_presences):
        f = FusionEngine()
        f.update(ev("sala", "network", net_p, 0.6))       # presence-only, never counts
        rs = f.update(_camc("sala", cam_p, cam_c, cam_n))
        checked += 1
        _assert_fusion_a_invariant(rs)
    assert checked == len(presences) * len(confidences) * len(counts) * len(net_presences)


def test_fusion_a_invariant_swept_with_disagreeing_camera_and_mmwave():
    import itertools

    cam_presences = (True, False)
    cam_counts = (0, 1, 3)
    mm_targets = (0, 1, 2)
    checked = 0
    for cam_p, cam_n, mm_n in itertools.product(cam_presences, cam_counts, mm_targets):
        f = FusionEngine()
        f.update(_mmc("sala", mm_n, ts=_at(0)))
        rs = f.update(_camc("sala", cam_p, 0.3, cam_n, ts=_at(0)))  # low confidence on purpose
        checked += 1
        _assert_fusion_a_invariant(rs)
    assert checked == len(cam_presences) * len(cam_counts) * len(mm_targets)


def test_fusion_a_invariant_holds_through_a_stateful_timeline_with_dwell_and_latch():
    # Single-shot sweeps above isolate each fuse; this drives ONE engine through a
    # realistic multi-step timeline -- occupied held past a confidence dip (dwell),
    # count held past a presence dropout (latch), a low-confidence FUSION-A
    # reassertion, then a genuine vacate -- asserting the invariant at EVERY step,
    # not just the final state.
    clock = {"t": _BASE}
    f = FusionEngine(now_fn=lambda: clock["t"])
    steps = [
        (0, _camc("sala", True, 0.9, 2, ts=_at(0))),     # occupied, count 2
        (1, _mmc("sala", 2, ts=_at(1))),                  # mmwave agrees
        (2, _camc("sala", False, 0.0, 0, ts=_at(2))),    # camera single-frame dropout
        (3, _mmc("sala", 0, ts=_at(3))),                  # mmwave also drops -- dwell holds
        (4, _camc("sala", True, 0.3, 1, ts=_at(4))),     # low-confidence reassertion (FUSION-A)
        (50, _camc("sala", False, 0.0, 0, ts=_at(50))),  # camera gone
        (50, _mmc("sala", 0, ts=_at(50))),                 # mmwave gone too
    ]
    for secs, event in steps:
        clock["t"] = _BASE + timedelta(seconds=secs)
        rs = f.update(event)
        _assert_fusion_a_invariant(rs)

    clock["t"] = _BASE + timedelta(seconds=200)          # past vacate_s AND stale_s
    rs = f.state("sala")
    _assert_fusion_a_invariant(rs)
    assert rs.occupied is False and rs.person_count is None
