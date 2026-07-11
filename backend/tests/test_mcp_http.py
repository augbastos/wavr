"""MCP-over-streamable-HTTP mount tests (ADR-0008, Slice 1: secure read transport).

Negative-path first (the mount opens an inbound network listener on a public AGPL repo):
unpaired -> 403, out-of-subnet + valid token -> 403, revoked token -> 403, default-OFF
kill-switch -> 503, bad Origin -> 403, rate-limit -> 429; call_ha_service ABSENT over
HTTP while the stdio bridge still exposes the full gated set.

Harness mirrors test_multidevice_integration: TestClient(app, client=(host,port)) forges a
non-loopback peer, and wavr.app._local_ipv4 is pinned so a fixed 192.168.1.x is in-subnet.
"""
import json

import anyio
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp.server.fastmcp")   # the mount needs the [mcp] extra

from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource
from wavr.devices import DeviceStore
from wavr.mcp import FusionStateProvider, build_mcp_server
from wavr.fusion import FusionEngine
from wavr.mcp_http import (
    _origin_ok, _RateLimiter, _extract_tool_call_names, build_mcp_http_mount,
    _buffer_body, _BodyTooLarge,
)
from wavr.auth import AGENT_ACTUATOR_TOOL_SCOPE, AGENT_READ_TOOL_SCOPE

CSRF = {"X-Wavr-Local": "1"}
MCP_HDRS = {"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}

_READ_TOOLS = {"list_rooms", "get_room_context", "get_house_map", "get_ha_entities",
              "get_network_inventory", "get_alerts", "query_occupancy_history",
              "get_house_status"}


class _FakeProvider:
    def list_rooms(self):
        return []

    def room_state(self, room):
        return None

    def house_map(self):
        return {}


# --------------------------------------------------------------------------- #
# Tool-set omission (server level -- authoritative for what the mount exposes).
# --------------------------------------------------------------------------- #
def test_http_server_omits_call_ha_service():
    # expose_control=False -> call_ha_service is ABSENT, not merely inert.
    server = build_mcp_server(_FakeProvider(), expose_control=False, stateless_http=True)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert names == _READ_TOOLS
    assert "call_ha_service" not in names


def test_stdio_server_still_exposes_full_set():
    # The stdio path (default expose_control=True) keeps the full gated toolset.
    server = build_mcp_server(_FakeProvider())
    names = {t.name for t in anyio.run(server.list_tools)}
    assert "call_ha_service" in names
    assert names == _READ_TOOLS | {"call_ha_service"}


def test_build_mcp_http_mount_returns_route_and_manager():
    route, sm = build_mcp_http_mount(
        FusionStateProvider(FusionEngine(), {}),
        is_enabled=lambda: True, local_ip="192.168.1.1")
    assert route.path == "/mcp"
    assert hasattr(sm, "handle_request") and hasattr(sm, "run")


# --------------------------------------------------------------------------- #
# Unit: Origin allowlist + rate limiter.
# --------------------------------------------------------------------------- #
def test_origin_ok_allowlist():
    assert _origin_ok(None, "192.168.1.1") is True                       # native client
    assert _origin_ok("https://192.168.1.1:8000", "192.168.1.1") is True  # central origin
    assert _origin_ok("http://localhost:5173", "192.168.1.1") is True
    assert _origin_ok("https://127.0.0.1", "192.168.1.1") is True
    assert _origin_ok("https://evil.example.com", "192.168.1.1") is False
    assert _origin_ok("https://192.168.1.99", "192.168.1.1") is False    # other LAN host
    assert _origin_ok("not-an-origin", "192.168.1.1") is False           # malformed


def test_rate_limiter_token_bucket():
    t = {"v": 0.0}
    rl = _RateLimiter(capacity=2, refill_per_sec=1.0, now_fn=lambda: t["v"])
    assert rl.allow("ip") is True    # new key: 1 token left
    assert rl.allow("ip") is True    # 0 left
    assert rl.allow("ip") is False   # denied
    assert rl.allow("other") is True  # independent bucket
    t["v"] = 1.0                     # +1 token refilled
    assert rl.allow("ip") is True
    assert rl.allow("ip") is False


# --------------------------------------------------------------------------- #
# Integration through the REAL create_app middleware + guard.
# --------------------------------------------------------------------------- #
@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def _enable(client, on=True):
    return client.post("/api/connectors/mcp-http/enable",
                       json={"enabled": on}, headers=CSRF)


def _pair(app, role="user"):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_app_wires_real_whole_house_data_sources_into_mcp_mount(tmp_path, monkeypatch):
    # High-value regression for the Phase 2A / B1-B3 app.py wiring: proves the
    # closures app.py hands to build_mcp_http_mount capture the RIGHT live objects
    # (network inventory service, alert monitors, occupancy log, house-status
    # composer) and return real data when invoked -- not just that the tool NAMES
    # are registered (covered by _READ_TOOLS above).
    captured = {}
    import wavr.mcp_http as mcp_http_module
    real_build_mount = mcp_http_module.build_mcp_http_mount

    def spy_build_mount(*args, **kwargs):
        captured.update(kwargs)
        return real_build_mount(*args, **kwargs)

    monkeypatch.setattr(mcp_http_module, "build_mcp_http_mount", spy_build_mount)
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wiring.db"))
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
              storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))

    assert callable(captured["network_inventory_fn"])
    assert captured["network_inventory_fn"]() == []       # no scan run yet -> honestly empty
    assert callable(captured["alerts_fn"])
    assert captured["alerts_fn"]() == []                   # nothing alerted -> honestly empty
    # WAVR_OCCUPANCY_LOG defaults ON -> a real OccupancyLog is wired (duck-typed, no
    # adapter). No rows logged yet in this fresh db -> an honestly empty timeline.
    assert captured["occupancy_provider"] is not None
    assert captured["occupancy_provider"].timeline(None) == []
    assert callable(captured["house_status_fn"])
    status = anyio.run(captured["house_status_fn"])
    assert status["status"] == "ok" and status["reasons"] == []   # nothing wrong -> composes clean


