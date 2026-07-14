# Precision / resolution ladder tests -- the SECOND axis, DISTINCT from confidence.
# Verifies: each rung earned from the right evidence, honest degrade when the
# precision-capable source is stale/off, confidence numerically unchanged by the
# new block, not-occupied -> none, and a FUSION-B-latched count keeping its rung.
from datetime import datetime, timezone

from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine


def ev(room, modality, presence, conf, count=None, targets=(),
       ts="2026-07-01T10:00:00+00:00"):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf, ts=ts,
                        targets=targets, count=count)


def tgt(x, y, conf):
    return Target(id=1, x=x, y=y, confidence=conf)


def test_house_rung_from_network_only():
    f = FusionEngine()
    rs = f.update(ev("casa", "network", True, 1.0))
    assert rs.occupied is True
    assert rs.confidence == 0.5           # agreement 1.0 * strength (0.5 weight * 1.0)
    assert rs.precision_level == "house"
    assert rs.precision_pct == 25
    assert rs.precision_next == "add_room_sensor"


def test_room_rung_from_ble_only():
    f = FusionEngine()
    rs = f.update(ev("sala", "ble", True, 1.0))
    assert rs.occupied is True
    assert rs.precision_level == "room"
    assert rs.precision_pct == 50
    assert rs.precision_next == "add_counting_sensor"
    assert rs.person_count is None        # presence-only source never vouches a number


def test_count_rung_from_mmwave_count_without_position():
    f = FusionEngine()
    rs = f.update(ev("sala", "mmwave", True, 1.0, count=2))
    assert rs.occupied is True
    assert rs.person_count == 2
    assert rs.precision_level == "count"
    assert rs.precision_pct == 75
    assert rs.precision_next == "calibrate_camera_position"


def test_position_rung_from_calibrated_camera_target():
    f = FusionEngine()
    rs = f.update(ev("sala", "camera", True, 0.9, count=1, targets=(tgt(1.0, 2.0, 0.85),)))
    assert rs.occupied is True
    assert rs.person_count == 1
    assert rs.precision_level == "position"
    assert rs.precision_pct == 100
    assert rs.precision_next is None


def test_monocular_camera_caps_at_count_not_position():
    # x/y present but per-target quality below _POSITION_QUALITY_MIN (monocular blob)
    # -> honestly caps at count, never the exact-position rung.
    f = FusionEngine()
    rs = f.update(ev("sala", "camera", True, 0.9, count=1, targets=(tgt(1.0, 2.0, 0.45),)))
    assert rs.person_count == 1
    assert rs.precision_level == "count"


def test_counting_source_without_a_number_caps_at_room():
    # mmwave present but count None (not vouching a headcount) -> honest cap at room.
    f = FusionEngine()
    rs = f.update(ev("sala", "mmwave", True, 1.0, count=None))
    assert rs.occupied is True
    assert rs.person_count is None
    assert rs.precision_level == "room"


def test_camera_boot_off_never_reaches_position():
    # Camera boot-OFF (never fed). Only mmwave counts -> ladder tops out at count.
    f = FusionEngine()
    rs = f.update(ev("sala", "mmwave", True, 1.0, count=1))
    assert rs.precision_level == "count"


def test_not_occupied_is_none():
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", False, 0.4))
    assert rs.occupied is False
    assert rs.precision_level == "none"
    assert rs.precision_pct == 0
    assert rs.precision_next is None


def test_ladder_recedes_to_none_when_only_source_goes_stale():
    clock = {"t": datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)}
    f = FusionEngine(vacate_s=0, now_fn=lambda: clock["t"])
    t0 = "2026-07-01T10:00:00+00:00"
    rs = f.update(ev("sala", "mmwave", True, 1.0, count=1, ts=t0))
    assert rs.precision_level == "count"
    clock["t"] = datetime(2026, 7, 1, 10, 5, 0, tzinfo=timezone.utc)  # +300s, > stale 90
    rs2 = f.state("sala")
    assert rs2.occupied is False
    assert rs2.precision_level == "none"
    assert rs2.person_count is None


def test_confidence_unchanged_by_precision_block():
    # The precision block is a pure read over already-fused values; confidence must
    # still equal agreement * strength regardless of which rung is reached.
    f = FusionEngine()
    rs = f.update(ev("casa", "network", True, 1.0))
    assert rs.confidence == 0.5           # lone 0.5-weight source, house rung
    f2 = FusionEngine()
    rs2 = f2.update(ev("sala", "camera", True, 0.9, count=1, targets=(tgt(1.0, 2.0, 0.85),)))
    assert rs2.confidence == 0.9          # 1.0 weight * 0.9, unaffected by the position rung
    assert rs2.precision_level == "position"


def test_fusion_b_latched_count_keeps_the_rung():
    # A still counting source single-frame drops (presence False) while occupied is
    # held by the vacate dwell; person_count stays latched (FUSION-B) and the ladder
    # must KEEP the count rung -- consistent with the count still surfaced.
    f = FusionEngine()
    ts = "2026-07-01T10:00:00+00:00"
    rs1 = f.update(ev("sala", "mmwave", True, 1.0, count=1, ts=ts))
    assert rs1.precision_level == "count" and rs1.person_count == 1
    rs2 = f.update(ev("sala", "mmwave", False, 0.0, count=None, ts=ts))
    assert rs2.occupied is True           # held by the vacate dwell
    assert rs2.person_count == 1          # FUSION-B latch
    assert rs2.confidence == 0.0          # honest: low confidence, count still held
    assert rs2.precision_level == "count"  # rung kept via the latch, not dropped to none
