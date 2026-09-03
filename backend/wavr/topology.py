"""Which rooms touch which, and whether a movement between two of them is
physically plausible.

## What this is for, and what it must never do

Topology **contextualises** evidence. It never creates it.

Wavr must not decide someone is in the hallway because they were in the bedroom
and are now in the kitchen. That would be inventing an observation, and it is the
single most tempting mistake in this area — a plausible inference is still a
fabrication, and it would appear in the same field as measured presence with no
way for anyone to tell them apart.

What topology legitimately does:

  * **evaluate a transition.** Bedroom → hallway → kitchen is ordinary. Bedroom →
    detached garage in two seconds is not, and the second one is evidence that
    something is wrong — a sensor bleeding through a wall, a mislabelled room, or
    two people rather than one.
  * **explain a contradiction.** "The office radar sees movement while the
    living-room camera sees nobody leave" reads differently once you know the two
    rooms share a wall and no door.
  * **give the operator a reason.** "These rooms are 4 m apart with no connecting
    room" is a sentence; a lowered confidence with no explanation is not.

## Where adjacency comes from

Two sources, in this order:

1. **Geometry**, derived from the house map the operator already drew. Rooms whose
   outlines come within `DEFAULT_TOUCH_M` of each other are adjacent — that gap is
   a wall, and a wall usually has a door. This is a heuristic and is labelled as
   one.
2. **The operator**, who can add a link the geometry missed (a staircase between
   floors, a corridor drawn as a separate room) or cut one it wrongly inferred (a
   shared wall with no door). An explicit statement always wins over an inferred
   one, and Wavr says which it used.

Cross-floor adjacency is NEVER inferred from geometry. Two rooms stacked on
different levels are not connected by being above each other; they are connected
by stairs, which is a thing the operator knows and the polygon does not.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import deque

# How close two room outlines must come to count as adjacent. A typical interior
# wall plus drawing slop. Generous on purpose: a missed adjacency makes an
# ordinary movement look suspicious, which is the more annoying failure.
DEFAULT_TOUCH_M = 0.6

# How far a path may wander before Wavr stops calling a movement plausible. Three
# hops covers "through the hallway and the kitchen" in an ordinary home; beyond
# that, a person crossing that many rooms between two sensor readings is more
# likely to be two people or a bleeding sensor.
MAX_PLAUSIBLE_HOPS = 3

LINK_INFERRED = "inferred"      # from the drawn geometry
LINK_DECLARED = "declared"      # the operator said so
LINK_CUT = "cut"                # the operator said NOT so


def _seg_distance(p1, p2, q1, q2) -> float:
    """Minimum distance between two line segments.

    Used on polygon edges rather than bounding boxes: an L-shaped living room's
    bounding box can touch a room it shares no wall with, and inferring a door
    there would make an impossible movement look ordinary.
    """
    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1]

    def sub(a, b):
        return (a[0] - b[0], a[1] - b[1])

    def point_seg(p, a, b):
        ab = sub(b, a)
        denom = dot(ab, ab)
        if denom == 0:
            return math.dist(p, a)
        t = max(0.0, min(1.0, dot(sub(p, a), ab) / denom))
        proj = (a[0] + t * ab[0], a[1] + t * ab[1])
        return math.dist(p, proj)

    # Segments that cross are at distance zero; otherwise the minimum is attained
    # at one of the four endpoint-to-segment distances.
    d1, d2 = sub(p2, p1), sub(q2, q1)
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if denom != 0:
        s = ((q1[0] - p1[0]) * d2[1] - (q1[1] - p1[1]) * d2[0]) / denom
        t = ((q1[0] - p1[0]) * d1[1] - (q1[1] - p1[1]) * d1[0]) / denom
        if 0.0 <= s <= 1.0 and 0.0 <= t <= 1.0:
            return 0.0
    return min(point_seg(p1, q1, q2), point_seg(p2, q1, q2),
               point_seg(q1, p1, p2), point_seg(q2, p1, p2))


def _polygon_gap(a: list, b: list) -> float:
    """Smallest distance between two room outlines."""
    best = math.inf
    for i in range(len(a)):
        p1, p2 = a[i], a[(i + 1) % len(a)]
        for j in range(len(b)):
            q1, q2 = b[j], b[(j + 1) % len(b)]
            best = min(best, _seg_distance(p1, p2, q1, q2))
            if best == 0.0:
                return 0.0
    return best


def _rooms_by_level(house: dict) -> dict[int, list[tuple[str, list]]]:
    """Room name + polygon, grouped by floor level.

    Grouped because geometry may only ever infer adjacency WITHIN a level: two
    rooms stacked on different floors are not connected by being above each
    other.
    """
    out: dict[int, list[tuple[str, list]]] = {}
    floors = house.get("floors")
    if not isinstance(floors, list):
        return out
    for floor in floors:
        try:
            level = int(floor.get("level", 0))
        except (TypeError, ValueError):
            continue
        bucket = out.setdefault(level, [])
        for room in floor.get("rooms") or []:
            name = room.get("name")
            poly = room.get("polygon")
            if isinstance(name, str) and name and isinstance(poly, list) and len(poly) >= 3:
                try:
                    bucket.append((name, [(float(x), float(y)) for x, y in poly]))
                except (TypeError, ValueError):
                    continue          # a malformed polygon is not an adjacency
    return out


def derive_adjacency(house: dict, touch_m: float = DEFAULT_TOUCH_M) -> dict[str, set[str]]:
    """Adjacency inferred from the drawn floor plan.

    A heuristic, and reported as one: rooms whose outlines come within `touch_m`
    are treated as connected because that gap is a wall and walls usually have
    doors. It will occasionally link two rooms that share a solid wall — which is
    why the operator can cut a link.
    """
    adj: dict[str, set[str]] = {}
    for rooms in _rooms_by_level(house).values():
        for i, (name_a, poly_a) in enumerate(rooms):
            adj.setdefault(name_a, set())
            for name_b, poly_b in rooms[i + 1:]:
                adj.setdefault(name_b, set())
                if _polygon_gap(poly_a, poly_b) <= touch_m:
                    adj[name_a].add(name_b)
                    adj[name_b].add(name_a)
    return adj


class TopologyStore:
    """Operator corrections to the inferred graph.

    Only the corrections are stored. The geometry is re-derived from the house
    map every time, so redrawing a room updates adjacency without anything going
    stale — a cached graph would quietly describe a floor plan that no longer
    exists.
    """

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS room_links (
                room_a  TEXT NOT NULL,
                room_b  TEXT NOT NULL,
                kind    TEXT NOT NULL,
                note    TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (room_a, room_b)
            )""")
        self._conn.commit()

    @staticmethod
    def _pair(a: str, b: str) -> tuple[str, str]:
        """Links are undirected; storing one ordering stops a graph that says
        the kitchen connects to the hall but not the reverse."""
        return (a, b) if a <= b else (b, a)

    def declare(self, room_a: str, room_b: str, connected: bool,
                note: str = "") -> dict:
        if not room_a or not room_b or room_a == room_b:
            raise ValueError("two different rooms are required")
        a, b = self._pair(room_a, room_b)
        kind = LINK_DECLARED if connected else LINK_CUT
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_links (room_a, room_b, kind, note) VALUES (?,?,?,?)"
                " ON CONFLICT(room_a, room_b) DO UPDATE SET kind=excluded.kind,"
                " note=excluded.note", (a, b, kind, note[:200]))
            self._conn.commit()
        return {"room_a": a, "room_b": b, "kind": kind, "note": note[:200]}

    def clear(self, room_a: str, room_b: str) -> bool:
        """Drop an override, returning to whatever the geometry says."""
        a, b = self._pair(room_a, room_b)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM room_links WHERE room_a = ? AND room_b = ?", (a, b))
            self._conn.commit()
            return cur.rowcount > 0

    def overrides(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT room_a, room_b, kind, note FROM room_links"
                " ORDER BY room_a, room_b").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def build_graph(house: dict, overrides: list[dict] | None = None,
                touch_m: float = DEFAULT_TOUCH_M) -> dict[str, dict[str, str]]:
    """The effective graph: geometry, then the operator's corrections on top.

    Values are the link KIND, not just a boolean, so every answer can say
    whether it rests on something the operator stated or something Wavr guessed
    from a drawing.
    """
    graph: dict[str, dict[str, str]] = {
        room: {n: LINK_INFERRED for n in neighbours}
        for room, neighbours in derive_adjacency(house, touch_m).items()}
    for row in overrides or []:
        a, b, kind = row.get("room_a"), row.get("room_b"), row.get("kind")
        if not a or not b:
            continue
        graph.setdefault(a, {})
        graph.setdefault(b, {})
        if kind == LINK_CUT:
            graph[a].pop(b, None)
            graph[b].pop(a, None)
        elif kind == LINK_DECLARED:
            graph[a][b] = LINK_DECLARED
            graph[b][a] = LINK_DECLARED
    return graph


