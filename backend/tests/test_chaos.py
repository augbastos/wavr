"""Chaos scenarios: prove the FusionEngine stabilizes RoomState under stress.
Everything is deterministic (no RNG, fixed clock), so the same run backs both the
tests here and the scripts/chaos_demo.py demo."""

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine, DEFAULT_WEIGHTS
from wavr.roomstate import RoomState
from wavr.rules import RulesEngine
from wavr.sources.base import SensorSource
from wavr.sources.chaos import (
    ChaosSource, FLICKER_CONF, SCENARIOS, scenario_events,
    wifi_drop, camera_flicker, multi_target, fall,
)
from wavr.storage import Storage


def _replay(events, engine=None):
    """Feed a scripted event list through fusion, returning (event, state) pairs."""
    f = engine or FusionEngine()
    return [(e, f.update(e)) for e in events]


async def _take(agen, n):
    out = []
    async for x in agen:
        out.append(x)
        if len(out) >= n:
            break
    return out


# ---- source seam / plumbing ----

def test_chaos_source_satisfies_protocol():
    assert isinstance(ChaosSource("wifi-drop"), SensorSource)


def test_unknown_scenario_raises():
    try:
        scenario_events("nope")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unknown chaos scenario" in str(e)


def test_scenarios_are_deterministic():
    a = [e.to_dict() for e in scenario_events("fall")]
    b = [e.to_dict() for e in scenario_events("fall")]
    assert a == b   # includes timestamps: chaos never touches the wall clock


async def test_chaos_source_replays_full_script_then_stops():
    src = ChaosSource("camera-flicker", interval=0.0)
    script = scenario_events("camera-flicker")
    got = await _take(src.events(), len(script))
    assert [e.to_dict() for e in got] == [e.to_dict() for e in script]
    assert all(isinstance(e, SensingEvent) for e in got)


# ---- 1. wifi-drop ----

def test_wifi_drop_stays_occupied_while_camera_holds():
    # The network source collapses mid-run, but the camera keeps reporting
    # presence, so the room must NOT fall vacant during the dropout.
    pairs = _replay(wifi_drop())
    held = [rs for ev, rs in pairs
            if ev.modality == "camera" and ev.presence       # camera still reporting
            and any(s["modality"] == "network" and not s["presence"] for s in rs.sources)]
    assert held, "expected a phase with network absent but camera still present"
    assert all(rs.occupied for rs in held)                   # camera (trusted) holds it occupied


def test_wifi_drop_goes_vacant_only_when_all_trusted_sources_gone():
    pairs = _replay(wifi_drop())
    assert pairs[1][1].occupied is True                      # after first tick's camera event
    assert pairs[-1][1].occupied is False                    # ends vacant (camera also gone)


def test_wifi_drop_collapses_a_lone_network_signal_to_zero():
    # Network is coarse (weight 0.5) so it never alone-occupies a room, but its
    # confidence must visibly collapse to zero once the source drops out.
    net_only = [e for e in wifi_drop() if e.modality == "network"]
    pairs = _replay(net_only)
    assert pairs[0][1].confidence > 0.0                      # some signal while present
    assert pairs[-1][1].confidence == 0.0                    # collapsed to nothing
    assert pairs[-1][1].occupied is False


# ---- 2. camera-flicker ----

def test_camera_flicker_never_reaches_full_confidence():
    # A lone, weak-confidence flickering source must never read 100%: agreement
    # is 1.0 for a single present source, so only `× strength` keeps it honest.
    states = [rs for _, rs in _replay(camera_flicker())]
    confs = [rs.confidence for rs in states]
    assert max(confs) < 1.0
    # peak == strength (weight × conf), NOT the inflated agreement of 1.0 (=100%)
    assert max(confs) <= DEFAULT_WEIGHTS["camera"] * FLICKER_CONF + 1e-9


def test_camera_flicker_oscillates_but_is_bounded():
    states = [rs for _, rs in _replay(camera_flicker())]
    assert any(rs.occupied for rs in states)      # false-positive frames
    assert any(not rs.occupied for rs in states)  # false-negative frames


def test_camera_flicker_is_stabilized_by_a_steady_trusted_source():
    # Fuse the flickering camera with a steady wifi_csi presence in the same room:
    # the trusted steady source damps the flicker and the room stays occupied.
    f = FusionEngine()
    for i, cam in enumerate(camera_flicker()):
        f.update(SensingEvent(room=cam.room, modality="wifi_csi", presence=True,
                              motion=2.0, breathing_bpm=13.0, heart_bpm=64.0,
                              confidence=0.9, ts=cam.ts))
        rs = f.update(cam)
        assert rs.occupied is True                 # never flaps vacant now
        assert rs.confidence < 1.0


# ---- 3. multi-target ----

def test_multi_target_all_targets_flow_to_roomstate():
    events = multi_target()
    _, rs = _replay(events)[-1]
    assert 6 <= len(rs.targets) <= 8
    assert len(rs.targets) == len(events[-1].targets)
    assert {t["id"] for t in rs.targets} == set(range(1, len(rs.targets) + 1))
    assert rs.occupied is True


def test_multi_target_targets_are_live_only_never_persisted():
    # HARD INVARIANT: per-person x/y targets flow live (WebSocket) but must never
    # reach SQLite or MQTT.
    _, rs = _replay(multi_target())[-1]
    assert len(rs.to_dict()["targets"]) == 7          # live path carries them

    st = Storage(":memory:")
    st.insert_state(rs)
    row = st.recent()[0]
    st.close()
    assert "targets" not in row                        # SQLite never stored them
    assert "vitals" not in row                         # ADR-0002: vitals live-only too
    assert set(row.keys()) == {"room", "occupied", "confidence",
                               "sources", "explanation", "ts"}

    msgs = []
    RulesEngine(lambda t, p, r: msgs.append((t, p))).handle(rs.to_dict())
    state_payload = [p for t, p in msgs if t.endswith("/state")][0]
    assert "target" not in state_payload               # MQTT never published them


# ---- 4. fall ----

def test_fall_transitions_posture_standing_to_lying():
    pairs = _replay(fall())
    postures = [rs.targets[0]["posture"] for _, rs in pairs if rs.targets]
    assert postures[0] == "standing"
    assert postures[-1] == "lying"


def test_fall_reflects_lying_and_low_motion_but_stays_occupied():
    pairs = _replay(fall())
    # room never falsely reads vacant just because body motion collapsed
    assert all(rs.occupied for _, rs in pairs)
    _, final = pairs[-1]
    assert final.targets[0]["posture"] == "lying"
    assert final.targets[0]["velocity"] == 0.0         # micro-motion only
    assert final.vitals.get("breathing_bpm") == 13.0   # still breathing / present
    # the underlying wifi_csi body motion dropped across the fall
    csi_motion = [e.motion for e in fall() if e.modality == "wifi_csi"]
    assert csi_motion[0] > 5.0 and csi_motion[-1] < 1.0


def test_fall_targets_are_not_persisted():
    _, rs = _replay(fall())[-1]
    st = Storage(":memory:")
    st.insert_state(rs)
    assert "targets" not in st.recent()[0]
    st.close()


# ---- registry sanity ----

def test_all_four_scenarios_registered_and_runnable():
    assert set(SCENARIOS) == {"wifi-drop", "camera-flicker", "multi-target", "fall"}
    for name in SCENARIOS:
        pairs = _replay(scenario_events(name))
        assert pairs and all(isinstance(rs, RoomState) for _, rs in pairs)
