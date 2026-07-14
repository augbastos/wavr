"""Unit tests for the sensor-node registry, enrollment, kill-switch state machine,
and node -> SensingEvent translation (wavr.nodes). No FastAPI, no create_app --
these exercise the pure store/enroller/translation layer directly."""
from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta, timezone

import pytest

from wavr.nodes import (
    REACTIVATE_MAX_ATTEMPTS, STATE_ACTIVE, STATE_DISABLED, STATE_REVOKED, Node,
    NodeEnroller, NodeReactivateRateLimited, NodeStore, node_event,
)


def _store(tmp_path) -> NodeStore:
    return NodeStore(str(tmp_path / "nodes.db"))


def _ld2450_frame(x_mm: int = 1000, y_mm: int = 500, speed_cms: int = 25) -> str:
    """A valid 30-byte LD2450 report frame (one present target) as hex. Sign-
    magnitude with the MSB as the sign bit set (positive), matching the wired
    source's parse_ld2450_frame decoder."""
    def sm(v: int) -> int:
        return 0x8000 | (v & 0x7FFF)          # MSB set => positive magnitude
    slot = struct.pack("<HHHH", sm(x_mm), sm(y_mm), sm(speed_cms), 0)
    frame = b"\xaa\xff\x03\x00" + slot + b"\x00" * 8 + b"\x00" * 8 + b"\x55\xcc"
    assert len(frame) == 30
    return frame.hex()


# -- NodeStore: enrollment + token auth --------------------------------------

def test_add_returns_id_and_token_and_verifies(tmp_path):
    s = _store(tmp_path)
    node_id, token = s.add("kitchen radar", "ld2450", "kitchen")
    assert node_id and token
    node = s.get_by_token(token)
    assert node is not None
    assert node.node_id == node_id
    assert node.modality == "mmwave"          # ld2450 -> mmwave
    assert node.confidence_cap == 1.0         # native transport = full trust
    assert node.state == STATE_ACTIVE


def test_unknown_token_returns_none(tmp_path):
    s = _store(tmp_path)
    s.add("n", "ld2450", "kitchen")
    assert s.get_by_token("not-a-real-token") is None
    assert s.get_by_token("") is None


def test_modality_and_cap_derived_from_type_and_transport(tmp_path):
    s = _store(tmp_path)
    _, t_pir = s.add("hall pir", "pir", "hall")
    _, t_mqtt = s.add("mqtt radar", "mmwave", "den", transport="mqtt")
    _, t_env = s.add("thermo", "environmental", "den")
    assert s.get_by_token(t_pir).modality == "pir"
    assert s.get_by_token(t_pir).confidence_cap == 1.0
    mqtt_node = s.get_by_token(t_mqtt)
    assert mqtt_node.modality == "mmwave" and mqtt_node.confidence_cap == 0.7
    assert s.get_by_token(t_env).modality == ""   # non-presence sensor


def test_no_remote_enable_method_exists(tmp_path):
    # The kill-switch invariant (remote-OFF-never-ON) is enforced structurally: the
    # store exposes disable + node-initiated reactivate, but NO remote enable.
    s = _store(tmp_path)
    assert not hasattr(s, "enable")


# -- NodeStore: kill-switch state machine ------------------------------------

def test_disable_is_remote_off_and_drops_from_fusion(tmp_path):
    s = _store(tmp_path)
    node_id, token = s.add("n", "ld2450", "kitchen")
    assert s.disable(node_id) is True
    node = s.get_by_token(token)               # disabled node still authenticates...
    assert node is not None and node.state == STATE_DISABLED
    # ...but node_event yields nothing, so its data never reaches fusion.
    assert node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()]}) is None


def test_reactivate_only_edge_back_on_and_needs_higher_press(tmp_path):
    s = _store(tmp_path)
    node_id, _ = s.add("n", "ld2450", "kitchen")
    s.disable(node_id)
    # A press_count that does not exceed the stored high-water mark cannot re-enable.
    assert s.reactivate(node_id, 0) == STATE_DISABLED
    # A strictly-higher physical press flips disabled -> active (the ONLY such edge).
    assert s.reactivate(node_id, 1) == STATE_ACTIVE
    assert s.get(node_id).state == STATE_ACTIVE


def test_reactivate_replay_is_inert(tmp_path):
    s = _store(tmp_path)
    node_id, _ = s.add("n", "ld2450", "kitchen")
    s.reactivate(node_id, 5)                    # advance high-water while active (no-op state)
    s.disable(node_id)
    assert s.reactivate(node_id, 5) == STATE_DISABLED   # replay of <=5 cannot re-enable
    assert s.reactivate(node_id, 6) == STATE_ACTIVE