def shortest_path(graph: dict[str, dict[str, str]], start: str,
                  goal: str) -> list[str] | None:
    """Fewest rooms between two rooms, or None when there is no path.

    Breadth-first: every link is one step, because Wavr does not know how long a
    room takes to cross and inventing a cost would be a number with no source.
    """
    if start == goal:
        return [start]
    if start not in graph or goal not in graph:
        return None
    seen = {start}
    queue = deque([(start, [start])])
    while queue:
        room, path = queue.popleft()
        for neighbour in sorted(graph.get(room, {})):
            if neighbour in seen:
                continue
            if neighbour == goal:
                return path + [neighbour]
            seen.add(neighbour)
            queue.append((neighbour, path + [neighbour]))
    return None


def transition(graph: dict[str, dict[str, str]], from_room: str,
               to_room: str) -> dict:
    """How plausible is a movement between these two rooms, and why.

    Returns a verdict and a sentence. It never returns a probability: Wavr has no
    measured distribution of how people move through this particular house, and a
    number without one would be exactly the unexplainable confidence the product
    refuses elsewhere.
    """
    if from_room == to_room:
        return {"verdict": "same_room", "hops": 0, "path": [from_room],
                "plausible": True,
                "reason": "Same room — no movement to explain."}
    if from_room not in graph or to_room not in graph:
        unknown = from_room if from_room not in graph else to_room
        return {"verdict": "unknown_room", "hops": None, "path": None,
                "plausible": True,
                "reason": (f"{unknown} is not on the floor plan, so Wavr cannot "
                           f"say whether this movement makes sense.")}
    path = shortest_path(graph, from_room, to_room)
    if path is None:
        return {"verdict": "unreachable", "hops": None, "path": None,
                "plausible": False,
                "reason": (f"No drawn route connects {from_room} to {to_room}. "
                           f"Either a connection is missing from the floor plan, "
                           f"or one of these readings is not what it seems.")}
    hops = len(path) - 1
    if hops == 1:
        kind = graph[from_room].get(to_room, LINK_INFERRED)
        how = ("they are marked as connected" if kind == LINK_DECLARED
               else "their outlines touch on the floor plan")
        return {"verdict": "adjacent", "hops": 1, "path": path,
                "plausible": True, "link": kind,
                "reason": f"{from_room} and {to_room} are adjacent — {how}."}
    if hops <= MAX_PLAUSIBLE_HOPS:
        return {"verdict": "reachable", "hops": hops, "path": path,
                "plausible": True,
                "reason": ("Reachable via " + " → ".join(path[1:-1]) + ".")}
    return {"verdict": "distant", "hops": hops, "path": path,
            "plausible": False,
            "reason": (f"{to_room} is {hops} rooms from {from_room}. A single "
                       f"person is unlikely to have crossed that between two "
                       f"readings — this may be two people, or a sensor seeing "
                       f"through a wall.")}


def describe(graph: dict[str, dict[str, str]]) -> dict:
    """The graph as the UI and an agent read it.

    `isolated` is called out separately because a room nothing connects to is
    almost always a drawing mistake, and it is invisible in an adjacency list.
    """
    rooms = sorted(graph)
    return {
        "rooms": [
            {"room": room,
             "neighbours": [{"room": n, "link": kind}
                            for n, kind in sorted(graph[room].items())]}
            for room in rooms],
        "isolated": [r for r in rooms if not graph[r]],
        "note": ("Adjacency is inferred from the floor plan you drew — rooms "
                 "whose outlines nearly touch are treated as connected. Correct "
                 "any link that is wrong; your correction always wins."),
    }
