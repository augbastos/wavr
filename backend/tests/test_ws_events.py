"""The semantic event stream an application subscribes to.

Two properties matter here and the rest is plumbing: it carries nothing about
anybody, and it is gated exactly as hard as the stream that does.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage


def _app(monkeypatch, tmp_path, **kw):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    return create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                      fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                      health_resolvers={}, health_check=lambda: True, **kw)


def _sense(room, present, ts, count=None):
    return SensingEvent(sensor_id="cam1", room=room, modality="camera",
                        presence=present, motion=0.0, breathing_bpm=None,
                        heart_bpm=None, confidence=0.9 if present else 0.0,
                        ts=ts, count=count)


def test_a_change_reaches_a_subscribed_application(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        ingest = app.state.ingest
        # First reading establishes the baseline; the first sighting of a room
        # deliberately emits nothing.
        c.portal.call(ingest, _sense("kitchen", False, "2026-09-04T12:00:00+00:00"))
        with c.websocket_connect("/ws/events") as ws:
            c.portal.call(ingest,
                          _sense("kitchen", True, "2026-09-04T12:00:05+00:00"))
            ev = ws.receive_json()
            assert ev["event"] == "room.occupancy_changed"
            assert ev["room"] == "kitchen" and ev["occupied"] is True


def test_the_stream_carries_nothing_about_a_person(monkeypatch, tmp_path):
    """The reason this is a separate socket from /ws/live. A single hub would
    mean every application subscribing to occupancy also received per-person
    geometry and vitals."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        ingest = app.state.ingest
        c.portal.call(ingest, _sense("kitchen", False, "2026-09-04T12:00:00+00:00"))
        with c.websocket_connect("/ws/events") as ws:
            c.portal.call(ingest,
                          _sense("kitchen", True, "2026-09-04T12:00:05+00:00", 2))
            seen = [ws.receive_json() for _ in range(2)]
        text = str(seen)
        for forbidden in ("identities", "targets", "breathing", "heart", "\"x\""):
            assert forbidden not in text


def test_a_reconnecting_client_gets_the_tail_it_missed(monkeypatch, tmp_path):
    """An application whose Wi-Fi dropped should not miss the change that
    happened while it was away."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        ingest = app.state.ingest
        c.portal.call(ingest, _sense("kitchen", False, "2026-09-04T12:00:00+00:00"))
        c.portal.call(ingest, _sense("kitchen", True, "2026-09-04T12:00:05+00:00"))
        with c.websocket_connect("/ws/events") as ws:
            assert ws.receive_json()["event"] == "room.occupancy_changed"


def test_since_bounds_the_replay(monkeypatch, tmp_path):
    """A client that knows where it got to asks for only what came after."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        ingest = app.state.ingest
        c.portal.call(ingest, _sense("kitchen", False, "2026-09-04T12:00:00+00:00"))
        c.portal.call(ingest, _sense("kitchen", True, "2026-09-04T12:00:05+00:00"))
        with c.websocket_connect(
                "/ws/events?since=2026-09-04T12:00:09+00:00") as ws:
            c.portal.call(ingest,
                          _sense("kitchen", False, "2026-09-04T12:00:10+00:00"))
            ev = ws.receive_json()
            assert ev["at"] == "2026-09-04T12:00:10+00:00"


def test_a_cross_site_page_cannot_open_the_stream(monkeypatch, tmp_path):
    """Same gate as /ws/live, from the same function — two copies of a WS gate
    drift, and a drifted one leaves a door open on one stream that the other
    one closed."""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/events",
                                     headers={"origin": "https://evil.example"}):
                pass


def test_the_token_gate_applies_to_the_event_stream_too(monkeypatch, tmp_path):
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cret")
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/events"):
                pass
        with c.websocket_connect("/ws/events",
                                 headers={"x-wavr-token": "s3cret"}) as ws:
            assert ws is not None