def test_revoke_is_terminal_and_kills_token(tmp_path):
    s = _store(tmp_path)
    node_id, token = s.add("n", "ld2450", "kitchen")
    assert s.revoke(node_id) is True
    assert s.get_by_token(token) is None       # revoked token is dead
    assert s.disable(node_id) is False         # cannot disable a revoked node
    assert s.reactivate(node_id, 999) == STATE_REVOKED   # cannot resurrect


# -- NodeStore: reactivate abuse brake (appsec finding #3) -------------------

def test_reactivate_rate_limited_after_max_attempts(tmp_path):
    # NOT a security boundary (the server can never verify a physical press) --
    # just a brake on a compromised/buggy node hammering the store. Each call
    # below is a fresh press_count so every one is a "real" attempt, not a replay.
    s = _store(tmp_path)
    node_id, _ = s.add("n", "ld2450", "kitchen")
    for i in range(1, REACTIVATE_MAX_ATTEMPTS + 1):
        assert s.reactivate(node_id, i) == STATE_ACTIVE
    with pytest.raises(NodeReactivateRateLimited):
        s.reactivate(node_id, REACTIVATE_MAX_ATTEMPTS + 1)


def test_reactivate_rate_limit_is_per_node(tmp_path):
    # One node's spam does not throttle another node's legitimate reactivate.
    s = _store(tmp_path)
    node_a, _ = s.add("a", "ld2450", "kitchen")
    node_b, _ = s.add("b", "ld2450", "den")
    for i in range(1, REACTIVATE_MAX_ATTEMPTS + 1):
        s.reactivate(node_a, i)
    with pytest.raises(NodeReactivateRateLimited):
        s.reactivate(node_a, REACTIVATE_MAX_ATTEMPTS + 1)
    assert s.reactivate(node_b, 1) == STATE_ACTIVE   # unaffected


# -- NodeStore: telemetry anti-replay ----------------------------------------

def test_record_seq_rejects_replays(tmp_path):
    s = _store(tmp_path)
    node_id, _ = s.add("n", "ld2450", "kitchen")
    assert s.record_seq(node_id, 1) is True
    assert s.record_seq(node_id, 1) is False   # replay
    assert s.record_seq(node_id, 0) is False   # older
    assert s.record_seq(node_id, 2) is True    # newer accepted
    assert s.record_seq("ghost", 1) is False


# -- NodeEnroller -------------------------------------------------------------

def test_enroller_mint_redeem_creates_node(tmp_path):
    s = _store(tmp_path)
    e = NodeEnroller(s)
    code = e.mint_code("kitchen radar", "ld2450", "kitchen")
    result = e.redeem(code, cert_fingerprint="AB:CD")
    assert result is not None
    node_id, token = result
    node = s.get_by_token(token)
    assert node.room == "kitchen" and node.sensor_type == "ld2450"
    assert node.cert_fingerprint == "AB:CD"


def test_enroller_code_is_one_time(tmp_path):
    s = _store(tmp_path)
    e = NodeEnroller(s)
    code = e.mint_code("n", "ld2450", "kitchen")
    assert e.redeem(code) is not None
    assert e.redeem(code) is None              # consumed


def test_enroller_wrong_and_expired_code(tmp_path):
    now = {"t": datetime(2026, 7, 11, tzinfo=timezone.utc)}
    s = _store(tmp_path)
    e = NodeEnroller(s, now_fn=lambda: now["t"], code_ttl=100)
    assert e.redeem("00000000") is None
    code = e.mint_code("n", "ld2450", "kitchen")
    now["t"] += timedelta(seconds=101)
    assert e.redeem(code) is None              # expired


def test_enroller_per_ip_rate_limit(tmp_path):
    s = _store(tmp_path)
    e = NodeEnroller(s, max_failed=3)
    for _ in range(3):
        assert e.redeem("bad", source_ip="10.0.0.9") is None
    # This host is now locked out even for a would-be-valid code...
    code = e.mint_code("n", "ld2450", "kitchen")
    assert e.redeem(code, source_ip="10.0.0.9") is None
    # ...but a different host is unaffected.
    assert e.redeem(code, source_ip="10.0.0.8") is not None


def test_enroller_rejects_unknown_type_transport_room(tmp_path):
    e = NodeEnroller(_store(tmp_path))
    with pytest.raises(ValueError):
        e.mint_code("n", "nonsense", "kitchen")
    with pytest.raises(ValueError):
        e.mint_code("n", "ld2450", "kitchen", transport="carrier-pigeon")
    with pytest.raises(ValueError):
        e.mint_code("n", "ld2450", "   ")