def test_mcp_not_mounted_when_multidevice_off(tmp_path, monkeypatch):
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "off.db"))
    off = create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                     storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    central = TestClient(off)   # loopback root
    assert central.post("/mcp", headers=MCP_HDRS, json=INIT).status_code == 404


def test_mcp_unpaired_lan_peer_403(app):
    # In-subnet peer with NO token -> 403 at loopback_or_authed, before any MCP dispatch.
    peer = TestClient(app, client=("192.168.1.50", 12345))
    assert peer.post("/mcp", headers=MCP_HDRS, json=INIT).status_code == 403


def test_mcp_out_of_subnet_valid_token_403(app):
    _peer, auth = _pair(app, "user")
    outsider = TestClient(app, client=("10.0.0.5", 9999))
    r = outsider.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
    assert r.status_code == 403       # out-of-subnet denied before token lookup


def test_mcp_revoked_token_403(app):
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
    assert r.status_code == 403


def test_mcp_disabled_returns_503(app):
    # Authed (loopback root) but the mcp-http connector is DEFAULT-OFF -> 503 kill-switch.
    central = TestClient(app)
    r = central.post("/mcp", headers=MCP_HDRS, json=INIT)
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"]


def test_mcp_bad_origin_403(app):
    central = TestClient(app)
    assert _enable(central, True).status_code == 200
    r = central.post("/mcp", headers={**MCP_HDRS, "Origin": "https://evil.example.com"},
                     json=INIT)
    assert r.status_code == 403
    assert "origin" in r.json()["detail"]


def test_mcp_enabled_reaches_dispatch(app):
    # Enabled + authed + good Origin -> reaches FastMCP; a real initialize returns 200.
    with TestClient(app) as central:
        assert _enable(central, True).status_code == 200
        r = central.post("/mcp", headers=MCP_HDRS, json=INIT)
        assert r.status_code == 200, r.text
        assert "serverInfo" in r.text


