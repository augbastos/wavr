import importlib

import pytest

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.mcp import (
    FusionStateProvider,
    build_mcp_server,
    get_house_map,
    get_room_context,
    list_rooms,
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
          "sources": [{"modality": "mmwave", "presence": True, "confidence": 0.9}],
          "explanation": "mmwave: presente -> 72% ocupado"}
    ctx = get_room_context(FakeProvider(["sala"], {"sala": rs}, HOUSE), "sala")
    assert "vitals" not in ctx
    assert "targets" not in ctx
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


def test_get_house_map_passthrough():
    assert get_house_map(FakeProvider([], {}, HOUSE)) == HOUSE


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

    assert get_house_map(provider) == HOUSE


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
    "call_ha_service", "get_ha_entities", "get_house_map",
    "get_room_context", "list_rooms",
]


def test_build_mcp_server_registers_all_tools_without_raising():
    # Regression lock (would have caught the shadowing bug): the real server must build
    # and expose exactly the 5 tools with byte-identical names (host wire contract).
    server = build_mcp_server(FakeProvider([], {}, {}))
    names = sorted(t.name for t in anyio.run(server.list_tools))
    assert names == _EXPECTED_TOOLS


def test_build_mcp_server_control_tool_registered_with_control_off():
    # Even with control DEFAULT-OFF the write tool is registered (but inert) -> the tool
    # surface is stable regardless of the control flag.
    server = build_mcp_server(FakeProvider([], {}, {}), control_enabled=False)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert "call_ha_service" in names


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
