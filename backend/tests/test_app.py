import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sources.simulated import SimulatedSource
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta


def build_client(client=None, device_meta=None, health_check=None):
    # `client`: optional (host, port) tuple forwarded to TestClient, which uses it
    # verbatim as scope["client"] for every request/websocket it issues. This lets
    # tests forge a non-loopback peer to exercise the *real* enforcement path
    # (middleware / route guard) instead of just the `_is_loopback` helper.
    # `device_meta`/`health_check`: injectable seams for the presence-report and
    # health-check routes -- keeps every test off the real db file / real network.
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=0.01), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"),
        device_meta=device_meta, health_check=health_check,
    )
    kwargs = {"client": client} if client is not None else {}
    return TestClient(app, **kwargs)


def test_history_returns_roomstate_list():
    with build_client() as client:
        import time; time.sleep(0.5)  # a rare empty result on a loaded box just means: re-run
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body
        assert set(body[0].keys()) == {"room", "occupied", "confidence", "sources", "explanation", "ts"}


def test_history_limit_is_clamped():
    # A negative limit means "no LIMIT" to SQLite (full-table dump); an absurdly large
    # one is still unbounded resource use. Both must be clamped to the [1, 1000] cap.
    with build_client() as client:
        import time; time.sleep(0.5)
        assert len(client.get("/api/history?limit=-1").json()) <= 1000
        assert len(client.get("/api/history?limit=999999").json()) <= 1000
        # a normal limit still behaves as a limit
        r = client.get("/api/history?limit=5")
        assert r.status_code == 200
        assert len(r.json()) <= 5


def test_ws_live_streams_roomstate():
    with build_client() as client:
        with client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
            assert "occupied" in msg and "explanation" in msg


def test_state_returns_latest_per_room():
    with build_client() as client:
        import time; time.sleep(0.5)
        r = client.get("/api/state")
        assert r.status_code == 200
        state = r.json()
        assert state  # at least one room
        any_room = next(iter(state.values()))
        assert set(any_room.keys()) == {"room", "occupied", "confidence", "vitals", "sources", "targets", "explanation", "ts"}


LOCAL = {"X-Wavr-Local": "1"}  # state-changing routes require this header (CSRF guard)


def test_system_toggle_off_then_on():
    with build_client() as client:
        assert client.get("/api/system").json()["running"] is True
        client.post("/api/system/toggle", json={"on": False}, headers=LOCAL)
        assert client.get("/api/system").json()["running"] is False
        client.post("/api/system/toggle", json={"on": True}, headers=LOCAL)
        assert client.get("/api/system").json()["running"] is True


def test_source_toggle_disables_named_source():
    with build_client() as client:
        client.post("/api/sources/sim/toggle", json={"enabled": False}, headers=LOCAL)
        sim = [s for s in client.get("/api/system").json()["sources"] if s["name"] == "sim"][0]
        assert sim["enabled"] is False


def test_unknown_source_returns_404():
    with build_client() as client:
        r = client.post("/api/sources/nope/toggle", json={"enabled": False}, headers=LOCAL)
        assert r.status_code == 404


def test_state_change_without_local_header_is_rejected():
    with build_client() as client:
        r = client.post("/api/system/toggle", json={"on": False})  # no X-Wavr-Local
        assert r.status_code == 403


def test_is_loopback_helper_rejects_non_loopback():
    from wavr.app import _is_loopback
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("testclient")
    assert not _is_loopback("192.168.1.50")
    assert not _is_loopback(None)


def test_root_serves_dashboard_html():
    with build_client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Wavr" in r.text  # stable marker from frontend/index.html (<title>)


def test_index_html_serves_same_shell_as_root():
    # H3 audit fix: sw.js precaches "./index.html" by name; without this route the
    # Cache.addAll precache 404s (all-or-nothing) and the SW never installs.
    with build_client() as client:
        r = client.get("/index.html")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Wavr" in r.text