def test_mcp_kill_switch_revokes_live(app):
    # Per-request kill-switch: toggling the connector OFF 503s the very next request.
    with TestClient(app) as central:
        _enable(central, True)
        assert central.post("/mcp", headers=MCP_HDRS, json=INIT).status_code == 200
        _enable(central, False)
        assert central.post("/mcp", headers=MCP_HDRS, json=INIT).status_code == 503


# --------------------------------------------------------------------------- #
# Phase-2A verify FIX 5: the reserved 'mcp' route scope, ENFORCED for the first
# time (Gate 1.5). `user` -- DEFAULT_SCOPES deliberately excludes 'mcp' for it --
# is denied every /mcp tool merely by being an authenticated in-subnet device
# (the HIGH finding this closes); central/agent (DEFAULT_SCOPES include 'mcp')
# and the loopback root are unaffected.
# --------------------------------------------------------------------------- #
def test_mcp_user_role_denied_missing_mcp_scope(app):
    peer, auth = _pair(app, "user")
    with TestClient(app) as central:
        _enable(central, True)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        assert r.status_code == 403
        assert r.json()["detail"] == "missing scope: mcp"


def test_mcp_central_role_allowed_default_mcp_scope(app):
    peer, auth = _pair(app, "central")
    with TestClient(app) as central:
        _enable(central, True)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        assert r.status_code == 200, r.text


def test_mcp_agent_role_allowed_default_mcp_scope(tmp_path, monkeypatch):
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        assert r.status_code == 200, r.text


def test_mcp_loopback_root_allowed_bypasses_scope_check(app):
    # Loopback root's request.state.scopes is None (no Device row) -- must still
    # bypass the 'mcp' scope check the same way require_scope() does elsewhere.
    with TestClient(app) as central:
        _enable(central, True)
        r = central.post("/mcp", headers=MCP_HDRS, json=INIT)
        assert r.status_code == 200, r.text


def test_mcp_http_connector_default_off_and_toggles(app):
    central = TestClient(app)
    conns = {c["id"]: c for c in central.get("/api/connectors", headers=CSRF).json()["connectors"]}
    assert "mcp-http" in conns
    mh = conns["mcp-http"]
    assert mh["kind"] == "builtin" and mh["direction"] == "inbound"
    assert mh["enforcement"] == "registry-overlay"
    assert mh["available"] is True          # multidevice on + [mcp] present
    assert mh["active"] is False            # DEFAULT-OFF
    # enable -> active True
    _enable(central, True)
    after = {c["id"]: c for c in central.get("/api/connectors", headers=CSRF).json()["connectors"]}
    assert after["mcp-http"]["active"] is True
    # disable -> active False
    _enable(central, False)
    again = {c["id"]: c for c in central.get("/api/connectors", headers=CSRF).json()["connectors"]}
    assert again["mcp-http"]["active"] is False


def test_status_features_discloses_mcp_http(app):
    central = TestClient(app)
    feats = central.get("/api/status").json()["features"]
    assert "mcp_http" in feats
    assert feats["mcp_http"] is False       # default-off
    _enable(central, True)
    assert central.get("/api/status").json()["features"]["mcp_http"] is True


def test_mcp_rate_limited_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "rl.db"))
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    monkeypatch.setattr("wavr.mcp_http._RATE_CAPACITY", 2)
    monkeypatch.setattr("wavr.mcp_http._RATE_REFILL_PER_SEC", 0.0)
    app = create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                     storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    with TestClient(app) as central:
        _enable(central, True)
        codes = [central.post("/mcp", headers=MCP_HDRS, json=INIT).status_code
                 for _ in range(4)]
    assert codes[0] == 200 and codes[1] == 200      # burst of 2 allowed
    assert 429 in codes[2:]                          # then rate-limited


# --------------------------------------------------------------------------- #
# _extract_tool_call_names: pure parse, unit-level (gate 4.5's advisory parser).
# --------------------------------------------------------------------------- #
def _tool_call(name, arguments=None, id_=2):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {}}}


def test_extract_tool_call_names_single_message():
    assert _extract_tool_call_names(json.dumps(_tool_call("list_rooms")).encode()) == \
        ["list_rooms"]