# -- node_event translation ---------------------------------------------------

def _active_node(**over) -> Node:
    base = dict(node_id="x", name="n", sensor_type="ld2450", modality="mmwave",
                room="kitchen", transport="native", cert_fingerprint="",
                confidence_cap=1.0, state=STATE_ACTIVE, press_count=0, last_seq=0,
                last_seen_ts=None, created_ts="2026-07-11T00:00:00+00:00")
    base.update(over)
    return Node(**base)


def test_node_event_ld2450_frame_to_mmwave(tmp_path):
    node = _active_node()
    ev = node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert ev is not None
    assert ev.modality == "mmwave" and ev.room == "kitchen"
    assert ev.presence is True and ev.count == 1        # radar counts discrete targets
    assert len(ev.targets) == 1
    assert ev.targets[0].x == pytest.approx(1.0)


def test_node_event_room_and_modality_from_record_not_payload(tmp_path):
    # Anti-spoof: a node cannot relocate itself or claim a higher-trust modality.
    node = _active_node()
    ev = node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()],
                           "room": "bedroom", "modality": "camera"})
    assert ev.room == "kitchen" and ev.modality == "mmwave"


def test_node_event_mqtt_transport_caps_confidence(tmp_path):
    node = _active_node(transport="mqtt", confidence_cap=0.7)
    ev = node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert ev.confidence == pytest.approx(0.7)          # 0.9 raw capped to 0.7


def test_node_event_disabled_returns_none(tmp_path):
    node = _active_node(state=STATE_DISABLED)
    assert node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()]}) is None


def test_node_event_non_presence_sensor_returns_none(tmp_path):
    node = _active_node(sensor_type="environmental", modality="")
    assert node_event(node, {"seq": 1, "presence": True}) is None


def test_node_event_pir_presence_never_counts(tmp_path):
    node = _active_node(sensor_type="pir", modality="pir")
    ev = node_event(node, {"seq": 1, "presence": True, "motion": 0.4, "count": 3})
    assert ev.modality == "pir" and ev.presence is True
    assert ev.count is None                             # PIR can't count people
    assert ev.confidence == pytest.approx(0.7)          # decoded default


def test_node_event_empty_ld2450_is_vacant(tmp_path):
    node = _active_node()
    empty = (b"\xaa\xff\x03\x00" + b"\x00" * 24 + b"\x55\xcc").hex()
    ev = node_event(node, {"seq": 1, "ld2450_frames": [empty]})
    assert ev.presence is False and ev.count == 0 and ev.confidence == 0.0


def test_node_event_rejects_nan_and_infinite_floats(tmp_path):
    # A malicious/buggy node can hand raw JSON floats straight to a Target; NaN and
    # +-Infinity must never survive into a SensingEvent (they'd break strict
    # frontend JSON.parse on re-serialization -- see _num's docstring).
    node = _active_node(sensor_type="generic", modality="node")
    ev = node_event(node, {"seq": 1, "targets": [
        {"x": float("nan"), "y": 1.0},
        {"x": float("inf"), "y": float("-inf")},
        {"x": 1.0, "y": 2.0, "velocity": float("nan")},
        {"x": 1.0, "y": 2.0, "confidence": float("inf")},
    ]})
    assert ev is not None
    for t in ev.targets:
        assert t.x is None or math.isfinite(t.x)
        assert t.y is None or math.isfinite(t.y)
        assert t.velocity is None or math.isfinite(t.velocity)
        assert math.isfinite(t.confidence)
    ev2 = node_event(node, {"seq": 2, "presence": True,
                            "motion": float("nan"), "confidence": float("inf")})
    assert math.isfinite(ev2.motion) and math.isfinite(ev2.confidence)


# -- node_event: malformed telemetry is dropped, never raises (appsec finding #2) -

def test_node_event_malformed_ld2450_frames_not_a_list_is_dropped(tmp_path):
    # A wrong-shaped `ld2450_frames` (not iterable) used to raise an unhandled
    # TypeError straight into the request handler. Must now be a clean no-op.
    node = _active_node()
    assert node_event(node, {"seq": 1, "ld2450_frames": 123}) is None
    assert node_event(node, {"seq": 1, "ld2450_frames": True}) is None


