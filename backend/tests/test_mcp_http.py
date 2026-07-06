"""MCP-over-streamable-HTTP mount tests (ADR-0008, Slice 1: secure read transport).

Negative-path first (the mount opens an inbound network listener on a public AGPL repo):
unpaired -> 403, out-of-subnet + valid token -> 403, revoked token -> 403, default-OFF
kill-switch -> 503, bad Origin -> 403, rate-limit -> 429; call_ha_service ABSENT over
HTTP while the stdio bridge still exposes the full gated set.

Harness mirrors test_multidevice_integration: TestClient(app, client=(host,port)) forges a
non-loopback peer, and wavr.app._local_ipv4 is pinned so a fixed 192.168.1.x is in-subnet.
"""
import anyio
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp.server.fastmcp")   # the mount needs the [mcp] extra

from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource
from wavr.mcp import FusionStateProvider, build_mcp_server
from wavr.fusion import FusionEngine
from wavr.mcp_http import _origin_ok, _RateLimiter, build_mcp_http_mount

CSRF = {"X-Wavr-Local": "1"}
MCP_HDRS = {"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}

_READ_TOOLS = {"list_rooms", "get_room_context", "get_house_map", "get_ha_entities"}


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