def test_extract_tool_call_names_batch_array():
    batch = [_tool_call("list_rooms", id_=1), _tool_call("call_ha_service", id_=2)]
    assert _extract_tool_call_names(json.dumps(batch).encode()) == \
        ["list_rooms", "call_ha_service"]


def test_extract_tool_call_names_non_tool_call_methods_are_empty():
    assert _extract_tool_call_names(json.dumps(INIT).encode()) == []
    ping = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
    assert _extract_tool_call_names(json.dumps(ping).encode()) == []


def test_extract_tool_call_names_malformed_or_empty_body_is_empty():
    assert _extract_tool_call_names(b"not json{{{") == []
    assert _extract_tool_call_names(b"") == []
    assert _extract_tool_call_names(b'"just a string"') == []
    assert _extract_tool_call_names(json.dumps({"method": "tools/call"}).encode()) == []


# --------------------------------------------------------------------------- #
# _buffer_body size cap (audit MEDIUM): gate 4.5 is the ONE path that reads a whole
# body up front for a restricted agent principal -- must fail closed, not buffer
# without limit.
# --------------------------------------------------------------------------- #
def test_buffer_body_within_cap_returns_body_and_replay():
    chunks = [b'{"a":', b'1}']

    async def fake_receive():
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def run():
        body, replay = await _buffer_body(fake_receive, max_bytes=100)
        assert body == b'{"a":1}'
        # replay reproduces the SAME messages before falling through to fake_receive
        first = await replay()
        assert first["body"] == b'{"a":'

    anyio.run(run)


def test_buffer_body_over_cap_raises_body_too_large():
    chunks = [b"x" * 50, b"y" * 60]   # 110 bytes total

    async def fake_receive():
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def run():
        with pytest.raises(_BodyTooLarge):
            await _buffer_body(fake_receive, max_bytes=100)

    anyio.run(run)


def test_agent_oversized_tool_call_body_rejected_413_not_500(tmp_path, monkeypatch):
    # Integration: a tool-scope-restricted agent posting an oversized JSON-RPC body
    # (over the real production cap, _MAX_TOOL_CALL_BODY_BYTES) must get a clean 413
    # from gate 4.5's own guard -- never a 500 from an unbounded buffer / an
    # exception escaping into FastMCP/Starlette. (`_buffer_body`'s `max_bytes`
    # default is bound at import time, so this exercises the REAL 1 MB cap rather
    # than a monkeypatched one.)
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=frozenset({"list_rooms"}))
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        big_call = _tool_call("list_rooms", arguments={"padding": "x" * 1_100_000})
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=big_call)
        assert r.status_code == 413


# --------------------------------------------------------------------------- #
# Per-agent MCP tool scopes (Wavr Pass, Phase 2A / B4). Gate 4.5 must reject a
# tools/call whose name isn't in the calling agent's resolved tool_scopes BEFORE
# it ever reaches FastMCP. root/central/user are UNCHANGED (tool_scopes=None).
# --------------------------------------------------------------------------- #
def _agent_app(tmp_path, monkeypatch, tool_scopes=None):
    """Build a real app with ONE pre-seeded 'agent' device. Phase 2A / B4
    doesn't wire POST /api/pair-code to mint agent-role codes (an operator
    promotes an already-paired device via the EXISTING POST /api/devices/{id}/
    role instead) -- tests seed the device directly on the same db file BEFORE
    create_app opens its own DeviceStore, mirroring test_wavr_pass_scopes.py's
    own seed-store pattern. `tool_scopes=None` -> the least-privilege coarse
    default (auth.AGENT_DEFAULT_TOOL_SCOPE = list_rooms/get_room_context/
    get_house_status, via auth.effective_tool_scopes); get_house_map plus the
    four sensitive tools need an explicit grant (Phase-2B re-threat FIX 1).
    Returns `(app, bearer_headers)`."""
    db_path = str(tmp_path / "agent.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("mcp-agent", "agent", tool_scopes=tool_scopes)
    seed_store.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    return app, {"Authorization": f"Bearer {token}"}


def test_agent_default_scope_can_call_read_tools(tmp_path, monkeypatch):
    # tool_scopes=None at add() -> derives AGENT_DEFAULT_TOOL_SCOPE (coarse); list_rooms is in it.
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        assert peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT).status_code == 200
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("list_rooms"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["result"]["isError"] is False    # a REAL call reached the tool


def test_agent_default_scope_cannot_call_call_ha_service(tmp_path, monkeypatch):
    # The READ-ONLY default explicitly excludes call_ha_service -- refused by
    # gate 4.5 with a clean 403 (never even reaches FastMCP dispatch).
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(
            "call_ha_service",
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.x"}))
        assert r.status_code == 403
        assert "call_ha_service" in r.json()["detail"]


def test_agent_out_of_scope_tool_name_refused(tmp_path, monkeypatch):
    # A device granted a NARROWER explicit scope than the default: list_rooms only.
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=frozenset({"list_rooms"}))
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        ok = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("list_rooms"))
        assert ok.status_code == 200, ok.text
        denied = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("get_alerts"))
        assert denied.status_code == 403
        assert "get_alerts" in denied.json()["detail"]


