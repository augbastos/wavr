"""Named places, and the four things an anchor is not allowed to imply.

The load-bearing test in this file is the first one: an anchor with no
coordinates is COMPLETE. Most homes will never own an AR headset, and a model
that treated a logical anchor as unfinished would make the whole feature dead
weight for nearly everybody who installs Wavr.
"""
import pytest

from wavr.anchors import (
    KIND_AREA, KIND_LOGICAL, KIND_POINT, ORIGIN_IMPORTED, ROOM_SLACK_M,
    AnchorError, AnchorStore, summarize,
)
from wavr.spatial_frames import FRAME_ROOM

# A 4 x 3 metre kitchen, in the room-local frame anchors are stored in.
KITCHEN = [[0, 0], [4, 0], [4, 3], [0, 3]]


def store():
    return AnchorStore(":memory:")


# -- A logical anchor is a whole anchor ----------------------------------------

def test_an_anchor_with_no_coordinates_is_complete():
    """"The kitchen counter" is enough to write an automation against, enough to
    show in a list, enough to attach an experience to."""
    a = store().create("Kitchen counter", "kitchen")
    assert a.kind == KIND_LOGICAL
    assert a.positioned is False
    d = a.to_dict()
    assert d["name"] == "Kitchen counter" and d["room"] == "kitchen"
    assert "x" not in d and "frame" not in d


def test_a_logical_anchor_never_keeps_a_coordinate_passed_by_mistake():
    """`positioned` disagreeing with what is stored is how a consumer ends up
    reading an x that is not there."""
    a = store().create("Sofa", "living", kind=KIND_LOGICAL, x=1.0, y=2.0)
    assert a.x is None and a.y is None


# -- Coordinates, when there are any -------------------------------------------

def test_a_point_anchor_states_its_frame():
    """A consumer reading x/y off an anchor and off a Target should not have to
    remember which of them carries a frame."""
    a = store().create("Counter", "kitchen", kind=KIND_POINT, x=1.2, y=0.4,
                       room_polygon=KITCHEN)
    d = a.to_dict()
    assert d["frame"] == FRAME_ROOM
    assert (d["x"], d["y"]) == (1.2, 0.4)
    assert a.positioned is True


def test_millimetres_arriving_where_metres_are_expected_are_refused():
    """The D-006 bug class: a value three orders of magnitude too large put radar
    targets in the wrong place AND let the precision ladder promote them to
    Wavr's highest confidence."""
    with pytest.raises(AnchorError, match="millimetres"):
        store().create("Counter", "kitchen", kind=KIND_POINT,
                       x=2400, y=1500, room_polygon=KITCHEN)


def test_an_anchor_on_a_wall_is_allowed():
    """A window, a door frame and a wall-mounted TV are all genuinely ON the
    boundary. A strict point-in-polygon test would reject the commonest case."""
    a = store().create("Window", "kitchen", kind=KIND_POINT,
                       x=4.0 + ROOM_SLACK_M / 2, y=1.5, room_polygon=KITCHEN)
    assert a.positioned


def test_without_a_floor_plan_there_is_nothing_to_validate_against():
    """Refusing a coordinate would punish the operator for a drawing they have
    not made yet."""
    a = store().create("Counter", "kitchen", kind=KIND_POINT, x=99, y=99)
    assert a.positioned


def test_a_point_needs_finite_numbers():
    for bad in (None, "over there", float("inf")):
        with pytest.raises(AnchorError):
            store().create("X", "kitchen", kind=KIND_POINT, x=bad, y=1.0)


def test_an_area_needs_at_least_three_points():
    with pytest.raises(AnchorError):
        store().create("Rug", "kitchen", kind=KIND_AREA, polygon=[[0, 0], [1, 1]])


def test_an_area_round_trips_through_storage():
    s = store()
    a = s.create("Rug", "kitchen", kind=KIND_AREA,
                 polygon=[[0.5, 0.5], [2.0, 0.5], [2.0, 2.0]],
                 room_polygon=KITCHEN)
    back = s.get(a.anchor_id)
    assert back.polygon == ((0.5, 0.5), (2.0, 0.5), (2.0, 2.0))
    assert back.to_dict()["frame"] == FRAME_ROOM