def test_vendor_serves_self_hosted_threejs():
    # 3D house view: three.js is self-hosted under /vendor, same loopback gating as
    # every other route -- confirms the static mount is wired, not a 404.
    with build_client() as client:
        r = client.get("/vendor/three/build/three.module.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]


# --- Merge-gate regressions: exercise the wired-up enforcement, not just the helper ---

def test_non_loopback_http_peer_gets_403():
    # Forge scope["client"] to a LAN address so the request actually goes through
    # `loopback_only` (the middleware wired up in app.py), not `_is_loopback` in
    # isolation. TestClient's default peer ("testclient") is in the allowlist, so
    # without this forge the middleware would never be exercised by any test.
    with build_client(client=("192.168.1.50", 12345)) as client:
        r = client.get("/api/system")
        assert r.status_code == 403


def test_bad_host_header_returns_400():
    # TestClient's default Host ("testserver") is in TrustedHostMiddleware's
    # allowlist, so this is the only case that needs forcing.
    with build_client() as client:
        r = client.get("/api/system", headers={"Host": "evil.com"})
        assert r.status_code == 400


def test_get_house_returns_rooms():
    with build_client() as client:
        r = client.get("/api/house")
        assert r.status_code == 200
        house = r.json()
        # v2 structure: look for "sala" across all floors
        rooms = [room for floor in house.get("floors", []) for room in floor.get("rooms", [])]
        assert any(room["name"] == "sala" for room in rooms)


def test_ws_non_loopback_peer_closed_with_1008():
    # Same forged-peer technique as the HTTP 403 test, but through the WebSocket
    # route, which the http middleware does NOT cover (see app.py comment) — the
    # /ws/live handler does its own inline `_is_loopback` check and must close
    # with policy-violation code 1008 before accepting.
    with build_client(client=("192.168.1.50", 12345)) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/live"):
                pass
        assert exc_info.value.code == 1008


def test_camera_rtsp_url_scheme_restricted_to_rtsp():
    # rtsp_url is handed straight to cv2.VideoCapture -- a non-rtsp scheme (http://,
    # file://, ...) would let a caller reach an internal/metadata endpoint (SSRF) or
    # the local filesystem (LFI) via camera add. Only rtsp(s):// may pass.
    with build_client() as client:
        ssrf = client.post("/api/cameras", json={
            "name": "cam_ssrf", "room": "sala",
            "rtsp_url": "http://169.254.169.254/latest/meta-data/", "confidence": 0.5,
        }, headers=LOCAL)
        assert ssrf.status_code == 400

        lfi = client.post("/api/cameras", json={
            "name": "cam_lfi", "room": "sala",
            "rtsp_url": "file:///etc/passwd", "confidence": 0.5,
        }, headers=LOCAL)
        assert lfi.status_code == 400

        ok = client.post("/api/cameras", json={
            "name": "cam_ok", "room": "sala",
            "rtsp_url": "rtsp://10.0.0.5/stream", "confidence": 0.5,
        }, headers=LOCAL)
        assert ok.status_code == 200


# --- /healthz + /api/status ----------------------------------------------------

def test_healthz_returns_ok_and_version():
    from wavr import __version__
    with build_client() as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "version": __version__}


def test_status_shape_and_no_secrets():
    from wavr import __version__
    with build_client() as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"version", "sources", "features", "house", "internet"}
        assert body["version"] == __version__

        assert isinstance(body["sources"], list) and body["sources"]
        for s in body["sources"]:
            assert set(s) == {"name", "active"}
        assert any(s["name"] == "sim" for s in body["sources"])

        expected_features = {
            "multidevice", "mqtt", "ha_discovery", "mcp_control",
            "narrate", "net_inventory", "tls", "ntfy", "internet_monitor",
        }
        assert set(body["features"]) == expected_features
        assert all(isinstance(v, bool) for v in body["features"].values())

        assert set(body["house"]) == {"floors", "rooms"}
        assert body["house"]["floors"] >= 1
        assert body["house"]["rooms"] >= 1

        # internet monitor off by default -> null/null (Feature B contract)
        assert body["internet"] == {"ok": None, "since": None}

        # NO SECRETS: grep the raw JSON text for anything token/credential/MAC/rtsp shaped.
        raw = json.dumps(body).lower()
        for secret_marker in ("token", "ha_token", "ha_url", "mac", "rtsp", "password", "secret"):
            assert secret_marker not in raw


