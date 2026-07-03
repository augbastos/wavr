from __future__ import annotations

import json
import logging
import math
import os
import tempfile

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


# Validation
MAX_FLOORS = 64
MAX_ROOMS_PER_FLOOR = 512
MAX_WALLS_PER_FLOOR = 4096
MAX_VERTICES = 512
_FEATURE_TYPES = {"stairs", "door", "window"}


class HouseMapError(ValueError):
    """Raised by validate_house_map on any structural violation."""


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _point(p) -> bool:
    return isinstance(p, (list, tuple)) and len(p) == 2 and _finite(p[0]) and _finite(p[1])


def validate_house_map(doc: dict) -> None:
    if not isinstance(doc, dict):
        raise HouseMapError("house map must be an object")
    if doc.get("version") != 2:
        raise HouseMapError("version must be 2")
    if doc.get("units") != "m":
        raise HouseMapError("units must be 'm'")
    floors = doc.get("floors")
    if not isinstance(floors, list) or not floors:
        raise HouseMapError("floors must be a non-empty list")
    if len(floors) > MAX_FLOORS:
        raise HouseMapError(f"too many floors (> {MAX_FLOORS})")
    seen_levels, seen_ids = set(), set()
    for f in floors:
        if not isinstance(f, dict):
            raise HouseMapError("each floor must be an object")
        level = f.get("level")
        if not isinstance(level, int) or isinstance(level, bool):
            raise HouseMapError("floor level must be an integer")
        if level in seen_levels:
            raise HouseMapError(f"duplicate floor level {level}")
        seen_levels.add(level)
        fid = f.get("id")
        if not isinstance(fid, str) or fid in seen_ids:
            raise HouseMapError("floor id must be a unique string")
        seen_ids.add(fid)
        rooms = f.get("rooms", [])
        if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS_PER_FLOOR:
            raise HouseMapError(f"rooms must be a list (<= {MAX_ROOMS_PER_FLOOR})")
        for r in rooms:
            poly = r.get("polygon") if isinstance(r, dict) else None
            if not isinstance(poly, list) or len(poly) < 3:
                raise HouseMapError("room polygon must have >= 3 vertices")
            if len(poly) > MAX_VERTICES:
                raise HouseMapError(f"polygon too large (> {MAX_VERTICES} vertices)")
            if not all(_point(p) for p in poly):
                raise HouseMapError("polygon vertices must be finite [x, y] pairs")
        walls = f.get("walls", [])
        if not isinstance(walls, list) or len(walls) > MAX_WALLS_PER_FLOOR:
            raise HouseMapError(f"walls must be a list (<= {MAX_WALLS_PER_FLOOR})")
        for w in walls:
            if not (isinstance(w, dict) and _point(w.get("a")) and _point(w.get("b"))):
                raise HouseMapError("wall a/b must be finite points")
        feats = f.get("features", [])
        if not isinstance(feats, list):
            raise HouseMapError("features must be a list")
        for feat in feats:
            if not isinstance(feat, dict) or feat.get("type") not in _FEATURE_TYPES:
                raise HouseMapError(f"feature type must be one of {sorted(_FEATURE_TYPES)}")
            at = feat.get("at")
            if at is not None and not _point(at):
                raise HouseMapError("feature 'at' must be a finite point")


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    """Ray-casting (even-odd). Boundary behaviour is not specially handled (good enough
    for room assignment; a point exactly on an edge may go either way)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def room_at(house: dict, level: int, x: float, y: float) -> str | None:
    for f in house.get("floors", []):
        if f.get("level") != level:
            continue
        for r in f.get("rooms", []):
            poly = r.get("polygon") or []
            if len(poly) >= 3 and _point_in_polygon(x, y, poly):
                return r.get("name")
        return None      # right floor found, no room matched
    return None


def save_house_map(path: str, doc: dict) -> None:
    """Validate then atomically persist the house doc. Raises HouseMapError on an empty
    path or an invalid doc (writing nothing). Temp file + os.replace = no torn writes."""
    if not path:
        raise HouseMapError("no house_map path configured (set WAVR_HOUSE_MAP)")
    validate_house_map(doc)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