# -- Moving an anchor ----------------------------------------------------------

def test_moving_an_anchor_to_another_room_clears_its_coordinates():
    """They were metres from the OLD room's corner. Carrying them across would
    place the counter that far into the new room — a silent wrong answer instead
    of a visible missing one."""
    s = store()
    a = s.create("Counter", "kitchen", kind=KIND_POINT, x=1.0, y=1.0,
                 room_polygon=KITCHEN)
    moved = s.move(a.anchor_id, "living")
    assert moved.room == "living"
    assert moved.kind == KIND_LOGICAL and moved.x is None


def test_placing_an_anchor_cannot_also_relocate_it():
    """So a bad coordinate can never move the anchor to another room as well."""
    s = store()
    a = s.create("Counter", "kitchen")
    placed = s.place(a.anchor_id, 1.0, 1.0, room_polygon=KITCHEN)
    assert placed.room == "kitchen" and placed.kind == KIND_POINT


def test_placing_is_the_upgrade_path_from_logical():
    s = store()
    a = s.create("Counter", "kitchen")
    assert a.positioned is False
    assert s.place(a.anchor_id, 2.0, 1.0, room_polygon=KITCHEN).positioned is True


# -- External ids are identity, never position ---------------------------------

def test_binding_an_external_id_stores_no_pose():
    """ARKit's world origin is wherever that session started. Importing its
    transform as room metres would put the counter somewhere arbitrary with a
    vendor's name on the error."""
    s = store()
    a = s.create("Counter", "kitchen")
    m = s.bind(a.anchor_id, "arkit", "F1A2-BEEF")
    assert set(m) == {"anchor_id", "provider_id", "external_id", "origin"}
    stored = s.get(a.anchor_id)
    assert stored.x is None, "binding an external anchor placed nothing"
    assert stored.mappings[0]["external_id"] == "F1A2-BEEF"


def test_an_external_id_resolves_to_a_list_not_a_winner():
    """Two headsets can bind their own id to the same counter, and one id can be
    bound twice by mistake — returning the first would hide that behind a
    plausible answer."""
    s = store()
    a = s.create("Counter", "kitchen")
    b = s.create("Counter (again)", "kitchen")
    s.bind(a.anchor_id, "openxr", "SAME")
    s.bind(b.anchor_id, "openxr", "SAME")
    assert len(s.resolve("openxr", "SAME")) == 2


def test_who_bound_it_is_recorded():
    s = store()
    a = s.create("Counter", "kitchen")
    s.bind(a.anchor_id, "arkit", "X", origin=ORIGIN_IMPORTED)
    assert s.get(a.anchor_id).mappings[0]["origin"] == ORIGIN_IMPORTED


def test_binding_to_a_missing_anchor_is_refused():
    with pytest.raises(AnchorError):
        store().bind("anc_nope", "arkit", "X")


def test_deleting_an_anchor_takes_its_mappings_with_it():
    """An external id pointing at a deleted anchor resolves to nothing, and the
    next anchor to reuse it would inherit somebody else's headset session."""
    s = store()
    a = s.create("Counter", "kitchen")
    s.bind(a.anchor_id, "arkit", "X")
    s.delete(a.anchor_id)
    assert s.resolve("arkit", "X") == []


def test_unbinding_leaves_the_anchor_alone():
    s = store()
    a = s.create("Counter", "kitchen")
    s.bind(a.anchor_id, "arkit", "X")
    assert s.unbind(a.anchor_id, "arkit", "X") is True
    assert s.get(a.anchor_id) is not None


# -- An orphaned anchor is shown, not hidden -----------------------------------

def test_an_anchor_whose_room_disappeared_is_marked_not_dropped():
    """Silently dropping it destroys an operator's work; silently keeping it
    makes an application ask about a room that does not exist."""
    s = store()
    a = s.create("Counter", "kitchen")
    assert a.to_dict(known_rooms=["living"])["orphaned"] is True
    assert a.to_dict(known_rooms=["kitchen"])["orphaned"] is False