def test_status_features_reflect_config_defaults(monkeypatch):
    for var in ("WAVR_MULTIDEVICE", "WAVR_MQTT_ENABLED", "WAVR_HA_DISCOVERY",
                "WAVR_MCP_CONTROL", "WAVR_NARRATE_ENABLED", "WAVR_NET_INVENTORY",
                "WAVR_NTFY_URL", "WAVR_INTERNET_MONITOR"):
        monkeypatch.delenv(var, raising=False)
    with build_client() as client:
        r = client.get("/api/status")
        features = r.json()["features"]
        assert features == {
            "multidevice": False, "mqtt": False, "ha_discovery": False,
            "mcp_control": False, "narrate": False, "net_inventory": False,
            "tls": False, "ntfy": False, "internet_monitor": False,
        }


def test_status_house_counts_match_default_map(monkeypatch):
    monkeypatch.delenv("WAVR_HOUSE_MAP", raising=False)   # unset -> DEFAULT_MAP (1 floor, 3 rooms)
    with build_client() as client:
        assert client.get("/api/status").json()["house"] == {"floors": 1, "rooms": 3}


def test_status_source_list_matches_system_endpoint():
    with build_client() as client:
        system_sources = {s["name"]: s["active"] for s in client.get("/api/system").json()["sources"]}
        status_sources = {s["name"]: s["active"] for s in client.get("/api/status").json()["sources"]}
        assert status_sources == system_sources


# --- /api/presence/report -------------------------------------------------------

def test_presence_report_shape_on_empty_store():
    dm = DeviceMeta(":memory:")
    with build_client(device_meta=dm) as client:
        r = client.get("/api/presence/report")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "generated_at", "device_count", "first_activity_at", "last_activity_at",
            "quiet_period_seconds", "currently_present", "recently_away", "stale",
            "most_present",
        }
        assert body["device_count"] == 0
        assert body["currently_present"] == []


def test_presence_report_reflects_device_meta_sightings():
    dm = DeviceMeta(":memory:")
    dm.seen("a4:83:e7:11:22:33")
    dm.set_name("a4:83:e7:11:22:33", "MacBook")
    with build_client(device_meta=dm) as client:
        body = client.get("/api/presence/report").json()
        assert body["device_count"] == 1
        assert body["currently_present"][0]["mac"] == "a4:83:e7:11:22:33"
        assert body["currently_present"][0]["name"] == "MacBook"


def test_presence_report_get_requires_no_local_header():
    # Read-only route -- no CSRF/X-Wavr-Local needed, same as every other GET.
    dm = DeviceMeta(":memory:")
    with build_client(device_meta=dm) as client:
        assert client.get("/api/presence/report").status_code == 200


# --- /api/health -----------------------------------------------------------------

async def _fake_health_up() -> bool:
    return True


async def _fake_health_down() -> bool:
    return False


def test_health_shape_and_gateway_reachable():
    with build_client(health_check=_fake_health_up) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"gateway", "internet_monitor"}
        assert set(body["gateway"]) == {"ok", "host"}
        assert body["gateway"]["ok"] is True
        # internet_monitor is off by default (Feature B contract, same as /api/status)
        assert body["internet_monitor"] is None


def test_health_reports_gateway_down():
    with build_client(health_check=_fake_health_down) as client:
        body = client.get("/api/health").json()
        assert body["gateway"]["ok"] is False


def test_health_never_triggers_real_network_when_injected():
    # The injected checker never touches a socket -- confirms the route uses
    # the injected transport (not the real ping) when one is provided.
    calls = []

    async def spy() -> bool:
        calls.append(1)
        return True

    with build_client(health_check=spy) as client:
        client.get("/api/health")
        client.get("/api/health")
    assert len(calls) == 2   # on-demand: one real call per GET, no caching