def test_agent_explicit_empty_tool_scopes_denies_everything(tmp_path, monkeypatch):
    # An explicit EMPTY grant (deny-all) is distinct from the None default and
    # refuses even the most basic read tool.
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=frozenset())
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("list_rooms"))
        assert r.status_code == 403


def test_agent_batch_request_cannot_smuggle_a_disallowed_tool(tmp_path, monkeypatch):
    # A JSON-RPC batch (array of messages) mixing an allowed call with a
    # disallowed one must be refused wholesale -- gate 4.5 inspects every
    # message in the batch, not just the first.
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=frozenset({"list_rooms"}))
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        batch = [_tool_call("list_rooms", id_=2), _tool_call("get_alerts", id_=3)]
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=batch)
        assert r.status_code == 403
        assert "get_alerts" in r.json()["detail"]


def test_agent_actuator_scope_grant_still_absent_from_http_toolset(tmp_path, monkeypatch):
    # An explicit ACTUATOR grant (includes call_ha_service) passes gate 4.5 (the
    # NEW per-tool scope check) -- but call_ha_service is STILL absent from the
    # HTTP transport's registered tool set (expose_control=False, unconditional,
    # ADR-0008 "READ-ONLY BY CONSTRUCTION"), which this feature deliberately does
    # NOT touch. FastMCP itself refuses the unknown tool once dispatch is
    # reached -- proof the actuator bundle is defined + gate-enforced but not
    # wired live over HTTP, a STRONGER "off by default" than a scope check alone.
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=AGENT_ACTUATOR_TOOL_SCOPE)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(
            "call_ha_service",
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.x"}))
        assert r.status_code != 403               # NOT blocked by the scope gate...
        assert r.json()["result"]["isError"] is True   # ...but still unreachable (unknown tool)


def test_root_central_user_unaffected_by_tool_scope_axis(app):
    # root/central/user resolve tool_scopes=None ("not restricted by this axis") --
    # gate 4.5 is a total no-op for them; every already-exposed read tool remains
    # callable exactly as before this feature (regression proof, additive-only).
    with TestClient(app) as central:
        _enable(central, True)
        central.post("/mcp", headers=MCP_HDRS, json=INIT)
        r = central.post("/mcp", headers=MCP_HDRS, json=_tool_call("get_alerts"))
        assert r.status_code == 200, r.text
        assert r.json()["result"]["isError"] is False


# --------------------------------------------------------------------------- #
# Live end-to-end /mcp round trips for the Phase 2A / B1-B3 whole-house tools
# (get_network_inventory, query_occupancy_history, get_house_status) through the
# REAL create_app wiring. test_app_wires_real_whole_house_data_sources_into_mcp_mount
# above only invokes the captured closures DIRECTLY (never through FastMCP's own
# tool dispatch / param binding), and test_mcp.py's round-trip test uses a
# hand-built server + FakeOccupancyProvider, not the real app.py closures. These
# close that gap: a fresh app (no scan run, no history logged, no alerts fired)
# proves the empty/missing-data path is honest all the way through gate 4.5 +
# FastMCP + app.py's real `_inventory`/`_occupancy_log`/`_compute_house_status`.
# --------------------------------------------------------------------------- #
def _tool_result(resp_json: dict) -> dict:
    """Parse a JSON-RPC tools/call HTTP response's single text-content item back
    into the tool's own JSON return value (mirrors what a real MCP client does)."""
    return json.loads(resp_json["result"]["content"][0]["text"])


def test_mcp_get_network_inventory_live_round_trip_empty(app):
    with TestClient(app) as central:
        _enable(central, True)
        central.post("/mcp", headers=MCP_HDRS, json=INIT)
        r = central.post("/mcp", headers=MCP_HDRS, json=_tool_call("get_network_inventory"))
        assert r.status_code == 200, r.text
        assert r.json()["result"]["isError"] is False
        # no scan run yet -> honestly empty (FIX 1 also adds "count")
        assert _tool_result(r.json()) == {"devices": [], "count": 0}


def test_mcp_query_occupancy_history_live_round_trip_house_wide(app):
    with TestClient(app) as central:
        _enable(central, True)
        central.post("/mcp", headers=MCP_HDRS, json=INIT)
        r = central.post("/mcp", headers=MCP_HDRS, json=_tool_call("query_occupancy_history"))
        assert r.status_code == 200, r.text
        # WAVR_OCCUPANCY_LOG defaults ON -> enabled, but nothing logged yet in this
        # fresh db, and no `room` -> routine/unusual stay None (house-wide query).
        assert _tool_result(r.json()) == {
            "enabled": True, "history": [], "routine": None, "unusual": None}


def test_mcp_query_occupancy_history_live_round_trip_with_room(app):
    with TestClient(app) as central:
        _enable(central, True)
        central.post("/mcp", headers=MCP_HDRS, json=INIT)
        r = central.post("/mcp", headers=MCP_HDRS,
                         json=_tool_call("query_occupancy_history", {"room": "sala", "hours": 12}))
        assert r.status_code == 200, r.text
        payload = _tool_result(r.json())
        assert payload["enabled"] is True and payload["history"] == []
        assert payload["routine"]["room"] == "sala"   # a real (empty-samples) routine baseline
        # No current fused reading for "sala" in this fresh app -> unusual honestly None,
        # never a guessed/fabricated verdict.
        assert payload["unusual"] is None


def test_mcp_get_house_status_live_round_trip(app):
    with TestClient(app) as central:
        _enable(central, True)
        central.post("/mcp", headers=MCP_HDRS, json=INIT)
        r = central.post("/mcp", headers=MCP_HDRS, json=_tool_call("get_house_status"))
        assert r.status_code == 200, r.text
        payload = _tool_result(r.json())
        assert payload["status"] == "ok" and payload["reasons"] == []   # nothing wrong -> clean

        # `window_minutes` actually reaches app.py's real _compute_house_status closure
        # over the wire (FastMCP param binding), not just the plain-function unit test.
        r2 = central.post("/mcp", headers=MCP_HDRS,
                          json=_tool_call("get_house_status", {"window_minutes": 5}, id_=3))
        assert r2.status_code == 200, r2.text
        assert _tool_result(r2.json())["status"] == "ok"


# --------------------------------------------------------------------------- #
# Adversarial scope-refusal, extended (Phase-2A verify FIX 4 -- least-privilege
# default agent scope): the DEFAULT agent grant is now COARSE/current-state-only
# (list_rooms/get_room_context/get_house_status) -- it must reach every one of
# those live, and must be REFUSED (gate 4.5, before FastMCP dispatch) for the
# tools that now require an explicit admin grant: query_occupancy_history/
# get_network_inventory/get_alerts/get_ha_entities, plus get_house_map
# (Phase-2B re-threat FIX 1 -- MEDIUM: room `id` encodes the room name and it
# ships polygon geometry, i.e. the floor plan itself). A regression lock
# against MCP_TOOL_NAMES (auth.py) drifting from the @server.tool() names
# actually registered (mcp.py).
# --------------------------------------------------------------------------- #
def test_agent_default_scope_can_call_all_coarse_read_tools_live(tmp_path, monkeypatch):
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        for i, name in enumerate(
                ["get_house_status"], start=2):
            r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(name, id_=i))
            assert r.status_code == 200, (name, r.text)
            assert r.json()["result"]["isError"] is False, (name, r.json())


