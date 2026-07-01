from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sources.simulated import SimulatedSource


def build_client():
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=0.01), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
    )
    return TestClient(app)


def test_history_returns_roomstate_list():
    with build_client() as client:
        import time; time.sleep(0.5)  # a rare empty result on a loaded box just means: re-run
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body
        assert set(body[0].keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}


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
        assert set(any_room.keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}


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
