"""App-level wiring + read routes for A4 house memory (wavr.occupancy_log): proves
`create_app` actually feeds published RoomStates into the log (not just that the module
works in isolation, covered by test_occupancy_log.py) and that /api/occupancy/* reads it."""
import time

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.occupancy_log import OccupancyLog
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage


def _client(occupancy_log=None, sources=None):
    app = create_app(
        sources=sources if sources is not None else [],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"),
        occupancy_log=occupancy_log,
    )
    return TestClient(app)


# ---- live wiring: a published RoomState actually lands in the log -----------------

def test_live_fusion_events_get_logged_into_occupancy_history():
    log = OccupancyLog(":memory:")
    with _client(occupancy_log=log,
                 sources=[("sim", lambda: SimulatedSource(interval=0.01), True)]) as client:
        time.sleep(0.5)
        r = client.get("/api/occupancy/history")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body
        assert set(body[0]) == {"room", "occupied", "person_count", "confidence", "ts"}
    # Also true straight off the injected store (not just the route) -- proves the
    # hook in app.py's `_publish`, not just that the route can read an empty log.
    assert log.rooms()


# ---- /api/occupancy/history ---------------------------------------------------------

def test_history_route_room_filter():
    log = OccupancyLog(":memory:")
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    log.append_if_changed("quarto", False, 0.1, None, "2026-07-01T10:00:00+00:00")
    with _client(occupancy_log=log) as client:
        r = client.get("/api/occupancy/history?room=sala")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1 and body[0]["room"] == "sala"


def test_history_route_503_when_disabled(monkeypatch):
    monkeypatch.setenv("WAVR_OCCUPANCY_LOG", "0")
    with _client() as client:
        r = client.get("/api/occupancy/history")
        assert r.status_code == 503


# ---- /api/occupancy/routine ----------------------------------------------------------

def test_routine_route_returns_24_hours():
    log = OccupancyLog(":memory:")
    log.append_if_changed("sala", True, 0.9, None, "2026-07-01T10:00:00+00:00")
    with _client(occupancy_log=log) as client:
        r = client.get("/api/occupancy/routine?room=sala&weeks=4")
        assert r.status_code == 200
        body = r.json()
        assert body["room"] == "sala"
        assert len(body["hours"]) == 24


def test_routine_route_requires_room_query_param():
    log = OccupancyLog(":memory:")
    with _client(occupancy_log=log) as client:
        r = client.get("/api/occupancy/routine")
        assert r.status_code == 422  # FastAPI's own required-query-param rejection


# ---- /api/occupancy/unusual ----------------------------------------------------------

def test_unusual_route_404s_for_a_room_with_no_live_state():
    log = OccupancyLog(":memory:")
    with _client(occupancy_log=log) as client:
        r = client.get("/api/occupancy/unusual?room=nonexistent")
        assert r.status_code == 404


def test_unusual_route_reads_live_state_and_baseline():
    log = OccupancyLog(":memory:")
    with _client(occupancy_log=log,
                 sources=[("sim", lambda: SimulatedSource(interval=0.01), True)]) as client:
        time.sleep(0.5)
        r = client.get("/api/occupancy/unusual?room=sala")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"unusual", "baseline_probability", "samples", "hour"}
        # Freshly-seeded room, essentially no history yet -> an honest "don't know".
        assert body["unusual"] is None
