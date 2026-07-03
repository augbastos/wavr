from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

DEFAULT_MAP = {
    "version": 2,
    "units": "m",
    "floors": [
        {
            "id": "f0",
            "name": "Térreo",
            "level": 0,
            "rooms": [
                {"id": "r_sala",    "name": "sala",    "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]},
                {"id": "r_quarto",  "name": "quarto",  "polygon": [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]},
                {"id": "r_quintal", "name": "quintal", "polygon": [[0.0, 3.2], [7.7, 3.2], [7.7, 5.7], [0.0, 5.7]]},
            ],
            "walls": [],
            "features": [],
            "backdrop": None,
        }
    ],
}


def _rect_to_polygon(x: float, y: float, w: float, h: float) -> list[list[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _migrate_v1(m: dict) -> dict:
    """A v1 doc ({'rooms':[{name,x,y,w,h}]}) -> one level-0 floor of rectangle polygons."""
    rooms = []
    for i, r in enumerate(m.get("rooms", [])):
        try:
            poly = _rect_to_polygon(float(r["x"]), float(r["y"]), float(r["w"]), float(r["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        rooms.append({"id": r.get("id") or f"r{i}", "name": str(r.get("name", f"cômodo {i}")), "polygon": poly})
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "Térreo", "level": 0, "rooms": rooms, "walls": [], "features": [], "backdrop": None}
    ]}


def load_house_map(path: str) -> dict:
    """User's floor plan from JSON as v2; DEFAULT_MAP on any problem (never raises).
    A v1 doc (no 'version', has 'rooms' at top level) is migrated to v2."""
    if not path:
        return DEFAULT_MAP
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("house map %s unreadable (%s); using default", path, exc)
        return DEFAULT_MAP
    if not isinstance(m, dict):
        log.warning("house map %s malformed (not an object); using default", path)
        return DEFAULT_MAP
    if m.get("version") == 2 and isinstance(m.get("floors"), list):
        return m
    if isinstance(m.get("rooms"), list):        # v1 -> migrate
        return _migrate_v1(m)
    log.warning("house map %s malformed (no floors/rooms); using default", path)
    return DEFAULT_MAP


def room_names(house: dict) -> list[str]:
    """Flat list of room names. v2: across all floors; tolerates a v1 top-level 'rooms'."""
    if isinstance(house.get("floors"), list):
        return [r["name"] for f in house["floors"] for r in f.get("rooms", []) if "name" in r]
    return [r["name"] for r in house.get("rooms", []) if "name" in r]