def test_agent_default_scope_denied_the_four_sensitive_read_tools_live(tmp_path, monkeypatch):
    # HIGH/MEDIUM finding this closes: query_occupancy_history/
    # get_network_inventory/get_alerts/get_ha_entities are the household PII/
    # tracking crown jewels (even after the mcp.py-side minimization narrows
    # each one's OWN field set) -- a default agent (no explicit tool_scopes
    # grant) must be refused by gate 4.5 BEFORE FastMCP dispatch, exactly like
    # call_ha_service already is.
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        for i, name in enumerate(
                ["get_network_inventory", "get_alerts", "query_occupancy_history",
                 "get_ha_entities"], start=2):
            r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(name, id_=i))
            assert r.status_code == 403, (name, r.text)
            assert name in r.json()["detail"]


def test_agent_default_scope_denied_get_house_map_live(tmp_path, monkeypatch):
    # Phase-2B re-threat FIX 1 (MEDIUM): unlike the four PII/tracking crown
    # jewels above, get_house_map's minimized shape (mcp.py FIX C) still ships
    # room `id` (which ENCODES the room name in every real house.json, e.g.
    # "cozinha"/"quarto-1") plus the room's polygon GEOMETRY -- the annotated
    # floor plan. A default (no explicit tool_scopes grant) agent must be
    # refused by gate 4.5 BEFORE FastMCP dispatch, same as the four above.
    app, auth = _agent_app(tmp_path, monkeypatch)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("get_house_map", id_=2))
        assert r.status_code == 403, r.text
        assert "get_house_map" in r.json()["detail"]


