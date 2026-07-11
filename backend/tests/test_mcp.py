import importlib

import pytest

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.mcp import (
    FusionStateProvider,
    build_mcp_server,
    get_alerts,
    get_house_map,
    get_house_status,
    get_network_inventory,
    get_room_context,
    list_rooms,
    query_occupancy_history,
)

HOUSE = {"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}]}


def _ev(room, modality, presence, conf):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf,
                        ts="2026-07-02T10:00:00+00:00")


class FakeProvider:
    """Minimal StateProvider stand-in for pure shape tests (mocked state)."""

    def __init__(self, rooms, states, house):
        self._rooms = rooms
        self._states = states
        self._house = house

    def list_rooms(self):
        return list(self._rooms)

    def room_state(self, room):
        return self._states.get(room)

    def house_map(self):
        return self._house


# --- import is lazy: no `mcp` package needed to import the module ------------------

def test_import_wavr_mcp_without_mcp_sdk_installed():
    # Importing the module must NOT require the optional [mcp] extra.
    m = importlib.import_module("wavr.mcp")
    assert hasattr(m, "list_rooms") and hasattr(m, "build_mcp_server")


def test_building_server_without_mcp_sdk_raises_import_error():
    # The SDK import is deferred to build time. If it's genuinely absent (as in the
    # dev test venv), building raises ImportError; if a dev has it installed, skip.
    try:
        import mcp.server.fastmcp  # noqa: F401
    except Exception:
        with pytest.raises(ImportError):
            build_mcp_server(FakeProvider([], {}, {}))
    else:
        pytest.skip("mcp SDK is installed; lazy-import-absent path not exercised")


# --- plain tool logic against a mocked provider -----------------------------------

def test_list_rooms_shape():
    states = {
        "sala": {"room": "sala", "occupied": True, "confidence": 0.8},
        "quarto": {"room": "quarto", "occupied": False, "confidence": 0.1},
    }
    out = list_rooms(FakeProvider(["sala", "quarto"], states, HOUSE))
    assert out == [
        {"room": "sala", "occupied": True, "confidence": 0.8},
        {"room": "quarto", "occupied": False, "confidence": 0.1},
    ]


def test_list_rooms_skips_rooms_with_no_state():
    out = list_rooms(FakeProvider(["ghost"], {}, HOUSE))
    assert out == []


def test_get_room_context_returns_full_why():
    rs = {"room": "sala", "occupied": True, "confidence": 0.72,
          "sources": [{"modality": "wifi_csi", "presence": True, "confidence": 0.6}],
          "explanation": "wifi_csi: presente -> 72% ocupado"}
    ctx = get_room_context(FakeProvider(["sala"], {"sala": rs}, HOUSE), "sala")
    assert ctx["sources"][0]["modality"] == "wifi_csi"
    assert "ocupado" in ctx["explanation"]


def test_get_room_context_unknown_room_is_none():
    assert get_room_context(FakeProvider([], {}, HOUSE), "nope") is None


def test_get_room_context_strips_vitals_and_targets():
    # Privacy (audit CRITICAL-1): the MCP read tool must never expose per-person
    # breathing/heart rate or x/y tracking, even though the underlying RoomState
    # carries both. Only room-level occupancy/confidence + the explainable why.
    rs = {"room": "sala", "occupied": True, "confidence": 0.72,
          "vitals": {"breathing_bpm": 14, "heart_bpm": 68},
          "targets": [{"x": 1.2, "y": 3.4, "id": "person-1"}],
          "identities": [{"person": "alice", "source": "ble", "rssi": -55}],
          "sources": [{"modality": "mmwave", "presence": True, "confidence": 0.9}],
          "explanation": "mmwave: presente -> 72% ocupado"}
    ctx = get_room_context(FakeProvider(["sala"], {"sala": rs}, HOUSE), "sala")
    assert "vitals" not in ctx
    assert "targets" not in ctx
    assert "identities" not in ctx    # "who is home" is PII — never reaches an agent
    assert ctx["room"] == "sala"
    assert ctx["occupied"] is True
    assert ctx["confidence"] == 0.72
    assert ctx["sources"][0]["modality"] == "mmwave"
    assert "ocupado" in ctx["explanation"]


def test_get_room_context_strips_vitals_and_targets_from_real_fusion():
    # Same invariant against a REAL FusionEngine/RoomState (not just a fake dict).
    fusion = FusionEngine()
    fusion.update(SensingEvent(room="sala", modality="mmwave", presence=True, motion=1.0,
                               breathing_bpm=14.0, heart_bpm=68.0, confidence=0.9,
                               ts="2026-07-02T10:00:00+00:00"))
    provider = FusionStateProvider(fusion, HOUSE)
    # Sanity: the underlying state actually carries vitals (proves this is a real test).
    assert provider.room_state("sala")["vitals"]
    ctx = get_room_context(provider, "sala")
    assert "vitals" not in ctx
    assert "targets" not in ctx


