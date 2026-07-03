import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sources.simulated import SimulatedSource
from wavr.camera_store import CameraStore


def build_client(client=None):
    # `client`: optional (host, port) tuple forwarded to TestClient, which uses it
    # verbatim as scope["client"] for every request/websocket it issues. This lets
    # tests forge a non-loopback peer to exercise the *real* enforcement path
    # (middleware / route guard) instead of just the `_is_loopback` helper.
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=0.01), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"),
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
        assert "Fused Home Sensing" in r.text  # distinctive marker from frontend/index.html


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
