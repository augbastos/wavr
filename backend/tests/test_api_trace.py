"""Recording is off, sanitised by default, and never runs the live engine.

The routes carry three product promises that a test has to hold, because each of
them fails silently and in the wrong direction:

  * recording OFF unless deliberately started — a movement log of somebody's
    home must never accumulate from a forgotten switch;
  * download SANITISED unless raw is asked for — a trace on a bug report should
    be safe by default, not by remembering;
  * replay into a FRESH engine — running a recording through the live one would
    inject the past into the present.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from wavr.api_trace import build_trace_router
from wavr.events import Identity, SensingEvent
from wavr.fusion import FusionEngine

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def ev(sec, present=True, identities=()):
    return SensingEvent(
        room="sala", modality="camera", presence=present, motion=0.0,
        breathing_bpm=14.0, heart_bpm=60.0,
        confidence=0.9 if present else 0.0,
        ts=(T0 + timedelta(seconds=sec)).isoformat(),
        sensor_id="hall-cam", identities=identities)


@pytest.fixture
def scene():
    state = {"recorder": None, "last": None}
    app = FastAPI()
    app.include_router(build_trace_router(
        state,
        engine_factory=lambda now_fn: FusionEngine(now_fn=now_fn),
        deps=[Depends(lambda: None)]))
    with TestClient(app) as c:
        yield c, state


def _record(state, *events):
    """Simulate the ingest hook."""
    for e in events:
        state["recorder"].record(e)


# -- Off by default ------------------------------------------------------------

def test_nothing_is_recording_until_asked(scene):
    c, state = scene
    body = c.get("/api/trace/status").json()
    assert body["recording"] is False and body["captured"] == 0
    assert state["recorder"] is None


def test_starting_says_plainly_what_is_being_captured(scene):
    """The operator is about to record where people are in their home. The
    response has to say so, not just return 200."""
    c, _ = scene
    body = c.post("/api/trace/start", json={"label": "kitchen radar drops"}).json()
    assert body["recording"] is True
    assert "where people are" in body["note"]


def test_stopping_summarises_what_was_captured(scene):
    c, state = scene
    c.post("/api/trace/start", json={"label": "test"})
    _record(state, ev(0), ev(5, present=False))
    body = c.post("/api/trace/stop").json()
    assert body["recording"] is False
    assert body["events"] == 2 and body["rooms"] == ["sala"]


def test_stopping_when_nothing_runs_is_not_an_error(scene):
    c, _ = scene
    assert c.post("/api/trace/stop").json() == {"recording": False, "captured": 0}


def test_starting_again_replaces_the_running_recording(scene):
    """An operator who forgot they were recording wants the new one — the old
    was accumulating a movement log they did not intend."""
    c, state = scene
    c.post("/api/trace/start", json={"label": "first"})
    _record(state, ev(0), ev(1))
    c.post("/api/trace/start", json={"label": "second"})
    assert len(state["recorder"]) == 0
    assert c.get("/api/trace/status").json()["label"] == "second"


# -- Sanitised by default ------------------------------------------------------

def test_download_is_sanitised_unless_raw_is_asked_for(scene):
    c, state = scene
    c.post("/api/trace/start")
    _record(state, ev(0, identities=(Identity(person="Sam", source="ble"),)))
    c.post("/api/trace/stop")

    clean = c.get("/api/trace/download").json()
    assert clean["sanitized"] is True
    assert "Sam" not in str(clean)
    assert clean["events"][0]["event"]["breathing_bpm"] is None


def test_the_raw_form_is_available_but_has_to_be_named(scene):
    """Available, because an operator debugging their own house may legitimately
    want it. Deliberate, because it carries people."""
    c, state = scene
    c.post("/api/trace/start")
    _record(state, ev(0, identities=(Identity(person="Sam", source="ble"),)))
    c.post("/api/trace/stop")

    raw = c.get("/api/trace/download", params={"raw": "true"}).json()
    assert raw["sanitized"] is False
    assert "Sam" in str(raw)


def test_positions_can_be_dropped_on_the_way_out(scene):
    c, state = scene
    c.post("/api/trace/start")
    _record(state, ev(0))
    c.post("/api/trace/stop")
    body = c.get("/api/trace/download",
                 params={"drop_positions": "true"}).json()
    assert "target positions" in body["sanitized_removed"]


def test_downloading_before_anything_was_recorded_is_a_404(scene):
    c, _ = scene
    assert c.get("/api/trace/download").status_code == 404


# -- Replay --------------------------------------------------------------------

def test_replay_returns_the_states_and_a_summary(scene):
    c, state = scene
    c.post("/api/trace/start")
    _record(state, ev(0), ev(3))
    c.post("/api/trace/stop")
    trace = c.get("/api/trace/download").json()

    body = c.post("/api/trace/replay", json=trace).json()
    assert len(body["states"]) == 2
    assert body["states"][-1]["occupied"] is True
    assert body["summary"]["events"] == 2


def test_replay_is_deterministic_across_calls(scene):
    c, state = scene
    c.post("/api/trace/start")
    _record(state, ev(0), ev(4, present=False), ev(8))
    c.post("/api/trace/stop")
    trace = c.get("/api/trace/download").json()

    a = c.post("/api/trace/replay", json=trace).json()["states"]
    b = c.post("/api/trace/replay", json=trace).json()["states"]
    assert a == b


def test_an_uploaded_trace_is_untrusted_input(scene):
    """Anyone who can reach this route can post arbitrary JSON. A malformed
    trace must be a 422, never a 500 that takes a worker with it."""
    c, _ = scene
    for junk in ({"events": "not-a-list"},
                 {"events": [{"event": {"ts": 5}}]},
                 {"events": [{"event": {"room": [], "modality": {}}}]}):
        assert c.post("/api/trace/replay", json=junk).status_code in (200, 422)


def test_replay_never_touches_the_live_engine(scene):
    """A fresh engine per call. Running a recording through the live one would
    inject the past into the present and make the dashboard report a house that
    no longer exists."""
    live = FusionEngine(now_fn=lambda: T0)
    seen = []

    app = FastAPI()
    state = {"recorder": None, "last": None}

    def factory(now_fn):
        e = FusionEngine(now_fn=now_fn)
        seen.append(e)
        return e

    app.include_router(build_trace_router(state, engine_factory=factory,
                                          deps=[Depends(lambda: None)]))
    with TestClient(app) as c:
        c.post("/api/trace/start")
        state["recorder"].record(ev(0))
        c.post("/api/trace/stop")
        trace = c.get("/api/trace/download").json()
        c.post("/api/trace/replay", json=trace)
        c.post("/api/trace/replay", json=trace)

    assert len(seen) == 2, "one fresh engine per replay"
    assert live not in seen


def test_replay_is_refused_cleanly_when_unavailable():
    app = FastAPI()
    app.include_router(build_trace_router({"recorder": None, "last": None},
                                          deps=[Depends(lambda: None)]))
    with TestClient(app) as c:
        assert c.post("/api/trace/replay", json={"events": []}).status_code == 503


# -- The gate ------------------------------------------------------------------

def test_the_routes_fail_closed_with_no_gate_wired():
    app = FastAPI()
    app.include_router(build_trace_router({"recorder": None, "last": None}))
    with TestClient(app) as c:
        assert c.get("/api/trace/status").status_code == 403
        assert c.post("/api/trace/start", json={}).status_code == 403