def test_the_summary_counts_the_orphans_and_says_so():
    s = store()
    s.create("Counter", "kitchen")
    s.create("Sofa", "living", kind=KIND_POINT, x=1.0, y=1.0)
    out = summarize(s.list(), ["living"])
    assert out["count"] == 2 and out["positioned"] == 1
    assert len(out["orphaned"]) == 1
    assert "no longer exists" in out["note"]


def test_the_summary_explains_that_logical_is_enough():
    out = summarize([], ["kitchen"])
    assert "without any" in out["note"]


# -- Housekeeping --------------------------------------------------------------

def test_a_nameless_anchor_is_refused():
    with pytest.raises(AnchorError):
        store().create("   ", "kitchen")


def test_an_anchor_without_a_room_is_refused():
    with pytest.raises(AnchorError):
        store().create("Counter", "")


def test_renaming_keeps_the_id_so_references_survive():
    s = store()
    a = s.create("Counter", "kitchen")
    assert s.rename(a.anchor_id, "Worktop").anchor_id == a.anchor_id


def test_anchors_can_be_listed_per_room():
    s = store()
    s.create("Counter", "kitchen")
    s.create("Sofa", "living")
    assert [a.name for a in s.list(room="kitchen")] == ["Counter"]
    assert sorted(s.by_room()) == ["kitchen", "living"]


def test_deleting_a_missing_anchor_is_false_not_an_error():
    assert store().delete("anc_nope") is False


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


def test_the_whole_anchor_workflow_over_http(monkeypatch, tmp_path):
    """Create, read, place, bind, resolve, delete — the path the mandate asks
    for end to end rather than a model with no workflow around it."""
    with _client(monkeypatch, tmp_path) as c:
        made = c.post("/api/anchors", json={"name": "Kitchen counter",
                                            "room": "kitchen"})
        assert made.status_code == 200
        aid = made.json()["anchor_id"]
        assert made.json()["positioned"] is False

        listed = c.get("/api/anchors").json()
        assert listed["count"] == 1 and listed["positioned"] == 0

        placed = c.post(f"/api/anchors/{aid}/place", json={"x": 1.0, "y": 0.5})
        assert placed.status_code == 200
        assert placed.json()["frame"] == "room"

        assert c.post(f"/api/anchors/{aid}/bind",
                      json={"provider_id": "arkit",
                            "external_id": "ABC-123"}).status_code == 200
        found = c.get("/api/anchors/resolve/arkit/ABC-123").json()
        assert [a["anchor_id"] for a in found["anchors"]] == [aid]

        assert c.delete(f"/api/anchors/{aid}").status_code == 200
        assert c.get("/api/anchors/resolve/arkit/ABC-123").json()["anchors"] == []


def test_a_units_mistake_is_a_400_with_an_explanation(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        # The default house map has real rooms with real polygons, so the units
        # check has geometry to work against.
        rooms = c.get("/api/house").json()
        room = rooms["floors"][0]["rooms"][0]["name"]
        r = c.post("/api/anchors", json={"name": "Bad", "room": room,
                                         "kind": "point", "x": 2400, "y": 1500})
        assert r.status_code == 400
        assert "millimetres" in r.json()["detail"]


def test_a_missing_anchor_is_a_404(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/anchors/anc_nope").status_code == 404
        assert c.post("/api/anchors/anc_nope/place",
                      json={"x": 1, "y": 1}).status_code == 404


def test_moving_an_anchor_says_the_coordinates_were_cleared(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        aid = c.post("/api/anchors", json={"name": "Counter",
                                           "room": "kitchen"}).json()["anchor_id"]
        c.post(f"/api/anchors/{aid}/place", json={"x": 1.0, "y": 1.0})
        moved = c.post(f"/api/anchors/{aid}/room", json={"room": "hall"}).json()
        assert moved["positioned"] is False
        assert "cleared" in moved["note"]
