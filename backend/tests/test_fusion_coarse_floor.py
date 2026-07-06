"""Threat-model finding A1.3 — coarse-class occupancy floor.

A lone low-trust *coarse* source (a paired phone, or house-level network presence)
must NEVER be able to flip a room / the whole "casa" to occupied by itself, and that
guarantee must be STRUCTURAL — independent of any weight/threshold tuning. Coarse
sources vote presence but cannot localize a person; alone they may only RAISE
confidence, never CREATE occupancy. Occupancy requires >=1 LIVE non-coarse source.

The load-bearing proof is `test_lone_phone_cannot_flip_casa_under_hostile_retune`:
it drives confidence past the threshold on phone-only evidence (the old numeric
margin is deliberately broken) and asserts the room still does not occupy. That test
FAILS on the pre-A1.3 engine and PASSES with the class floor — the tuning-independence
proof. All clocks are injected; no real sleeps.
"""
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent
from wavr.fusion import DEFAULT_WEIGHTS, FusionEngine, _COARSE_MODALITIES

_BASE = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds):
    """ISO-8601 UTC timestamp `seconds` after (or before, if negative) the base."""
    return (_BASE + timedelta(seconds=seconds)).isoformat()


def _ev(room, modality, presence, conf, seconds=0):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf,
                        ts=_at(seconds))


# --- the class is exactly the coarse/house-level tier ------------------------------

def test_coarse_class_membership_is_exactly_phone_and_network():
    # Tripwire: a future house-level source that cannot localize a person MUST be
    # added here, and no room-localizing source may be.
    assert _COARSE_MODALITIES == frozenset({"phone", "network"})
    for room_localizing in ("camera", "wifi_csi", "mmwave", "ble", "sim"):
        assert room_localizing not in _COARSE_MODALITIES


# --- 1. lone phone, default tuning -------------------------------------------------

def test_lone_phone_cannot_flip_casa_default():
    f = FusionEngine()                                 # DEFAULT_WEIGHTS, threshold 0.5
    rs = f.update(_ev("casa", "phone", True, 0.8))
    assert rs.occupied is False
    assert rs.confidence == 0.4                        # 0.5 weight x 0.8 conf x 1.0 decay


# --- 2. LOAD-BEARING: lone phone under a hostile retune ----------------------------

def test_lone_phone_cannot_flip_casa_under_hostile_retune():
    # The numeric safety margin is deliberately BROKEN by the retune: phone-only
    # confidence (0.72) now clears the (lowered) threshold (0.35). On the pre-A1.3
    # engine this occupies the room. The structural class floor must still hold it
    # vacant — THIS is the tuning-independence proof.
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "phone": 0.9}, threshold=0.35)
    rs = f.update(_ev("casa", "phone", True, 0.8))
    assert rs.confidence == 0.72                       # 0.9 x 0.8
    assert rs.confidence >= f._threshold               # margin broken: would flip WITHOUT floor
    assert rs.occupied is False                        # floor holds it vacant anyway


# --- 3. the floor is on the CLASS, not any single modality -------------------------

def test_phone_plus_network_still_cannot_flip_casa():
    # Two coarse sources agreeing is still only coarse evidence. Even with both
    # retuned above the threshold, the whole coarse CLASS cannot create occupancy.
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "phone": 0.9, "network": 0.9},
                     threshold=0.35)
    f.update(_ev("casa", "phone", True, 0.8))
    rs = f.update(_ev("casa", "network", True, 0.8))
    assert rs.confidence >= f._threshold               # coarse mass clears the bar
    assert rs.occupied is False                        # ...but the class floor withholds it


def test_lone_network_under_retune_cannot_flip():
    # Same class guarantee via the OTHER coarse modality, on its own.
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "network": 0.9}, threshold=0.35)
    rs = f.update(_ev("casa", "network", True, 0.8))
    assert rs.confidence >= f._threshold
    assert rs.occupied is False


# --- 4. phone RAISES confidence, a camera CONFIRMS occupancy -----------------------

def test_phone_corroborates_but_camera_confirms():
    f = FusionEngine()
    cam = f.update(_ev("casa", "camera", True, 0.6))   # non-coarse -> occupies
    assert cam.occupied is True
    base_conf = cam.confidence                         # 0.6
    both = f.update(_ev("casa", "phone", True, 0.8))   # phone corroborates
    assert both.occupied is True                       # still occupied (camera confirms)
    assert both.confidence >= base_conf                # phone RAISES/holds, never lowers
    assert "só corroboração" not in both.explanation   # not a floored, coarse-only picture


# --- 5. phone going absent cannot un-occupy a camera-held room ---------------------

def test_phone_absence_cannot_flip_camera_occupied_room_to_empty():
    f = FusionEngine()
    f.update(_ev("casa", "camera", True, 0.9, seconds=0))
    occ = f.update(_ev("casa", "phone", True, 0.8, seconds=0))
    assert occ.occupied is True and occ.confidence == 0.9   # camera confirms; phone corroborates

    absent = f.update(_ev("casa", "phone", False, 0.0, seconds=1))
    assert absent.occupied is True                     # camera still holds it
    assert absent.confidence == 0.9                    # confidence unchanged by phone leaving

    # Phone stays fully silent while the camera keeps reporting -> still occupied.
    silent = f.update(_ev("casa", "camera", True, 0.9, seconds=200))
    assert silent.occupied is True
    assert silent.confidence == 0.9


# --- 6. the corroborator must be LIVE (mass > 0.0), not merely present -------------

def test_coarse_floor_needs_LIVE_noncoarse_corroborator():
    # A camera that is PRESENT but decayed past STALE_S contributes zero trust
    # (mass 0). Bare `e.presence` would wrongly count it as a corroborator and
    # license the lone phone; the `mass > 0.0` gate does NOT. Injected clock pins
    # "now" at _BASE so the ancient camera reading ages past STALE_S deterministically.
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "phone": 0.9}, threshold=0.35,
                     now_fn=lambda: _BASE)
    f.update(_ev("casa", "camera", True, 0.9, seconds=-200))   # present but long dead
    rs = f.update(_ev("casa", "phone", True, 0.8, seconds=0))  # fresh phone, retuned above bar
    assert rs.confidence >= f._threshold               # phone-only mass clears the threshold
    assert rs.occupied is False                        # decayed camera is NOT a live corroborator


# --- 7. documentation tripwire (safety no longer RESTS on this margin) -------------

def test_default_phone_margin_is_documentation_only():
    # Historically, safety rested on this numeric margin (0.5 x 0.8 = 0.4 < 0.5).
    # It is kept as a documentation tripwire ONLY -- the real, tuning-independent
    # guarantee is proved by tests 2 and 3 above, which hold even when this margin
    # is deliberately broken by a retune.
    assert DEFAULT_WEIGHTS["phone"] * 0.8 == 0.4
    assert DEFAULT_WEIGHTS["phone"] * 0.8 < 0.5


# --- explainability: the floor is surfaced, additively -----------------------------

def test_coarse_floor_marks_explanation_additively():
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "phone": 0.9}, threshold=0.35)
    rs = f.update(_ev("casa", "phone", True, 0.8))
    assert rs.occupied is False
    assert "phone" in rs.explanation                   # existing modality substring preserved
    assert "só corroboração (sem sensor de presença)" in rs.explanation