def test_get_house_map_no_floors_key_degrades_to_empty_floors():
    # verify FIX C (MEDIUM): a malformed/legacy-shaped house dict (HOUSE here has
    # no top-level "floors", the v1 shape) degrades honestly to an empty floors
    # list -- never a crash, and never the raw dict verbatim.
    assert get_house_map(FakeProvider([], {}, HOUSE)) == {"floors": []}


def test_get_house_map_minimizes_to_room_id_and_polygon():
    # verify FIX C (MEDIUM): house.json is home-layout PII -- get_house_map must
    # NOT hand an agent the floor plan verbatim. Only room id + polygon survive,
    # grouped by floor id/level; every name/label/note/free-text field (floor
    # name, room name, zone name/kind, walls, features, backdrop) is dropped.
    house = {
        "version": 2, "units": "m",
        "floors": [{
            "id": "f0", "name": "Térreo", "level": 0,
            "rooms": [{"id": "r1", "name": "quarto-do-augusto",
                       "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
            "walls": [{"id": "w1", "a": [0, 0], "b": [4, 0]}],
            "features": [{"id": "d1", "type": "door", "at": [2, 0]}],
            "zones": [{"id": "z1", "name": "bed", "kind": "rest",
                      "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
            "backdrop": None,
        }],
    }
    out = get_house_map(FakeProvider([], {}, house))
    assert out == {"floors": [{
        "id": "f0", "level": 0,
        "rooms": [{"id": "r1", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
    }]}
    assert "quarto-do-augusto" not in str(out)   # room NAME (free-text label) dropped
    assert "Térreo" not in str(out)              # floor NAME dropped
    assert "bed" not in str(out)                 # zone NAME dropped (zones[] gone entirely)


def test_get_house_map_tolerates_malformed_floors_and_rooms():
    # Defensive: a non-list `floors`, a non-dict floor/room entry, or a missing
    # `rooms` key must never crash the tool -- degrade to empty, honestly.
    house = {"floors": "not-a-list"}
    assert get_house_map(FakeProvider([], {}, house)) == {"floors": []}
    house2 = {"floors": [{"id": "f0", "level": 0}, "not-a-dict"]}
    assert get_house_map(FakeProvider([], {}, house2)) == {
        "floors": [{"id": "f0", "level": 0, "rooms": []}]}


# --- Whole-house read tools (Phase 2A / B1-B3): plain function tests --------------

def test_get_network_inventory_minimizes_pii_fields():
    # Phase-2A verify FIX 1 (HIGH): mac/name/hostname/first_seen/last_seen/
    # open_ports/sources are PII / LAN-attack-surface / tracking data and must
    # NEVER reach the agent-facing MCP surface, even though GET /api/inventory
    # (the human dashboard, unaffected) returns all of them. Only coarse
    # identity + a count survive.
    devices = [{
        "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5", "vendor": "TP-Link",
        "device_type": "smart_plug", "type_confidence": "high", "known": True,
        "make": "TP-Link", "model": "HS100", "os": "RTOS",
        "name": "a smart plug", "hostname": "smart-plug.lan",
        "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-07-10T10:00:00Z",
        "open_ports": [80, 443],
        "sources": [{"signal": "hostname",
                    "value": "smart-plug -> smart_plug", "weight": 5}],
        "is_gateway": False,
    }]
    out = get_network_inventory(lambda: devices)
    assert out == {"devices": [{
        "ip": "192.168.1.5", "vendor": "TP-Link", "device_type": "smart_plug",
        "type_confidence": "high", "known": True,
        "make": "TP-Link", "model": "HS100", "os": "RTOS",
    }], "count": 1}
    leaked = ("mac", "name", "hostname", "first_seen", "last_seen",
             "open_ports", "sources", "is_gateway")
    for field in leaked:
        assert field not in out["devices"][0]


def test_get_network_inventory_keeps_is_gateway_only_when_true():
    devices = [{"ip": "192.168.1.1", "vendor": "unknown", "device_type": "router",
                "type_confidence": "low", "known": True, "is_gateway": True}]
    out = get_network_inventory(lambda: devices)
    assert out["devices"][0]["is_gateway"] is True
    devices2 = [{"ip": "192.168.1.5", "vendor": "unknown", "device_type": "phone",
                "type_confidence": "low", "known": False, "is_gateway": False}]
    out2 = get_network_inventory(lambda: devices2)
    assert "is_gateway" not in out2["devices"][0]


def test_get_network_inventory_none_provider_is_disabled_shape():
    # Not wired (e.g. WAVR_NET_INVENTORY off, or a minimal build_mcp_server caller that
    # never passed network_inventory_fn) -> an honest empty list, never a crash.
    assert get_network_inventory(None) == {"devices": [], "count": 0}


def test_get_network_inventory_wired_but_currently_empty():
    # DISTINCT from the None-provider case above: the source IS wired (e.g.
    # WAVR_NET_INVENTORY on) but no scan has produced any devices yet -- still an
    # honest empty list, not a crash or a None leaking through.
    assert get_network_inventory(lambda: []) == {"devices": [], "count": 0}


def test_get_alerts_minimizes_pii_fields():
    # Phase-2A verify FIX 3 (LOW): the live known_present headcount and the
    # gateway/rogue MAC+IP/vendor/hostname fields must never reach the
    # agent-facing MCP surface -- only kind/severity/room/ts survive, with
    # `room` honestly None for the room-less network-layer alert kinds.
    alerts = [
        {"kind": "rogue_device", "severity": "note", "ts": "2026-07-10T10:00:00Z",
         "mac": "aa:bb:cc:dd:ee:ff", "vendor": "unknown", "ip": "192.168.1.77",
         "hostname": "unknown-device", "device_type": "unknown",
         "type_confidence": "low"},
        {"kind": "gateway_identity", "severity": "critical",
         "ts": "2026-07-10T10:05:00Z", "gateway_ip": "192.168.1.1",
         "trusted_mac": "11:22:33:44:55:66", "observed_mac": "aa:aa:aa:aa:aa:aa"},
        {"kind": "intrusion", "severity": "alert", "room": "sala",
         "person_count": 2, "known_present": 1, "ts": "2026-07-10T10:10:00Z"},
    ]
    out = get_alerts(lambda: alerts)
    assert out == {"alerts": [
        {"kind": "rogue_device", "severity": "note", "room": None,
         "ts": "2026-07-10T10:00:00Z"},
        {"kind": "gateway_identity", "severity": "critical", "room": None,
         "ts": "2026-07-10T10:05:00Z"},
        {"kind": "intrusion", "severity": "alert", "room": "sala",
         "ts": "2026-07-10T10:10:00Z"},
    ]}
    for a in out["alerts"]:
        assert set(a) == {"kind", "severity", "room", "ts"}


def test_get_alerts_none_provider_is_disabled_shape():
    assert get_alerts(None) == {"alerts": []}


def test_get_alerts_wired_but_currently_empty():
    # DISTINCT from the None-provider case: wired, but nothing is currently
    # alerting -- an honest empty list, not a crash.
    assert get_alerts(lambda: []) == {"alerts": []}


class FakeOccupancyProvider:
    """Minimal OccupancyHistoryProvider stand-in -- captures call args so tests can
    assert query_occupancy_history wires them correctly (room/start window/weeks)."""

    def __init__(self, timeline_out=None, routine_out=None, unusual_out=None):
        self.timeline_calls = []
        self.routine_calls = []
        self.unusual_calls = []
        self._timeline_out = timeline_out if timeline_out is not None else []
        self._routine_out = routine_out if routine_out is not None else {}
        self._unusual_out = unusual_out if unusual_out is not None else {}

    def timeline(self, room=None, *, start=None, end=None, limit=1000):
        self.timeline_calls.append({"room": room, "start": start, "end": end, "limit": limit})
        return self._timeline_out

    def routine(self, room, *, weeks=4.0):
        self.routine_calls.append({"room": room, "weeks": weeks})
        return self._routine_out

    def is_unusual(self, room, occupied_now, *, weeks=4.0):
        self.unusual_calls.append({"room": room, "occupied_now": occupied_now, "weeks": weeks})
        return self._unusual_out


def test_query_occupancy_history_disabled_without_provider():
    out = query_occupancy_history(FakeProvider([], {}, HOUSE), None, room="sala", hours=24)
    assert out == {"enabled": False, "history": [], "routine": None, "unusual": None}


def test_query_occupancy_history_house_wide_has_no_room_routine_or_unusual():
    # No `room` given -> routine/unusual are inherently per-room, so both stay None
    # even though history (across all rooms) is returned.
    history = [{"room": "sala", "occupied": True, "person_count": 1,
               "confidence": 0.8, "ts": "2026-07-10T09:00:00+00:00"}]
    occ = FakeOccupancyProvider(timeline_out=history)
    out = query_occupancy_history(FakeProvider([], {}, HOUSE), occ, room=None, hours=24)
    assert out == {"enabled": True, "history": history, "routine": None, "unusual": None}
    assert occ.timeline_calls[0]["room"] is None
    assert occ.routine_calls == [] and occ.unusual_calls == []


def test_query_occupancy_history_with_room_includes_routine_and_unusual():
    state = {"sala": {"room": "sala", "occupied": True, "confidence": 0.8}}
    provider = FakeProvider(["sala"], state, HOUSE)
    routine = {"room": "sala", "weeks": 4.0, "hours": [{"hour": 9, "probability": 0.7,
              "samples": 5, "trusted": True}]}
    unusual = {"unusual": False, "baseline_probability": 0.7, "samples": 5, "hour": 9}
    occ = FakeOccupancyProvider(timeline_out=[], routine_out=routine, unusual_out=unusual)
    out = query_occupancy_history(provider, occ, room="sala", hours=48)
    assert out["routine"] == routine
    assert out["unusual"] == unusual
    # "occupied now" is read from the SAME live provider list_rooms/get_room_context
    # use -- never a second source of truth.
    assert occ.unusual_calls == [{"room": "sala", "occupied_now": True, "weeks": 4.0}]


def test_query_occupancy_history_unknown_room_state_skips_unusual():
    # room given, but Wavr has no CURRENT fused reading for it (never seen, or a typo)
    # -> routine still computes (pure history), but unusual honestly stays None rather
    # than guessing a current occupied value.
    occ = FakeOccupancyProvider(routine_out={"room": "quarto", "weeks": 4.0, "hours": []})
    out = query_occupancy_history(FakeProvider([], {}, HOUSE), occ, room="quarto", hours=24)
    assert out["routine"] == {"room": "quarto", "weeks": 4.0, "hours": []}
    assert out["unusual"] is None
    assert occ.unusual_calls == []


def test_query_occupancy_history_clamps_nonpositive_hours():
    # Defensive clamp (never an unbounded/empty-window query off a bad `hours`).
    occ = FakeOccupancyProvider()
    query_occupancy_history(FakeProvider([], {}, HOUSE), occ, room=None, hours=0)
    assert occ.timeline_calls[0]["start"] is not None   # a real window, not "now..now"


def test_query_occupancy_history_clamps_excessive_hours():
    # The OTHER half of the clamp (module docstring: "defensive clamp -- never an
    # unbounded query"): an absurdly large `hours` must NOT be forwarded verbatim
    # -- it must be capped at _OCCUPANCY_MAX_HOURS (~1 year), not sent as a
    # multi-millennium window to the occupancy log.
    from datetime import datetime, timedelta, timezone
    from wavr.mcp import _OCCUPANCY_MAX_HOURS
    occ = FakeOccupancyProvider()
    before = datetime.now(timezone.utc)
    query_occupancy_history(FakeProvider([], {}, HOUSE), occ, room=None, hours=999_999_999)
    start = datetime.fromisoformat(occ.timeline_calls[0]["start"])
    expected = before - timedelta(hours=_OCCUPANCY_MAX_HOURS)
    assert abs((start - expected).total_seconds()) < 5   # clamped, not unbounded


def test_get_house_status_none_fn_is_unknown_shape():
    import anyio
    out = anyio.run(get_house_status, None)
    assert out == {"status": "unknown", "score": 0, "reasons": [], "ts": None}


def test_get_house_status_awaits_async_fn():
    import anyio

    # `window_minutes` is captured via the closure (not round-tripped through the
    # returned dict) because verify FIX B now projects the result down to an
    # explicit {status, score, reasons, ts} allowlist -- an extra top-level key
    # like the old test's "window_minutes" would be silently dropped, by design.
    captured = {}

    async def fake_house_status(window_minutes):
        captured["window_minutes"] = window_minutes
        return {"status": "ok", "score": 0, "reasons": [], "ts": "2026-07-10T10:00:00+00:00"}

    out = anyio.run(get_house_status, fake_house_status, 30.0)
    assert out["status"] == "ok" and captured["window_minutes"] == 30.0


def test_get_house_status_supports_sync_fn():
    # The stdio loopback bridge is a plain sync HTTP GET -- get_house_status must not
    # require an awaitable.
    import anyio

    def fake_house_status(window_minutes):
        return {"status": "notice", "score": 2, "reasons": [], "ts": "t"}

    out = anyio.run(get_house_status, fake_house_status)
    assert out["status"] == "notice"


def test_get_house_status_minimizes_network_identifiers_from_reasons():
    # verify FIX B (MEDIUM): wavr.house_status._network_what embeds vendor/
    # extra_server/gateway_ip into reasons[].what for network-layer reasons --
    # this tool is reachable from the CLOUD-scoped default tool set, so those
    # identifiers must never reach an agent, even though GET /api/house-status
    # (the human dashboard, unaffected) shows the same caption unminimized.
    import anyio

    async def fake_house_status(window_minutes):
        return {
            "status": "alert", "score": 5,
            "reasons": [
                {"layer": "network", "kind": "rogue_device",
                 "what": "unrecognized device on the network (TP-Link)",
                 "severity": "alert", "ts": "2026-07-10T10:00:00+00:00"},
                {"layer": "network", "kind": "gateway_identity",
                 "what": "router (gateway) identity changed (192.168.1.1)",
                 "severity": "alert", "ts": "2026-07-10T10:01:00+00:00"},
            ],
            "ts": "2026-07-10T10:01:00+00:00",
        }

    out = anyio.run(get_house_status, fake_house_status)
    assert out["status"] == "alert" and out["score"] == 5
    assert out["reasons"] == [
        {"layer": "network", "kind": "rogue_device",
         "severity": "alert", "ts": "2026-07-10T10:00:00+00:00"},
        {"layer": "network", "kind": "gateway_identity",
         "severity": "alert", "ts": "2026-07-10T10:01:00+00:00"},
    ]
    for r in out["reasons"]:
        assert "what" not in r
    assert "TP-Link" not in str(out)
    assert "192.168.1.1" not in str(out)


def test_get_house_status_malformed_result_degrades_honestly():
    # A house_status_fn returning something that isn't a dict (a caller bug, or a
    # future non-conforming source) must degrade to the honest "unknown" shape,
    # never raise or pass a non-dict/list through to the caller.
    import anyio

    async def fake_house_status(window_minutes):
        return "not a dict"

    out = anyio.run(get_house_status, fake_house_status)
    assert out == {"status": "unknown", "score": 0, "reasons": [], "ts": None}


# --- FusionStateProvider adapter against a REAL FusionEngine ----------------------

def test_provider_lists_rooms_and_context_from_real_fusion():
    fusion = FusionEngine()
    fusion.update(_ev("sala", "camera", True, 0.95))
    fusion.update(_ev("quarto", "network", False, 0.5))
    provider = FusionStateProvider(fusion, HOUSE)

    rooms = list_rooms(provider)
    names = {r["room"] for r in rooms}
    assert names == {"sala", "quarto"}
    sala = next(r for r in rooms if r["room"] == "sala")
    assert sala["occupied"] is True and 0.0 < sala["confidence"] <= 1.0

    ctx = get_room_context(provider, "sala")
    assert ctx["room"] == "sala"
    assert ctx["sources"][0]["modality"] == "camera"
    assert ctx.get("explanation")           # the explainable "why" is present

    # HOUSE (v1-shaped, no top-level "floors") minimizes to an honest empty
    # floors list -- see test_get_house_map_no_floors_key_degrades_to_empty_floors.
    assert get_house_map(provider) == {"floors": []}


def test_provider_unknown_room_returns_none():
    provider = FusionStateProvider(FusionEngine(), HOUSE)
    assert provider.room_state("void") is None
    assert get_room_context(provider, "void") is None


# --- REAL MCP server build (needs the [mcp] extra) --------------------------------
# These exercise build_mcp_server / the stdio bridge with the SDK actually installed.
# They are skipped cleanly on the no-extra CI leg. The first one is the regression lock
# for the UnboundLocalError shadowing bug: before the fix, build_mcp_server raised at
# tool-registration time, so merely BUILDING the server (R1) would have caught it.

import anyio  # noqa: E402

pytest.importorskip("mcp.server.fastmcp")

_EXPECTED_TOOLS = [
    "call_ha_service", "get_alerts", "get_ha_entities", "get_house_map",
    "get_house_status", "get_network_inventory", "get_room_context", "list_rooms",
    "query_occupancy_history",
]


def test_build_mcp_server_registers_all_tools_without_raising():
    # Regression lock (would have caught the shadowing bug): the real server must build
    # and expose exactly the 9 tools with byte-identical names (host wire contract).
    server = build_mcp_server(FakeProvider([], {}, {}))
    names = sorted(t.name for t in anyio.run(server.list_tools))
    assert names == _EXPECTED_TOOLS


def test_build_mcp_server_control_tool_registered_with_control_off():
    # Even with control DEFAULT-OFF the write tool is registered (but inert) -> the tool
    # surface is stable regardless of the control flag.
    server = build_mcp_server(FakeProvider([], {}, {}), control_enabled=False)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert "call_ha_service" in names


def test_build_mcp_server_whole_house_tools_round_trip():
    # Real MCP round-trip (mirrors test_end_to_end_client_session_reads_live_rooms
    # below) for the Phase 2A / B1-B3 whole-house tools, each wired with a real
    # provider so the wire contract (tool name + param names + return shape) is
    # exercised end-to-end, not just the plain function underneath.
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    import json

    # mac/extra_server are the PII/topology fields FIX 1/3 minimize away -- the
    # round-trip below asserts the WIRE response reflects that (not just the
    # plain-function unit tests above).
    devices = [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5", "vendor": "unknown",
               "device_type": "router", "type_confidence": "low", "known": True}]
    alerts = [{"kind": "rogue_dhcp", "severity": "alert", "ts": "2026-07-10T10:00:00+00:00",
               "extra_server": "192.168.1.99"}]
    occ = FakeOccupancyProvider(timeline_out=[{"room": "sala", "occupied": True,
                                              "person_count": 1, "confidence": 0.8,
                                              "ts": "2026-07-10T09:00:00+00:00"}])

    async def fake_house_status(window_minutes):
        return {"status": "ok", "score": 0, "reasons": [], "ts": "2026-07-10T10:00:00+00:00"}

    server = build_mcp_server(
        FakeProvider([], {}, {}), control_enabled=False,
        network_inventory_fn=lambda: devices, alerts_fn=lambda: alerts,
        occupancy_provider=occ, house_status_fn=fake_house_status)

    async def go():
        async with connect(server) as session:
            inv = await session.call_tool("get_network_inventory", {})
            assert json.loads(inv.content[0].text) == {"devices": [{
                "ip": "192.168.1.5", "vendor": "unknown", "device_type": "router",
                "type_confidence": "low", "known": True}], "count": 1}

            al = await session.call_tool("get_alerts", {})
            assert json.loads(al.content[0].text) == {"alerts": [{
                "kind": "rogue_dhcp", "severity": "alert", "room": None,
                "ts": "2026-07-10T10:00:00+00:00"}]}

            hist = await session.call_tool("query_occupancy_history", {"hours": 12})
            payload = json.loads(hist.content[0].text)
            assert payload["enabled"] is True and payload["history"]

            status = await session.call_tool("get_house_status", {})
            assert json.loads(status.content[0].text)["status"] == "ok"
    anyio.run(go)


def test_tool_wrapper_clamps_occupancy_history_hours_to_24():
    # Phase-2A verify FIX 2 (HIGH): the MCP tool wrapper (not the shared plain
    # function, which keeps its own ~1yr defensive backstop) clamps the
    # agent-reachable window to _AGENT_OCCUPANCY_MAX_HOURS -- a real MCP
    # round-trip asking for 30 days must only ever see a 24h-wide query.
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    import json
    from datetime import datetime, timedelta, timezone
    from wavr.mcp import _AGENT_OCCUPANCY_MAX_HOURS

    occ = FakeOccupancyProvider()
    server = build_mcp_server(FakeProvider([], {}, {}), occupancy_provider=occ)

    async def go():
        async with connect(server) as session:
            res = await session.call_tool(
                "query_occupancy_history", {"hours": 24 * 30})
            assert json.loads(res.content[0].text)["enabled"] is True
    anyio.run(go)
    assert occ.timeline_calls[0]["start"] is not None
    start = datetime.fromisoformat(occ.timeline_calls[0]["start"])
    now = datetime.now(timezone.utc)
    expected = now - timedelta(hours=_AGENT_OCCUPANCY_MAX_HOURS)
    assert abs((start - expected).total_seconds()) < 5   # ~24h, not ~30 days


# --- LocalApiStateProvider bridge (injected fake fetch, zero network) --------------

def test_local_api_state_provider_bridges_running_app():
    from wavr.mcp_serve import LocalApiStateProvider

    state = {
        "sala": {"room": "sala", "occupied": True, "confidence": 0.72,
                 "vitals": {"breathing_bpm": 14, "heart_bpm": 68},
                 "targets": [{"x": 1.2, "y": 3.4, "id": "p1"}],
                 "sources": [{"modality": "mmwave", "presence": True, "confidence": 0.9}],
                 "explanation": "mmwave: presente -> 72% ocupado"},
        "quarto": {"room": "quarto", "occupied": False, "confidence": 0.1},
    }
    house = {"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}]}

    def fake_fetch(path):
        if path == "/api/state":
            import json
            return json.dumps(state).encode()
        if path == "/api/house":
            import json
            return json.dumps(house).encode()
        raise AssertionError(f"unexpected path {path}")

    provider = LocalApiStateProvider("http://127.0.0.1:8000", token="", fetch=fake_fetch)
    assert provider.list_rooms() == ["quarto", "sala"]          # sorted
    assert provider.room_state("sala")["occupied"] is True
    assert provider.room_state("void") is None
    assert provider.house_map() == house

    # list_rooms tool over the bridge -> only room-level fields, no biometrics.
    rooms = list_rooms(provider)
    assert {r["room"] for r in rooms} == {"sala", "quarto"}

    # Privacy invariant SURVIVES the bridge: get_room_context strips vitals/targets even
    # though /api/state carried them over loopback (audit CRITICAL-1).
    ctx = get_room_context(provider, "sala")
    assert "vitals" not in ctx and "targets" not in ctx
    assert ctx["room"] == "sala" and ctx["sources"][0]["modality"] == "mmwave"


def test_local_api_state_provider_empty_state_is_safe():
    from wavr.mcp_serve import LocalApiStateProvider
    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=lambda p: b"")
    assert provider.list_rooms() == []
    assert provider.room_state("sala") is None
    assert provider.house_map() == {}


def test_local_api_state_provider_sends_token_header():
    from wavr.mcp_serve import LocalApiStateProvider
    seen = {}

    def fake_fetch(path):
        return b"{}"

    # Prove the token wiring: with a token set, _urllib_get would attach X-Wavr-Token.
    # We can't hit the network, so assert the header construction directly.
    provider = LocalApiStateProvider("http://127.0.0.1:8000", token="s3cret", fetch=fake_fetch)
    import urllib.request
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)

        class _R:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return b"{}"
        return _R()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        # Use the real _urllib_get path (bypass the injected fetch) to check headers.
        provider._urllib_get("/api/state")
    finally:
        urllib.request.urlopen = orig
    # urllib title-cases header keys.
    assert captured["headers"].get("X-wavr-token") == "s3cret"


# --- LocalApiStateProvider bridge: whole-house tools (Phase 2A / B1-B3) -----------

def test_local_api_state_provider_bridges_inventory_and_alerts():
    from wavr.mcp_serve import LocalApiStateProvider
    import json

    devices = [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5", "device_type": "router"}]
    alerts = [{"kind": "rogue_dhcp", "severity": "alert", "ts": "t"}]

    def fake_fetch(path):
        if path == "/api/inventory":
            return json.dumps({"devices": devices}).encode()
        if path == "/api/alerts":
            return json.dumps({"alerts": alerts}).encode()
        raise AssertionError(f"unexpected path {path}")

    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=fake_fetch)
    assert provider.inventory() == devices
    assert provider.alerts() == alerts


def test_local_api_state_provider_bridges_occupancy_and_house_status():
    from wavr.mcp_serve import LocalApiStateProvider
    import json

    routine = {"room": "sala", "weeks": 4.0, "hours": []}
    unusual = {"unusual": True, "baseline_probability": 0.2, "samples": 5, "hour": 22}
    status = {"status": "ok", "score": 0, "reasons": [], "ts": "t"}
    seen_paths = []

    def fake_fetch(path):
        seen_paths.append(path)
        if path.startswith("/api/occupancy/history"):
            return json.dumps([{"room": "sala", "occupied": True, "person_count": 1,
                               "confidence": 0.8, "ts": "t"}]).encode()
        if path.startswith("/api/occupancy/routine"):
            return json.dumps(routine).encode()
        if path.startswith("/api/occupancy/unusual"):
            return json.dumps(unusual).encode()
        if path.startswith("/api/house-status"):
            return json.dumps(status).encode()
        raise AssertionError(f"unexpected path {path}")

    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=fake_fetch)
    hist = provider.timeline("sala", start="2026-07-09T00:00:00+00:00")
    assert hist[0]["room"] == "sala"
    assert "room=sala" in seen_paths[-1] and "start=" in seen_paths[-1]

    assert provider.routine("sala") == routine
    assert "room=sala" in seen_paths[-1] and "weeks=4.0" in seen_paths[-1]

    # occupied_now is NOT forwarded onto the wire -- the endpoint recomputes "current"
    # server-side (see LocalApiStateProvider.is_unusual's docstring).
    assert provider.is_unusual("sala", True) == unusual
    assert "occupied_now" not in seen_paths[-1]

    assert provider.house_status(30.0) == status
    assert "window_minutes=30.0" in seen_paths[-1]


def test_local_api_state_provider_degrades_gracefully_when_source_disabled():
    # Mirrors the real app: WAVR_OCCUPANCY_LOG=0 -> the route 503s. The bridge must
    # degrade to the honest empty/disabled default, never crash the MCP server.
    from wavr.mcp_serve import LocalApiStateProvider
    import urllib.error

    def raising_fetch(path):
        raise urllib.error.HTTPError(path, 503, "disabled", {}, None)

    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=raising_fetch)
    assert provider.inventory() == []
    assert provider.alerts() == []
    assert provider.timeline("sala") == []
    assert provider.routine("sala") == {"room": "sala", "weeks": 4.0, "hours": []}
    assert provider.is_unusual("sala", True) == {
        "unusual": None, "baseline_probability": None, "samples": 0, "hour": None}
    assert provider.house_status() == {
        "status": "unknown", "score": 0, "reasons": [], "ts": None}


def test_local_api_state_provider_bridge_methods_degrade_on_empty_body():
    # A SECOND, DISTINCT degrade trigger inside `_json_safe` from the HTTP-error
    # test above: the fetch can succeed (no exception) but return an EMPTY body
    # (`_json()` -> None, e.g. a 200 with no content) -- `_json_safe` must land on
    # the exact same honest default via its `result is None` branch, not just its
    # `except Exception` branch. Both paths were previously collapsed into one
    # test that only ever exercised the exception branch.
    from wavr.mcp_serve import LocalApiStateProvider

    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=lambda path: b"")
    assert provider.inventory() == []
    assert provider.alerts() == []
    assert provider.timeline("sala") == []
    assert provider.routine("sala") == {"room": "sala", "weeks": 4.0, "hours": []}
    assert provider.is_unusual("sala", True) == {
        "unusual": None, "baseline_probability": None, "samples": 0, "hour": None}
    assert provider.house_status() == {
        "status": "unknown", "score": 0, "reasons": [], "ts": None}


# --- mcp_serve.make_server wiring --------------------------------------------------

class _FakeCfg:
    """Just the attributes make_server touches."""
    def __init__(self, **kw):
        self.port = kw.get("port", 8000)
        self.local_token = kw.get("local_token", "")
        self.db_path = kw.get("db_path", ":memory:")
        self.mcp_control = kw.get("mcp_control", False)
        self.ha_allowed_services = kw.get("ha_allowed_services", frozenset())
        self.ha_url = kw.get("ha_url", "")
        self.ha_token = kw.get("ha_token", "")


def test_make_server_wires_live_bridge_control_off(monkeypatch):
    monkeypatch.delenv("WAVR_MCP_TARGET", raising=False)
    from wavr import mcp_serve
    server = mcp_serve.make_server(cfg=_FakeCfg(mcp_control=False, ha_url="", ha_token=""))
    names = sorted(t.name for t in anyio.run(server.list_tools))
    assert names == _EXPECTED_TOOLS
    # HA unconfigured -> the read tool degrades to [].
    from wavr.mcp import get_ha_entities
    assert get_ha_entities(None) == []


def test_make_server_accepts_loopback_target_override(monkeypatch):
    # A loopback override (same box, different port) is fine -- the token stays on-box.
    monkeypatch.setenv("WAVR_MCP_TARGET", "http://127.0.0.1:9999")
    from wavr import mcp_serve
    server = mcp_serve.make_server(cfg=_FakeCfg(mcp_control=False, ha_url="", ha_token=""))
    names = sorted(t.name for t in anyio.run(server.list_tools))
    assert names == _EXPECTED_TOOLS


@pytest.mark.parametrize("target", [
    "http://192.168.1.50:8000",   # LAN peer -> would ship the local token off-box
    "https://evil.example.com",   # internet host
    "http://10.0.0.5:8000",       # private-but-off-box
])
def test_make_server_refuses_non_loopback_target(monkeypatch, target):
    # Fail-closed loopback guard: a non-loopback WAVR_MCP_TARGET must be refused BEFORE
    # any provider is built, so the same-box local-API token is never sent off the box
    # (loopback/stdio invariant). Operator-misconfig, not agent-reachable.
    monkeypatch.setenv("WAVR_MCP_TARGET", target)
    from wavr import mcp_serve
    with pytest.raises(ValueError, match="loopback"):
        mcp_serve.make_server(cfg=_FakeCfg(mcp_control=False, ha_url="", ha_token=""))


def test_is_loopback_target_classifies_hosts():
    from wavr.mcp_serve import _is_loopback_target
    # loopback -> True
    assert _is_loopback_target("http://127.0.0.1:8000")
    assert _is_loopback_target("http://127.5.5.5:8000")   # all of 127.0.0.0/8
    assert _is_loopback_target("http://localhost:8000")
    assert _is_loopback_target("http://[::1]:8000")
    # off-box / unparseable -> False (fail-closed)
    assert not _is_loopback_target("http://192.168.1.50:8000")
    assert not _is_loopback_target("https://example.com")
    assert not _is_loopback_target("")
    assert not _is_loopback_target("not a url")


# --- REAL end-to-end connect (in-memory client <-> server) -------------------------

def test_end_to_end_client_session_reads_live_rooms():
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    from wavr.mcp_serve import LocalApiStateProvider

    import json
    state = {"sala": {"room": "sala", "occupied": True, "confidence": 0.8,
                      "vitals": {"breathing_bpm": 12}, "targets": [{"x": 1, "y": 2}]}}

    def fake_fetch(path):
        if path == "/api/state":
            return json.dumps(state).encode()
        if path == "/api/house":
            return b"{}"
        raise AssertionError(path)

    provider = LocalApiStateProvider("http://127.0.0.1:8000", fetch=fake_fetch)
    server = build_mcp_server(provider, name="wavr", control_enabled=False)

    async def go():
        async with connect(server) as session:
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == _EXPECTED_TOOLS

            res = await session.call_tool("list_rooms", {})
            assert res.isError is False
            assert res.structuredContent == {"result": [
                {"room": "sala", "occupied": True, "confidence": 0.8}]}

            # get_room_context over the real MCP round-trip still strips biometrics.
            ctx = await session.call_tool("get_room_context", {"room": "sala"})
            payload = json.loads(ctx.content[0].text)
            assert "vitals" not in payload and "targets" not in payload

            # Control tool DEFAULT-OFF: refuses without erroring.
            called = await session.call_tool(
                "call_ha_service",
                {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
            body = json.loads(called.content[0].text)
            assert body["ok"] is False and body["status"] == "control_disabled"

    anyio.run(go)
