"""Room adjacency, and the discipline of never inventing a person.

Topology's whole risk is that a plausible inference looks like an observation.
The tests that matter are the ones asserting it stays an annotation: it explains
and it flags, and it never puts anybody anywhere.
"""
import pytest

from wavr.topology import (
    DEFAULT_TOUCH_M, LINK_CUT, LINK_DECLARED, LINK_INFERRED, MAX_PLAUSIBLE_HOPS,
    TopologyStore, build_graph, derive_adjacency, describe, shortest_path,
    transition,
)


def _house(*rooms, level=0):
    """rooms = (name, x, y, w, h)"""
    return {"version": 2, "units": "m", "floors": [{
        "id": "f0", "name": "Ground", "level": level,
        "rooms": [{"id": f"r_{n}", "name": n,
                   "polygon": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]}
                  for n, x, y, w, h in rooms]}]}


# A gap of 0.2 m between rooms — an interior wall.
HOUSE = _house(("sala", 0, 0, 4, 3),
               ("hall", 4.2, 0, 1.5, 3),
               ("kitchen", 5.9, 0, 3, 3),
               ("shed", 20, 20, 2, 2))       # far away, connected to nothing


@pytest.fixture
def store(tmp_path):
    s = TopologyStore(str(tmp_path / "wavr.db"))
    yield s
    s.close()


# -- Deriving from the drawing ------------------------------------------------

def test_rooms_separated_by_a_wall_are_adjacent():
    """0.2 m apart is a wall, and a wall usually has a door."""
    adj = derive_adjacency(HOUSE)
    assert adj["sala"] == {"hall"}
    assert adj["hall"] == {"sala", "kitchen"}


def test_a_distant_room_is_adjacent_to_nothing():
    assert derive_adjacency(HOUSE)["shed"] == set()


def test_adjacency_is_symmetric():
    adj = derive_adjacency(HOUSE)
    for room, neighbours in adj.items():
        for n in neighbours:
            assert room in adj[n], f"{room}->{n} but not back"


def test_the_touch_distance_is_adjustable_and_actually_bites():
    tight = derive_adjacency(HOUSE, touch_m=0.1)
    assert tight["sala"] == set(), "0.2 m apart is beyond a 0.1 m tolerance"


def test_edges_are_measured_not_bounding_boxes():
    """An L-shaped room's bounding box can touch a room it shares no wall with.
    Inferring a door there would make an impossible movement look ordinary."""
    house = {"version": 2, "units": "m", "floors": [{
        "id": "f0", "level": 0, "rooms": [
            # An L: occupies the left column and the bottom row.
            {"id": "r_l", "name": "L", "polygon": [
                [0, 0], [2, 0], [2, 6], [8, 6], [8, 8], [0, 8]]},
            # Sits inside the L's bounding box, but 3 m from any of its edges.
            {"id": "r_far", "name": "far", "polygon": [
                [5, 0], [7, 0], [7, 2], [5, 2]]},
        ]}]}
    assert derive_adjacency(house)["L"] == set()


