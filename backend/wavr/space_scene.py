"""Renderer-independent Space geometry and room verdicts from the Core.

The caller supplies the already-redacted /api/state projection. Per-person
positions, identities and vitals are live-only, consent-gated data and never
belong in this scene, even when Watch is off.

Schema 1:
  {schema, levels: [{level, name}],
   rooms: [{room, level, polygon, centroid, occupied, confidence, person_count,
            precision_level, watch}],
   unplaced_rooms, bounds: {level: {min_x, min_y, max_x, max_y} | None},
   attention_rooms, attention_could_not_check}
"""
from __future__ import annotations

from wavr.client_view import _bool, _int, _num, _str
from wavr.events import HOUSE_ROOM
from wavr.housemap import _point


def _attention_room(item) -> str | None:
    # An attention item's `where` names a SCREEN ("devices", "coverage"...), never
    # a room. The room, when there is one, is a title argument (an offline sensor
    # in the kitchen carries title_args["room"] == "kitchen").
    if not isinstance(item, dict):
        return None
    args = item.get("title_args")
    room = args.get("room") if isinstance(args, dict) else None
    return room if isinstance(room, str) else None


def scene(house_doc, room_states, *, attention=None, attention_missed=()) -> dict:
    """Project a loaded house document and readable RoomStates without I/O.

    `attention_missed` names attention sources that could not be read: shown as
    such, so an unreadable source never looks like "nothing needs attention".
    """
    floors = house_doc.get("floors", []) if isinstance(house_doc, dict) else []
    states = room_states if isinstance(room_states, dict) else {}
    levels = []
    rooms = []
    bounds = {}
    unplaced = set()
    placed = set()
    known_rooms = set(states)

    for floor in sorted((f for f in floors if isinstance(f, dict)
                         and _int(f.get("level")) is not None),
                        key=lambda f: f["level"]):
        level = floor["level"]
        levels.append({"level": level, "name": _str(floor.get("name"))})
        points = []
        floor_rooms = floor.get("rooms", [])
        if not isinstance(floor_rooms, list):
            floor_rooms = []
        for room in sorted((r for r in floor_rooms if isinstance(r, dict)
                            and isinstance(r.get("name"), str)),
                           key=lambda r: r["name"]):
            name = room["name"]
            known_rooms.add(name)
            polygon = room.get("polygon")
            if not isinstance(polygon, list) or len(polygon) < 3 or not all(
                    _point(p) for p in polygon):
                unplaced.add(name)
                continue
            placed.add(name)
            polygon = [list(p) for p in polygon]
            points.extend(polygon)
            state = states.get(name)
            state = state if isinstance(state, dict) else {}
            rooms.append({
                "room": name, "level": level, "polygon": polygon,
                "centroid": [sum(p[0] for p in polygon) / len(polygon),
                             sum(p[1] for p in polygon) / len(polygon)],
                "occupied": _bool(state.get("occupied")),
                "confidence": _num(state.get("confidence")),
                "person_count": _int(state.get("person_count")),
                "precision_level": _str(state.get("precision_level")),
                # The Watch projection only ever SETS the flag; its absence under a
                # readable state is a known "not flagged" -- as client_view says.
                "watch": state.get("watch") is True if state else None,
            })
        bounds[str(level)] = ({"min_x": min(p[0] for p in points),
                               "min_y": min(p[1] for p in points),
                               "max_x": max(p[0] for p in points),
                               "max_y": max(p[1] for p in points)} if points else None)

    # The house-level aggregate is not a room anyone can draw.
    unplaced.update(name for name in states
                    if isinstance(name, str) and name not in placed and name != HOUSE_ROOM)
    unplaced.difference_update(placed)
    items = attention.get("items", []) if isinstance(attention, dict) else attention
    items = items if isinstance(items, list) else []
    attention_rooms = sorted({room for room in map(_attention_room, items)
                              if room is not None and room in known_rooms})
    return {"schema": 1, "levels": levels, "rooms": rooms,
            "unplaced_rooms": sorted(unplaced), "bounds": bounds,
            "attention_rooms": attention_rooms,
            "attention_could_not_check": sorted(str(m) for m in attention_missed)}
