"""Map-honesty invariant — locatable-modality allowlist (defense-in-depth).

The house map renders a precise body dot only from a room's LOCATED targets (finite
x/y in RoomState.targets). The invariant "a coarse signal must NEVER render as a
precise body" must be STRUCTURAL, not merely a property of today's ingestion boundary
(phone POST /api/telemetry already forbids targets). `FusionEngine` must therefore
draw a body ONLY from an allowlisted locatable modality; ANY other source carrying a
fabricated `targets:[{x,y}]` must produce NO located target -- while still casting its
occupancy vote exactly as before.

These tests construct SensingEvents WITH targets directly, bypassing the ingestion
boundary, to prove the fusion engine itself is fail-closed.
"""
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent, Target
from wavr.fusion import (DEFAULT_WEIGHTS, FusionEngine, _COARSE_MODALITIES,
                         _LOCATABLE_MODALITIES)

_TS = "2026-07-05T00:00:00+00:00"


def _ev(modality, presence=True, targets=(), conf=0.9, room="sala"):
    return SensingEvent(room=room, modality=modality, presence=presence,
                        motion=1.0, breathing_bpm=None, heart_bpm=None,
                        confidence=conf, ts=_TS, targets=targets)


def _target():
    return (Target(id=1, x=2.0, y=1.5, posture="standing",
                   velocity=0.0, confidence=0.9),)


# --- the allowlist is exactly the room-localizing tier -----------------------------

def test_locatable_allowlist_is_exactly_the_room_localizing_modalities():
    # Tripwire: a NEW locatable source MUST be added here to render bodies; a
    # coarse/house-level source may NOT be.
    assert _LOCATABLE_MODALITIES == frozenset({"camera", "mmwave", "wifi_csi", "sim"})


def test_allowlist_and_coarse_blocklist_are_disjoint():
    # A modality can never be both locatable and coarse. In particular `ble` is in
    # NEITHER set: it must be excluded from bodies by the allowlist (fail-closed),
    # NOT by the coarse blocklist -- which is exactly the gap a blocklist would leave.
    assert _LOCATABLE_MODALITIES.isdisjoint(_COARSE_MODALITIES)
    assert "ble" not in _LOCATABLE_MODALITIES
    assert "ble" not in _COARSE_MODALITIES


# --- 1. fabricated coarse-modality targets NEVER render as a body ------------------

def test_fabricated_phone_target_produces_no_body():
    f = FusionEngine()
    rs = f.update(_ev("phone", targets=_target(), room="casa"))
    assert rs.targets == []          # coarse phone can never draw a precise person


def test_fabricated_network_target_produces_no_body():
    f = FusionEngine()
    rs = f.update(_ev("network", targets=_target(), room="casa"))
    assert rs.targets == []


def test_fabricated_ble_target_produces_no_body():
    # ble is coarse but NOT in _COARSE_MODALITIES -- the exact gap a `not in
    # _COARSE_MODALITIES` blocklist would leave open. The allowlist closes it.
    f = FusionEngine()
    rs = f.update(_ev("ble", targets=_target()))
    assert rs.targets == []


# --- 2. legitimate locatable sources STILL render their body (no regression) -------

def test_locatable_modalities_still_render_their_body():
    for modality in ("camera", "mmwave", "wifi_csi", "sim"):
        f = FusionEngine()
        t = _target()
        rs = f.update(_ev(modality, targets=t))
        assert rs.targets == [t[0].to_dict()], f"{modality} body was wrongly dropped"


# --- 3. an UNKNOWN / future modality with a target is fail-closed ------------------

def test_unknown_future_modality_target_produces_no_body():
    # Not in the allowlist and not in the coarse blocklist -> no located body.
    f = FusionEngine()
    rs = f.update(_ev("some_future_radar_v9", targets=_target()))
    assert rs.targets == []


# --- 4. the occupancy vote of a coarse source is UNCHANGED (only body suppressed) --

def test_coarse_source_occupancy_vote_is_unchanged_only_body_is_suppressed():
    # A fabricated-target phone must still vote presence exactly as a plain phone:
    # same confidence, same coarse-floor behaviour. ONLY the body is withheld.
    f_fab = FusionEngine()
    fab = f_fab.update(_ev("phone", targets=_target(), conf=0.8, room="casa"))

    f_plain = FusionEngine()
    plain = f_plain.update(_ev("phone", targets=(), conf=0.8, room="casa"))

    assert fab.confidence == plain.confidence == 0.4   # 0.5 weight x 0.8 conf
    assert fab.occupied == plain.occupied is False     # coarse floor still holds
    assert fab.targets == plain.targets == []          # no body either way
    # The phone still appears as a presence source in the fused explanation.
    assert "phone" in fab.explanation


def test_coarse_source_still_raises_confidence_beside_a_camera_body():
    # A camera draws the body; a fabricated-target phone still corroborates presence
    # (raises/holds confidence) but adds NO second body and never overrides the
    # camera's located target.
    f = FusionEngine()
    t = _target()
    f.update(_ev("camera", targets=t, conf=0.9, room="casa"))
    rs = f.update(_ev("phone", targets=_target(), conf=0.8, room="casa"))
    assert rs.occupied is True
    assert rs.confidence == 0.9                         # camera confirms
    assert rs.targets == [t[0].to_dict()]              # only the camera body renders


# --- 5. a coarse fabricated target cannot out-weight a real locatable body ---------

def test_coarse_high_weight_cannot_steal_the_body_from_a_locatable_source():
    # Even if a coarse modality were retuned to a higher trust weight than the
    # camera, it must not win target selection -- the allowlist excludes it before
    # weight is ever compared.
    f = FusionEngine(weights={**DEFAULT_WEIGHTS, "network": 5.0})
    cam = _target()
    f.update(_ev("network", targets=_target(), conf=0.9, room="casa"))
    rs = f.update(_ev("camera", targets=cam, conf=0.9, room="casa"))
    assert rs.targets == [cam[0].to_dict()]            # camera body, not network's


# --- 6. freshness gate still applies WITHIN the allowlist (no regression) ----------

def test_dead_locatable_source_still_ghosts_no_body():
    # An allowlisted-but-dead camera must still be gated out by the freshness decay,
    # unchanged by the allowlist.
    T = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    f = FusionEngine(now_fn=lambda: T)
    dead_ts = (T - timedelta(seconds=200)).isoformat()  # > stale_s (90) -> dead
    dead = SensingEvent(room="sala", modality="camera", presence=True, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=0.9,
                        ts=dead_ts, targets=_target())
    rs = f.update(dead)
    assert rs.targets == []