def test_agent_explicit_broad_grant_can_still_reach_all_read_tools_live(tmp_path, monkeypatch):
    # An operator who explicitly widens an agent to AGENT_READ_TOOL_SCOPE (every
    # read tool, unchanged from before FIX 4 -- see auth.py) can still reach the
    # four now-excluded-by-default tools; least-privilege is the DEFAULT, not a
    # hard cap.
    app, auth = _agent_app(tmp_path, monkeypatch, tool_scopes=AGENT_READ_TOOL_SCOPE)
    with TestClient(app) as central:
        _enable(central, True)
        peer = TestClient(app, client=("192.168.1.50", 12345))
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        for i, name in enumerate(
                ["get_network_inventory", "get_alerts", "query_occupancy_history",
                 "get_house_status", "get_house_map", "get_ha_entities"], start=2):
            r = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(name, id_=i))
            assert r.status_code == 200, (name, r.text)
            assert r.json()["result"]["isError"] is False, (name, r.json())


def test_agent_promoted_via_devices_role_route_can_call_mcp(app):
    # The ONLY provisioning path for 'agent' today is promoting an already-paired
    # device via POST /api/devices/{id}/role (test_wavr_pass_scopes.py's
    # test_agent_role_promoted_via_existing_devices_role_route proves the promoted
    # device loses ordinary API access). This is the other half: prove the SAME
    # promoted device's very next /mcp request resolves the READ-ONLY default
    # tool_scopes and can actually call a read tool, live -- every prior agent
    # test in this file seeds the device directly with role="agent" from the
    # start, never through the real promotion route.
    peer, auth = _pair(app, "user")
    with TestClient(app, headers=CSRF) as central:
        device_id = central.get("/api/devices").json()["devices"][0]["device_id"]
        r = central.post(f"/api/devices/{device_id}/role", json={"role": "agent"})
        assert r.status_code == 200 and r.json()["role"] == "agent"

        _enable(central, True)
        peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=INIT)
        ok = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call("list_rooms"))
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"]["isError"] is False

        # Still bounded exactly like a device seeded with role="agent" from the
        # start: the write tool is refused by gate 4.5 before FastMCP dispatch.
        denied = peer.post("/mcp", headers={**MCP_HDRS, **auth}, json=_tool_call(
            "call_ha_service",
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.x"}))
        assert denied.status_code == 403
