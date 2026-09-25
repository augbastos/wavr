"""SpaceScene keeps house geometry separate from live-only personal signals."""
import asyncio
import copy
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import SensingEvent, Target
from wavr.housemap import SAMPLE_MAP
from wavr.space_scene import scene
from wavr.storage import Storage


def _forbidden_keys(value):
    forbidden = {"x", "y", "targets", "identities", "vitals", "person",
                 "heart_bpm", "breathing_bpm"}
    if isinstance(value, dict):
        return (set(value) & forbidden).union(*(_forbidden_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_forbidden_keys(v) for v in value))
    return set()


def test_the_safety_walker_catches_a_planted_person_position():
    assert _forbidden_keys({"rooms": [{"targets": [{"x": 1, "y": 2}]}]}) == {
        "targets", "x", "y"}


def test_a_house_polygon_draws_in_house_units_and_unknown_is_not_empty():
    house = copy.deepcopy(SAMPLE_MAP)
    house["floors"].append({"id": "basement", "name": "Basement", "level": -1,
                            "rooms": [{"id": "store", "name": "store",
                                       "polygon": [[-2, -1], [0, -1], [0, 1]]}]})
    result = scene(house, {"sala": {"occupied": False, "confidence": 0.2,
                                    "person_count": 0, "precision_level": "room",
                                    "targets": [{"x": 1, "y": 2}],
                                    "identities": [{"person": "someone"}],
                                    "vitals": {"heart_bpm": 72}}},
                   attention={"items": [
                       # The room is a title argument; `where` names a screen.
                       {"where": "devices", "title_args": {"room": "sala"}},
                       {"where": "quarto"},                       # a screen name that happens to match
                       {"where": "devices", "title_args": {"room": "nowhere"}}]},
                   attention_missed=["pairings"])
    assert result["schema"] == 1
    assert result["levels"] == [{"level": -1, "name": "Basement"},
                                {"level": 0, "name": "Ground floor"}]
    assert [r["room"] for r in result["rooms"]] == [
        "store", "quarto", "quintal", "sala"]
    sala = result["rooms"][-1]
    assert sala["polygon"] == SAMPLE_MAP["floors"][0]["rooms"][0]["polygon"]
    assert sala["centroid"] == [2.0, 1.5]
    assert (sala["occupied"], sala["confidence"], sala["person_count"]) == (
        False, 0.2, 0)
    assert result["rooms"][1]["occupied"] is None
    assert result["rooms"][1]["confidence"] is None
    assert result["bounds"]["0"] == {"min_x": 0.0, "min_y": 0.0,
                                     "max_x": 7.7, "max_y": 5.7}
    assert result["attention_rooms"] == ["sala"]
    assert result["attention_could_not_check"] == ["pairings"]
    assert result["unplaced_rooms"] == []
    assert _forbidden_keys(result) == set()


def test_a_bad_polygon_and_a_stateless_room_do_not_claim_placement():
    house = copy.deepcopy(SAMPLE_MAP)
    house["floors"][0]["rooms"][0]["polygon"] = [[0, 0], [1, 0]]
    house["floors"][0]["rooms"][1]["polygon"] = [[0, 0], [1, "bad"], [0, 1]]
    result = scene(house, {"sala": {"occupied": True}, "orphan": {"occupied": True}})
    assert [r["room"] for r in result["rooms"]] == ["quintal"]
    assert result["unplaced_rooms"] == ["orphan", "quarto", "sala"]
    assert result["rooms"][0]["occupied"] is None


def test_garbled_room_verdicts_become_unknown():
    result = scene(SAMPLE_MAP, {"sala": {"occupied": "false", "confidence": "0.9",
                                         "person_count": True, "watch": "false",
                                         "health": {"state": "ok"}}})
    sala = next(r for r in result["rooms"] if r["room"] == "sala")
    assert all(sala[k] is None for k in ("occupied", "confidence", "person_count"))
    # Watch only ever SETS the flag: anything but a real True is "not flagged".
    assert sala["watch"] is False
    assert "health" not in sala   # RoomState has no room-level health to report


def test_scene_route_shows_the_same_room_verdicts_as_state(tmp_path, monkeypatch):
    house_path = tmp_path / "house.json"
    import json
    house_path.write_text(json.dumps(SAMPLE_MAP), encoding="utf-8")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(house_path))
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "core.db"))
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"), health_resolvers={})
    with TestClient(app) as client:
        event = SensingEvent(room="sala", modality="camera", presence=True,
                             motion=1.0, breathing_bpm=None, heart_bpm=None,
                             confidence=0.9, ts=datetime.now(timezone.utc).isoformat(),
                             targets=(Target(id=1, x=1.0, y=2.0),), count=1)
        asyncio.run(app.state.ingest(event))
        state = client.get("/api/state")
        response = client.get("/api/scene")
        assert state.status_code == response.status_code == 200
        sala = next(r for r in response.json()["rooms"] if r["room"] == "sala")
        assert {k: sala[k] for k in ("occupied", "confidence", "person_count",
                                    "precision_level")} == {
            k: state.json()["sala"].get(k) for k in (
                "occupied", "confidence", "person_count", "precision_level")}
        assert _forbidden_keys(response.json()) == set()

        assert client.post("/api/watch", json={"on": True},
                           headers={"X-Wavr-Local": "1"}).status_code == 200
        watched_state = client.get("/api/state").json()["sala"]
        watched_scene = next(r for r in client.get("/api/scene").json()["rooms"]
                             if r["room"] == "sala")
        assert {k: watched_scene[k] for k in ("occupied", "confidence",
                                              "person_count", "precision_level")} == {
            k: watched_state.get(k) for k in ("occupied", "confidence",
                                           "person_count", "precision_level")}
        assert watched_scene["watch"] is (watched_state.get("watch") is True)
        assert _forbidden_keys(watched_scene) == set()


def test_an_unauthenticated_lan_caller_cannot_read_the_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"), health_resolvers={})
    with TestClient(app, client=("192.168.1.50", 4444)) as peer:
        assert peer.get("/api/state").status_code == 403
        assert peer.get("/api/scene").status_code == 403


def test_the_house_level_aggregate_is_not_an_unplaced_room():
    from wavr.events import HOUSE_ROOM
    result = scene(SAMPLE_MAP, {HOUSE_ROOM: {"occupied": True}, "orphan": {"occupied": False}})
    assert result["unplaced_rooms"] == ["orphan"]


def test_a_device_without_presence_read_cannot_read_the_scene(tmp_path, monkeypatch):
    """The gate, not just the middleware: a paired device that DOES authenticate
    but lacks presence:read (a guest, an agent) is refused /api/scene exactly
    as /api/state refuses it -- and a user token (the control) reads both."""
    from wavr.devices import DeviceStore
    db = str(tmp_path / "md.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    store = DeviceStore(db)
    try:
        tokens = {role: store.add(f"device-{role}", role)[1] for role in ("guest", "agent", "user")}
    finally:
        store.close()
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"), health_resolvers={})
    with TestClient(app, client=("192.168.1.50", 4444)) as peer:
        for role, token in tokens.items():
            auth = {"Authorization": f"Bearer {token}"}
            want = 200 if role == "user" else 403
            assert peer.get("/api/state", headers=auth).status_code == want, role
            assert peer.get("/api/scene", headers=auth).status_code == want, role
