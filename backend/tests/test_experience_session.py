"""Experience sessions, and the handoff Wavr deliberately does not perform.

The load-bearing tests are the absences: Wavr does not move application state, it
does not decide that an experience should follow somebody to a television, and a
session carries no people. Each of those is somewhere the convenient thing to
build would be a promise the platform cannot keep.
"""
import pytest

from wavr.experience_session import (
    EV_ROOM_CHANGED, EV_TARGET_AVAILABLE, EV_TARGET_LOST,
    IDLE_TTL_S, MAX_METADATA_CHARS, MAX_SESSIONS, SESSION_EVENTS, SessionError,
    SessionStore, targets_in,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def store(on_event=None):
    return SessionStore(clock=Clock(), on_event=on_event), None


def tv(room="living", device_id="tv", display=True, audio=None):
    return {"device_id": device_id, "name": "Living room TV", "room": room,
            "display": display, "audio": audio}


# -- What a session is not -----------------------------------------------------

def test_a_session_carries_no_people():
    """There is no spatial scope that grants identity, and adding one would be a
    different product with a different consent conversation."""
    s = SessionStore()
    session = s.open("recipe-app", room="kitchen", devices=["phone-1"])
    body = session.to_dict()
    assert "participants" not in body and "people" not in body
    assert body["devices"] == ["phone-1"], "devices, which the app already knew"


def test_wavr_says_out_loud_that_it_does_not_move_your_state():
    """Building something that looked like it did would be the worst kind of
    platform promise: the demo works, and then somebody ships an app that loses
    a user's half-finished form on the way to the television."""
    s = SessionStore()
    note = s.open("x").to_dict()["note"]
    assert "does not move your application's state" in note


def test_metadata_is_bounded_because_wavr_is_not_a_state_store():
    s = SessionStore()
    session = s.open("x")
    with pytest.raises(SessionError, match="not a state store"):
        s.set_metadata(session.session_id, {"blob": "x" * (MAX_METADATA_CHARS + 1)})
    with pytest.raises(SessionError):
        s.set_metadata(session.session_id, {f"k{i}": 1 for i in range(64)})


def test_metadata_is_stored_uninterpreted():
    """The moment Wavr reads a field it becomes a contract, and this one is
    explicitly not."""
    s = SessionStore()
    session = s.open("x")
    out = s.set_metadata(session.session_id, {"chapter": 4, "difficulty": "hard"})
    assert out.metadata == {"chapter": 4, "difficulty": "hard"}


def test_no_event_claims_a_person_moved():
    """Wavr does not know the user moved, or that the person in the new room is
    the same one."""
    for name in SESSION_EVENTS:
        assert "user" not in name and "person" not in name


def test_joining_a_session_is_not_an_authorization():
    """A session is not a gate. Checking pairing here would put a second, weaker
    one beside the real gate."""
    s = SessionStore()
    session = s.open("x")
    joined = s.join(session.session_id, "some-device-nobody-paired")
    assert "some-device-nobody-paired" in joined.devices


# -- Targets, and the tristate that shows up in front of a user ----------------

def test_only_devices_that_said_they_have_a_screen_are_offered():
    """A device that never sent a manifest reports `display: None`. Offering it
    would put "Continue on TV?" in front of somebody whose device has no
    screen."""
    found = targets_in("living", [
        tv(display=True, device_id="a"),
        tv(display=None, device_id="b"),
        tv(display=False, device_id="c"),
    ])
    assert [t["device_id"] for t in found] == ["a"]


def test_a_device_in_another_room_is_not_a_target():
    assert targets_in("living", [tv(room="kitchen")]) == ()


def test_a_target_says_what_it_can_do():
    found = targets_in("living", [tv(display=True, audio=True)])
    assert set(found[0]["can"]) == {"display", "audio"}


def test_the_target_vocabulary_matches_the_context_one():
    """So an application is not translating between two vocabularies that mean
    the same thing."""
    from wavr.experience import CAP_AUDIO, CAP_DISPLAY
    from wavr.experience_session import TARGET_AUDIO, TARGET_DISPLAY
    assert (TARGET_DISPLAY, TARGET_AUDIO) == (CAP_DISPLAY, CAP_AUDIO)


# -- Observing: the primitive, not the decision --------------------------------

def test_a_display_appearing_is_an_event_not_a_handoff():
    """Wavr reports that a display became available. Whether the experience
    should move there depends on what it IS — a recipe follows you to a screen,
    a private message does not."""
    seen = []
    s = SessionStore(on_event=seen.append)
    session = s.open("recipe-app", room="kitchen")
    s.observe(session.session_id, room="living", devices=[tv()])
    kinds = [e["event"] for e in seen]
    assert EV_ROOM_CHANGED in kinds and EV_TARGET_AVAILABLE in kinds
    for ev in seen:
        assert "handoff" not in str(ev).lower()
        assert "should" not in str(ev).lower()


def test_a_room_change_carries_where_it_came_from():
    s = SessionStore()
    session = s.open("x", room="kitchen")
    events = s.observe(session.session_id, room="hall", devices=[])
    moved = [e for e in events if e["event"] == EV_ROOM_CHANGED][0]
    assert moved["room"] == "hall" and moved["previous"] == "kitchen"


def test_a_target_going_away_is_an_event_too():
    s = SessionStore()
    session = s.open("x", room="living")
    s.observe(session.session_id, room="living", devices=[tv()])
    events = s.observe(session.session_id, room="living", devices=[])
    assert [e["event"] for e in events] == [EV_TARGET_LOST]


def test_an_unchanged_world_produces_nothing():
    """Otherwise every poll is an event and applications learn to ignore the
    stream."""
    s = SessionStore()
    session = s.open("x", room="living")
    s.observe(session.session_id, room="living", devices=[tv()])
    assert s.observe(session.session_id, room="living", devices=[tv()]) == []


def test_observing_a_missing_session_is_refused():
    with pytest.raises(SessionError):
        SessionStore().observe("ses_nope", room="x", devices=[])


def test_every_event_names_the_session_and_the_experience():
    """An application running two sessions needs to know which one changed."""
    s = SessionStore()
    session = s.open("recipe-app", room="kitchen")
    for ev in s.observe(session.session_id, room="living", devices=[tv()]):
        assert ev["session_id"] == session.session_id
        assert ev["experience_id"] == "recipe-app"


# -- Lifecycle -----------------------------------------------------------------

def test_a_session_needs_an_experience():
    with pytest.raises(SessionError, match="name the experience"):
        SessionStore().open("  ")


def test_an_idle_session_expires():
    """A crashed application should not sit in the list until the Core
    restarts."""
    clock = Clock()
    s = SessionStore(clock=clock)
    session = s.open("x")
    clock.advance(IDLE_TTL_S + 1)
    assert s.get(session.session_id) is None


def test_touching_a_session_keeps_it_alive():
    clock = Clock()
    s = SessionStore(clock=clock)
    session = s.open("x")
    for _ in range(3):
        clock.advance(IDLE_TTL_S - 1)
        assert s.touch(session.session_id) is not None
    assert s.get(session.session_id) is not None


def test_the_store_is_bounded_and_drops_the_stalest_first():
    """Reached only by something already misbehaving, and a session nobody has
    touched is the least likely to be in use."""
    clock = Clock()
    s = SessionStore(clock=clock)
    first = s.open("x")
    for _ in range(MAX_SESSIONS + 5):
        clock.advance(1)
        s.open("y")
    assert s.get(first.session_id) is None
    assert len(s.list()) <= MAX_SESSIONS


def test_closing_a_session_removes_it():
    s = SessionStore()
    session = s.open("x")
    assert s.close(session.session_id) is True
    assert s.get(session.session_id) is None
    assert s.close(session.session_id) is False


def test_sessions_do_not_survive_the_core():
    """A session belongs to a running application. Persisting them would create
    rows describing applications that stopped months ago."""
    import inspect

    from wavr import experience_session
    src = inspect.getsource(experience_session)
    assert "sqlite3" not in src and "CREATE TABLE" not in src


def test_there_is_no_session_scoped_copy_of_the_rooms_own_events():
    """A room's precision, occupancy and count already have events. A
    session-scoped duplicate would make an application choose between two
    sources of one truth, and eventually get a different answer from each."""
    assert not any("context" in name for name in SESSION_EVENTS)


# -- Over HTTP -----------------------------------------------------------------

def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_a_session_opens_reports_and_closes(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        opened = c.post("/api/experience/sessions",
                        json={"experience_id": "recipe-app", "room": room})
        assert opened.status_code == 200
        sid = opened.json()["session_id"]

        assert c.get(f"/api/experience/sessions/{sid}").status_code == 200

        moved = c.post(f"/api/experience/sessions/{sid}/observe",
                       json={"room": "hall"}).json()
        assert any(e["event"] == EV_ROOM_CHANGED for e in moved["events"])
        assert moved["room"] == "hall"

        assert c.delete(f"/api/experience/sessions/{sid}").status_code == 200
        assert c.get(f"/api/experience/sessions/{sid}").status_code == 404


def test_a_session_without_an_experience_is_a_400(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.post("/api/experience/sessions",
                      json={"experience_id": "  "}).status_code == 400


def test_a_device_can_join_a_session(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        sid = c.post("/api/experience/sessions",
                     json={"experience_id": "x"}).json()["session_id"]
        body = c.post(f"/api/experience/sessions/{sid}/devices",
                      json={"device_id": "phone-1"}).json()
        assert body["devices"] == ["phone-1"]


def test_session_events_reach_the_same_stream_as_the_spatial_ones(monkeypatch, tmp_path):
    """One socket for an application to hold open, not two."""
    with _client(monkeypatch, tmp_path) as c:
        sid = c.post("/api/experience/sessions",
                     json={"experience_id": "x", "room": "kitchen"}).json()["session_id"]
        c.post(f"/api/experience/sessions/{sid}/observe", json={"room": "hall"})
        tail = c.get("/api/events/recent").json()["events"]
        assert any(e.get("event") == EV_ROOM_CHANGED for e in tail)


def test_a_target_label_is_derived_even_when_the_caller_supplies_one():
    """The sibling of a leak that WAS live in `experience._visible_devices`.

    `targets_in` preferred a caller-supplied `label` over its own derived one,
    so the guarantee in its comment held only because today's single caller
    happens not to pass that key. A test pins the guarantee, not the accident:
    the same audit found this pattern three times in adjacent code, and the two
    that were live had both been read past by a reviewer.
    """
    targets = targets_in("kitchen", [
        {"device_id": "d1", "room": "kitchen", "display": True,
         "label": "Augusto's iPhone", "name": "Augusto's iPhone"}])
    assert len(targets) == 1
    label = targets[0]["label"]
    assert "Augusto" not in label, f"the pairing name reached an experience: {label}"
    assert "kitchen" in label.lower()