def test_node_event_malformed_targets_not_a_list_is_dropped(tmp_path):
    # A truthy non-iterable `targets` (e.g. an int/bool) used to raise TypeError
    # from `enumerate(...)`. A string IS iterable (per-character) so it does NOT
    # crash -- it legitimately yields zero targets (each char fails the dict
    # check) -- that is correct behavior, not the bug this test targets.
    node = _active_node(sensor_type="generic", modality="node")
    assert node_event(node, {"seq": 1, "targets": 5}) is None
    assert node_event(node, {"seq": 1, "targets": 3.5}) is None


def test_node_event_dict_shaped_ld2450_frames_dropped_not_500(tmp_path):
    # M4 (appsec re-audit, 2026-07, MEDIUM): a JSON OBJECT for `ld2450_frames`
    # (dict) is truthy and used to reach the bare `payload["ld2450_frames"][:64]`
    # slice -- on this Python, slicing a dict raises KeyError (slice objects are
    # hashable), which escaped the narrower (TypeError, ValueError, OverflowError)
    # catch and would 500 the /api/nodes/telemetry route for any node-token
    # holder. Must now be a clean drop, exactly like the int/bool cases above.
    node = _active_node()
    assert node_event(node, {"seq": 1, "ld2450_frames": {"foo": 1}}) is None
    # A single-key dict is still truthy and still not a list -- same drop.
    assert node_event(node, {"seq": 1, "ld2450_frames": {"0": "aa"}}) is None


def test_node_event_dict_shaped_targets_dropped_not_500(tmp_path):
    # Same KeyError-on-slice shape error, for the `targets` field of a non-LD2450/
    # mmwave node (or an ld2450/mmwave node with no `ld2450_frames`, which falls
    # into the same `else` branch below).
    node = _active_node(sensor_type="generic", modality="node")
    assert node_event(node, {"seq": 1, "targets": {"foo": 1}}) is None
    assert node_event(node, {"seq": 1, "targets": {"0": {"id": 1, "x": 1.0, "y": 2.0}}}) is None


def test_node_event_malformed_target_id_is_dropped(tmp_path):
    # A target `id` that can't coerce to int (str garbage or a nested structure)
    # used to raise ValueError/TypeError out of int(t.get("id", ...)).
    node = _active_node(sensor_type="generic", modality="node")
    assert node_event(node, {"seq": 1, "targets": [
        {"id": "not-a-number", "x": 1.0, "y": 2.0}]}) is None
    assert node_event(node, {"seq": 1, "targets": [
        {"id": [1, 2], "x": 1.0, "y": 2.0}]}) is None


def test_node_event_overflow_errors_dropped_not_raised(tmp_path):
    # Appsec finding #2, part 2: TypeError/ValueError weren't the only shape-error
    # a hostile node can trigger. A JSON int with hundreds of digits (well within
    # spec -- JSON doesn't cap integer size) blows past what `math.isfinite()` can
    # widen to a C double inside `_num()`, raising OverflowError, not TypeError/
    # ValueError, for x/y/motion/velocity/confidence. And the non-standard JSON
    # float literal `1e400` decodes (via Python's json module) straight to +inf,
    # which then blows up `int(t.get("id", ...))` the same way. Both used to
    # escape the narrower `except (TypeError, ValueError)` and 500 the caller;
    # must now drop the whole payload (None) like every other malformed shape.
    node = _active_node(sensor_type="generic", modality="node")
    huge = 10 ** 400
    inf_id = float("inf")                     # what json.loads("1e400") decodes to
    assert node_event(node, {"seq": 1, "targets": [
        {"id": 1, "x": huge, "y": 1.0}]}) is None
    assert node_event(node, {"seq": 1, "targets": [
        {"id": 1, "x": 1.0, "y": 2.0, "velocity": huge}]}) is None
    assert node_event(node, {"seq": 1, "targets": [
        {"id": 1, "x": 1.0, "y": 2.0, "confidence": huge}]}) is None
    assert node_event(node, {"seq": 1, "targets": [
        {"id": inf_id, "x": 1.0, "y": 2.0}]}) is None
    assert node_event(node, {"seq": 1, "presence": True, "motion": huge}) is None
    assert node_event(node, {"seq": 1, "presence": True, "confidence": huge}) is None
    # The fix must not swallow a legitimately large-but-FINITE reading.
    ev = node_event(node, {"seq": 1, "presence": True, "motion": 2 ** 62})
    assert ev is not None and math.isfinite(ev.motion)


def test_node_event_well_formed_telemetry_still_works_after_the_fix(tmp_path):
    # The try/except added for the fix above must not swallow legitimate events.
    node = _active_node()
    ev = node_event(node, {"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert ev is not None and ev.presence is True and ev.count == 1