def test_rooms_on_different_floors_are_never_inferred_adjacent():
    """Two rooms stacked on different levels are not connected by being above
    each other. They are connected by stairs, which the polygon does not know."""
    house = {"version": 2, "units": "m", "floors": [
        {"id": "f0", "level": 0, "rooms": [
            {"id": "a", "name": "downstairs", "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]}]},
        {"id": "f1", "level": 1, "rooms": [
            {"id": "b", "name": "upstairs", "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]}]},
    ]}
    adj = derive_adjacency(house)
    assert adj["downstairs"] == set() and adj["upstairs"] == set()


def test_a_malformed_polygon_is_skipped_not_crashed():
    house = {"version": 2, "floors": [{"id": "f0", "level": 0, "rooms": [
        {"id": "ok", "name": "ok", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        {"id": "bad", "name": "bad", "polygon": "not-a-polygon"},
        {"id": "short", "name": "short", "polygon": [[0, 0], [1, 1]]},
    ]}]}
    assert set(derive_adjacency(house)) == {"ok"}


# -- The operator always wins -------------------------------------------------

def test_a_declared_link_beats_the_geometry(store):
    """Stairs. The polygons say nothing; the operator knows."""
    store.declare("shed", "sala", connected=True, note="side door")
    graph = build_graph(HOUSE, store.overrides())
    assert graph["shed"]["sala"] == LINK_DECLARED
    assert graph["sala"]["shed"] == LINK_DECLARED


def test_a_cut_link_removes_an_inferred_one(store):
    """A shared wall with no door. Geometry cannot tell; the operator can."""
    store.declare("sala", "hall", connected=False, note="solid wall")
    graph = build_graph(HOUSE, store.overrides())
    assert "hall" not in graph["sala"]
    assert "sala" not in graph["hall"]


def test_links_are_undirected_however_they_are_written(store):
    store.declare("hall", "sala", connected=False)
    graph = build_graph(HOUSE, store.overrides())
    assert "sala" not in graph["hall"] and "hall" not in graph["sala"]


def test_clearing_an_override_returns_to_the_geometry(store):
    store.declare("sala", "hall", connected=False)
    assert store.clear("sala", "hall") is True
    graph = build_graph(HOUSE, store.overrides())
    assert graph["sala"]["hall"] == LINK_INFERRED


def test_a_room_cannot_link_to_itself(store):
    with pytest.raises(ValueError):
        store.declare("sala", "sala", connected=True)


# -- Judging a movement -------------------------------------------------------

def test_adjacent_rooms_are_plausible_and_say_why():
    g = build_graph(HOUSE)
    out = transition(g, "sala", "hall")
    assert out["verdict"] == "adjacent" and out["plausible"] is True
    assert "outlines touch" in out["reason"]


def test_a_declared_link_is_explained_differently_from_a_guessed_one(store):
    store.declare("shed", "sala", connected=True)
    g = build_graph(HOUSE, store.overrides())
    out = transition(g, "shed", "sala")
    assert out["link"] == LINK_DECLARED
    assert "marked as connected" in out["reason"]


def test_a_route_through_a_middle_room_is_plausible_and_names_it():
    g = build_graph(HOUSE)
    out = transition(g, "sala", "kitchen")
    assert out["verdict"] == "reachable" and out["plausible"] is True
    assert out["path"] == ["sala", "hall", "kitchen"]
    assert "hall" in out["reason"]


def test_an_unreachable_room_is_flagged_without_blaming_the_sensor():
    """The sentence has to leave both explanations open: the floor plan may be
    incomplete, or the reading may be wrong. Asserting either would be a guess."""
    g = build_graph(HOUSE)
    out = transition(g, "sala", "shed")
    assert out["verdict"] == "unreachable" and out["plausible"] is False
    assert "missing from the floor plan" in out["reason"]
    assert "not what it seems" in out["reason"]


def test_a_room_not_on_the_plan_produces_no_verdict():
    """Wavr rooms can exist without being drawn. Absence from the map is not
    evidence that a movement is impossible."""
    g = build_graph(HOUSE)
    out = transition(g, "sala", "attic")
    assert out["verdict"] == "unknown_room"
    assert out["plausible"] is True, "no drawing is not a contradiction"


def test_the_same_room_is_not_a_movement():
    g = build_graph(HOUSE)
    assert transition(g, "sala", "sala")["verdict"] == "same_room"


def test_a_long_chain_stops_being_plausible():
    chain = _house(*[(f"r{i}", i * 5.2, 0, 5, 3) for i in range(MAX_PLAUSIBLE_HOPS + 3)])
    g = build_graph(chain)
    far = transition(g, "r0", f"r{MAX_PLAUSIBLE_HOPS + 2}")
    assert far["plausible"] is False
    assert "two people" in far["reason"], "the alternative explanation is offered"


def test_no_probability_is_ever_returned():
    """Wavr has no measured distribution of how people move through THIS house.
    A number without one is the unexplainable confidence the product refuses."""
    g = build_graph(HOUSE)
    for a, b in (("sala", "hall"), ("sala", "kitchen"), ("sala", "shed")):
        out = transition(g, a, b)
        assert not any(isinstance(v, float) for v in out.values())
        assert "probability" not in out and "likelihood" not in out


# -- Paths ---------------------------------------------------------------------

def test_the_shortest_path_is_returned():
    g = build_graph(HOUSE)
    assert shortest_path(g, "sala", "kitchen") == ["sala", "hall", "kitchen"]


def test_no_path_is_none_not_an_empty_list():
    """An empty list reads as "they are connected by nothing", which is a
    different claim from "there is no route"."""
    g = build_graph(HOUSE)
    assert shortest_path(g, "sala", "shed") is None


# -- What the UI reads ---------------------------------------------------------

def test_isolated_rooms_are_called_out():
    """A room nothing connects to is nearly always a drawing mistake, and it is
    invisible in a plain adjacency list."""
    out = describe(build_graph(HOUSE))
    assert out["isolated"] == ["shed"]


def test_the_description_says_adjacency_is_inferred():
    out = describe(build_graph(HOUSE))
    assert "inferred from the floor plan" in out["note"]
    assert "your correction always wins" in out["note"]
