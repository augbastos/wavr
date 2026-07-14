"""Sensor-health / freshness-decay tests for the fusion CORE.

A stale or dead source must lose trust so `confidence = agreement × strength`
stays honest. The engine's clock is injected so ageing is deterministic.
"""

from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine, DEFAULT_WEIGHTS

# Fixed reference "now" — all ages are measured against this via the injected clock.
T = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _clock():
    return T


def ev(room, modality, presence, conf, age_s, br=None, hr=None):
    """Event whose ts is `age_s` seconds before the injected clock T."""
    ts = (T - timedelta(seconds=age_s)).isoformat()
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=br, heart_bpm=hr, confidence=conf, ts=ts)


def engine(**kw):
    # Defaults: freshness_s=30, stale_s=90.
    return FusionEngine(now_fn=_clock, **kw)


# ---- new BLE trust weight -------------------------------------------------------

def test_ble_weight_sits_between_wifi_csi_and_network():
    assert DEFAULT_WEIGHTS["ble"] == 0.7
    assert DEFAULT_WEIGHTS["network"] < DEFAULT_WEIGHTS["ble"] < DEFAULT_WEIGHTS["wifi_csi"]


# ---- fresh = no change (identity with today's math) -----------------------------

def test_fresh_source_is_identical_to_no_decay():
    fresh = engine().update(ev("sala", "wifi_csi", True, 0.9, age_s=0))
    # A plain engine (no clock) on the same fresh event: age 0 → decay 1.0.
    plain = FusionEngine().update(SensingEvent(
        room="sala", modality="wifi_csi", presence=True, motion=1.0,
        breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=T.isoformat()))
    assert fresh.confidence == plain.confidence == 0.765  # 0.85 * 0.9
    assert fresh.sources[0]["health"] == "fresh"
    assert fresh.sources[0]["age_s"] == 0


def test_within_freshness_window_keeps_full_weight():
    # 20 s < 30 s freshness window → still full trust, unchanged confidence.
    rs = engine().update(ev("sala", "wifi_csi", True, 0.9, age_s=20))
    assert rs.confidence == 0.765
    assert rs.sources[0]["health"] == "fresh"
    assert rs.sources[0]["age_s"] == 20


# ---- stale = linear decay, confidence drops -------------------------------------

def test_stale_source_decays_confidence():
    fresh = engine().update(ev("sala", "wifi_csi", True, 0.9, age_s=0))
    stale = engine().update(ev("sala", "wifi_csi", True, 0.9, age_s=60))
    # 60 s is halfway between 30 and 90 → 0.5 trust multiplier.
    expected = round(0.85 * 0.9 * 0.5, 3)
    assert stale.confidence == expected
    assert stale.confidence < fresh.confidence
    assert stale.sources[0]["health"] == "stale"
    assert stale.sources[0]["age_s"] == 60


# ---- dead = zero contribution ---------------------------------------------------

def test_dead_source_contributes_zero():
    rs = engine().update(ev("sala", "wifi_csi", True, 0.9, age_s=120))  # > 90 s
    assert rs.confidence == 0.0
    assert rs.occupied is False
    assert rs.sources[0]["health"] == "dead"
    assert rs.sources[0]["age_s"] == 120


def test_dead_source_drops_out_leaving_fresh_source_intact():
    # A dead (present, high-conf) network must add ~0 mass, so the fused result
    # equals the fresh camera alone — the dead source no longer freezes the room.
    f = engine()
    f.update(ev("sala", "network", True, 0.8, age_s=200))   # dead
    both = f.update(ev("sala", "camera", True, 0.95, age_s=0))  # fresh

    camera_only = engine().update(ev("quarto", "camera", True, 0.95, age_s=0))
    assert both.confidence == camera_only.confidence == 0.95
    net = next(s for s in both.sources if s["modality"] == "network")
    assert net["health"] == "dead"


# ---- health + age_s surface in RoomState.sources --------------------------------

def test_health_and_age_fields_present_across_states():
    f = engine()
    f.update(ev("sala", "camera", True, 0.9, age_s=0))     # fresh
    f.update(ev("sala", "wifi_csi", True, 0.9, age_s=60))  # stale
    rs = f.update(ev("sala", "network", True, 0.9, age_s=120))  # dead

    by_mod = {s["modality"]: s for s in rs.sources}
    assert by_mod["camera"]["health"] == "fresh" and by_mod["camera"]["age_s"] == 0
    assert by_mod["wifi_csi"]["health"] == "stale" and by_mod["wifi_csi"]["age_s"] == 60
    assert by_mod["network"]["health"] == "dead" and by_mod["network"]["age_s"] == 120
    # additive only — existing keys stay, plus the two new ones.
    assert set(by_mod["camera"]) == {"modality", "presence", "confidence", "age_s", "health", "count"}


def test_custom_windows_are_respected():
    # Tight window: anything past 10 s is already dead.
    rs = engine(freshness_s=5, stale_s=10).update(ev("sala", "camera", True, 0.9, age_s=12))
    assert rs.sources[0]["health"] == "dead"
    assert rs.confidence == 0.0
