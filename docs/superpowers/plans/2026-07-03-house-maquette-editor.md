# House Maquette Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Wavr Desktop an editable, multi-floor, top-down 2D editor for the shape of the property (rooms as polygons, walls, stairs) in meters, persisted and rendered, as the coordinate frame for position sensing.

**Architecture:** Extend `housemap.py` from a flat v1 rectangle map to a v2 model (floors → room polygons + wall segments + features), with a non-raising v1→v2 migration. Add a validated, central-only `PUT /api/house` that atomically writes `house.json` and updates the in-memory map. Add a top-down SVG editor panel to the single-file dashboard (live-only central), with client-side undo/redo. Walls are stored and drawn now; fusion using walls (occlusion) is a later spec (B2).

**Tech Stack:** Python 3.11+, FastAPI, pytest (backend, offline, injected transports). Vanilla JS + inline SVG in `frontend/index.html` (no build step, zero external requests). Playwright MCP for frontend behavior verification.

## Global Constraints

- Backend runtime deps unchanged; no new packages. Stdlib only for the map (`json`, `os`, `tempfile`, `math`).
- `frontend/index.html` stays a single file with ZERO external requests (no CDN/library/font/image). All SVG + DOM + JS inline.
- Privacy: geometry is authored config — never targets, vitals, frames, or MACs. `house.json` is local; nothing leaves the box.
- Writes are central-only: `PUT /api/house` uses `Depends(require_local)` (loopback + `X-Wavr-Local` CSRF header, or `can_change_state`/central role under multi-device) — identical gating to the camera CRUD routes.
- Editor UI is live-only: the whole panel is gated on `MODE === "live"`; absent in the demo; the companion viewer renders the map read-only but has no editing tools and no PUT.
- Units are meters throughout. Grid snap default `0.25` m.
- `load_house_map` must NEVER raise (falls back to `DEFAULT_MAP`); only `PUT` validation raises (→ 422).
- Backward compatibility: a v1 doc (`{"rooms":[{name,x,y,w,h}]}`, no `version`) must keep working via migration. Existing `GET /api/house` consumers must not break.

---

### Task 1: v2 house-map model + v1→v2 migration + `room_names` helper

**Files:**
- Modify: `backend/wavr/housemap.py`
- Modify: `backend/wavr/app.py:170` (HA-discovery room-name flatten)
- Test: `backend/tests/test_housemap.py` (create)

**Interfaces:**
- Produces: `DEFAULT_MAP` (v2 dict), `load_house_map(path: str) -> dict` (returns v2, non-raising), `room_names(house: dict) -> list[str]` (flatten room names across floors; tolerates v1).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_housemap.py
from wavr.housemap import DEFAULT_MAP, load_house_map, room_names

def test_default_map_is_v2():
    assert DEFAULT_MAP["version"] == 2
    assert DEFAULT_MAP["units"] == "m"
    assert isinstance(DEFAULT_MAP["floors"], list) and DEFAULT_MAP["floors"]
    f0 = DEFAULT_MAP["floors"][0]
    assert f0["level"] == 0
    assert all("polygon" in r for r in f0["rooms"])

def test_load_missing_path_returns_v2_default():
    assert load_house_map("")["version"] == 2

def test_v1_rectangles_migrate_to_v2_polygons(tmp_path):
    import json
    p = tmp_path / "v1.json"
    p.write_text(json.dumps({"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}]}))
    m = load_house_map(str(p))
    assert m["version"] == 2
    floor = m["floors"][0]
    assert floor["level"] == 0
    room = floor["rooms"][0]
    assert room["name"] == "sala"
    # rectangle -> closed polygon corners (x,y)-(x+w,y)-(x+w,y+h)-(x,y+h)
    assert room["polygon"] == [[0, 0], [4, 0], [4, 3], [0, 3]]

def test_malformed_falls_back_to_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    assert load_house_map(str(p)) == DEFAULT_MAP

def test_room_names_flattens_v2_across_floors():
    house = {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0, "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[1,0],[1,1]]}], "walls": [], "features": [], "backdrop": None},
        {"id": "f1", "name": "1", "level": 1, "rooms": [{"id": "r2", "name": "quarto", "polygon": [[0,0],[1,0],[1,1]]}], "walls": [], "features": [], "backdrop": None},
    ]}
    assert room_names(house) == ["sala", "quarto"]

def test_room_names_tolerates_v1():
    assert room_names({"rooms": [{"name": "sala"}]}) == ["sala"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_housemap.py -v`
Expected: FAIL (`ImportError: cannot import name 'room_names'`, `DEFAULT_MAP["version"]` KeyError).

- [ ] **Step 3: Rewrite `housemap.py` with the v2 model + migration + helper**

```python
# backend/wavr/housemap.py
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
```

- [ ] **Step 4: Update the HA-discovery consumer in `app.py`**

Change `app.py:170` from the v1 shape to the flatten helper.

```python
# backend/wavr/app.py — add to the housemap import on line 16:
from wavr.housemap import load_house_map, room_names
```
```python
# backend/wavr/app.py — replace the list comprehension at ~line 170:
                room_names(_house),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_housemap.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `cd backend && python -m pytest -q`
Expected: PASS (all prior tests + the 6 new).

- [ ] **Step 7: Commit**

```bash
git add backend/wavr/housemap.py backend/wavr/app.py backend/tests/test_housemap.py
git commit -m "feat(housemap): v2 model (floors/rooms/walls/features) + v1->v2 migration + room_names"
```

---

### Task 2: House-map validation

**Files:**
- Modify: `backend/wavr/housemap.py`
- Test: `backend/tests/test_housemap.py`

**Interfaces:**
- Produces: `HouseMapError(ValueError)`, `validate_house_map(doc: dict) -> None` (raises `HouseMapError` with a human message on any violation; returns None if valid).
- Caps: `MAX_FLOORS = 64`, `MAX_ROOMS_PER_FLOOR = 512`, `MAX_WALLS_PER_FLOOR = 4096`, `MAX_VERTICES = 512`.

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/tests/test_housemap.py
import pytest
from wavr.housemap import validate_house_map, HouseMapError, DEFAULT_MAP

def _valid():
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[4,0],[4,3],[0,3]]}],
         "walls": [{"id": "w1", "a": [4,0], "b": [4,3]}],
         "features": [{"id": "s1", "type": "stairs", "at": [3.5,2.5], "to_level": 1}],
         "backdrop": None}]}

def test_default_map_validates():
    validate_house_map(DEFAULT_MAP)          # must not raise

def test_valid_doc_passes():
    validate_house_map(_valid())

@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.update(version=1), "version"),
    (lambda d: d.update(units="ft"), "units"),
    (lambda d: d.update(floors=[]), "floors"),
    (lambda d: d["floors"].append(dict(d["floors"][0])), "level"),           # duplicate level
    (lambda d: d["floors"][0]["rooms"][0].update(polygon=[[0,0],[1,1]]), "polygon"),  # <3 verts
    (lambda d: d["floors"][0]["rooms"][0]["polygon"].__setitem__(0, ["x", 0]), "finite"),
    (lambda d: d["floors"][0]["walls"][0].update(a=[float("inf"), 0]), "finite"),
    (lambda d: d["floors"][0]["features"][0].update(type="teleporter"), "type"),
])
def test_invalid_docs_raise(mutate, msg):
    d = _valid()
    mutate(d)
    with pytest.raises(HouseMapError) as e:
        validate_house_map(d)
    assert msg in str(e.value).lower()

def test_over_cap_rooms_raise():
    d = _valid()
    d["floors"][0]["rooms"] = [{"id": f"r{i}", "name": str(i), "polygon": [[0,0],[1,0],[1,1]]} for i in range(513)]
    with pytest.raises(HouseMapError):
        validate_house_map(d)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_housemap.py -k "validate or invalid or cap or valid_doc" -v`
Expected: FAIL (`ImportError: cannot import name 'validate_house_map'`).

- [ ] **Step 3: Add validation to `housemap.py`**

```python
# backend/wavr/housemap.py — append
import math

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
        for feat in f.get("features", []):
            if not isinstance(feat, dict) or feat.get("type") not in _FEATURE_TYPES:
                raise HouseMapError(f"feature type must be one of {sorted(_FEATURE_TYPES)}")
            at = feat.get("at")
            if at is not None and not _point(at):
                raise HouseMapError("feature 'at' must be a finite point")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_housemap.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/housemap.py backend/tests/test_housemap.py
git commit -m "feat(housemap): validate_house_map with caps + finite/polygon/level checks"
```

---

### Task 3: Point-in-polygon room assignment

**Files:**
- Modify: `backend/wavr/housemap.py`
- Test: `backend/tests/test_housemap.py`

**Interfaces:**
- Produces: `room_at(house: dict, level: int, x: float, y: float) -> str | None` (name of the first room polygon on that floor containing the point; None if none / unknown floor).

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/tests/test_housemap.py
from wavr.housemap import room_at

def _house_L():
    # concave (L-shaped) room on level 0
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0, "walls": [], "features": [], "backdrop": None,
         "rooms": [{"id": "r1", "name": "L", "polygon": [[0,0],[4,0],[4,2],[2,2],[2,4],[0,4]]}]}]}

def test_point_inside_polygon():
    assert room_at(_house_L(), 0, 1.0, 1.0) == "L"

def test_point_in_concave_notch_is_outside():
    # (3,3) is in the cut-out notch of the L -> not inside
    assert room_at(_house_L(), 0, 3.0, 3.0) is None

def test_point_outside_polygon():
    assert room_at(_house_L(), 0, 10.0, 10.0) is None

def test_unknown_floor_returns_none():
    assert room_at(_house_L(), 5, 1.0, 1.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_housemap.py -k room_at -v`
Expected: FAIL (`cannot import name 'room_at'`).

- [ ] **Step 3: Implement ray-casting point-in-polygon in `housemap.py`**

```python
# backend/wavr/housemap.py — append

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_housemap.py -k room_at -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/housemap.py backend/tests/test_housemap.py
git commit -m "feat(housemap): room_at point-in-polygon room assignment (ray casting)"
```

---

### Task 4: Atomic writer `save_house_map`

**Files:**
- Modify: `backend/wavr/housemap.py`
- Test: `backend/tests/test_housemap.py`

**Interfaces:**
- Produces: `save_house_map(path: str, doc: dict) -> None` (validates via `validate_house_map`, then writes atomically with a temp file + `os.replace`; raises `HouseMapError` if `path` is empty or the doc is invalid — nothing is written on failure).

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/tests/test_housemap.py
from wavr.housemap import save_house_map

def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "house.json"
    doc = _valid()
    save_house_map(str(p), doc)
    assert load_house_map(str(p)) == doc

def test_save_rejects_invalid_and_writes_nothing(tmp_path):
    p = tmp_path / "house.json"
    bad = _valid(); bad["version"] = 1
    with pytest.raises(HouseMapError):
        save_house_map(str(p), bad)
    assert not p.exists()

def test_save_empty_path_raises(tmp_path):
    with pytest.raises(HouseMapError):
        save_house_map("", _valid())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_housemap.py -k save -v`
Expected: FAIL (`cannot import name 'save_house_map'`).

- [ ] **Step 3: Implement the atomic writer in `housemap.py`**

```python
# backend/wavr/housemap.py — append
import os
import tempfile


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_housemap.py -v`
Expected: PASS (all housemap tests).

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/housemap.py backend/tests/test_housemap.py
git commit -m "feat(housemap): save_house_map atomic writer (validate + temp + os.replace)"
```

---

### Task 5: `PUT /api/house` route (validated, central-only, updates in-memory map)

**Files:**
- Modify: `backend/wavr/app.py` (near the existing `GET /api/house` at ~line 262)
- Test: `backend/tests/test_house_api.py` (create)

**Interfaces:**
- Consumes: `save_house_map`, `validate_house_map`, `HouseMapError` from `wavr.housemap`; existing `require_local` dependency; the in-memory `_house` dict.
- Produces: `PUT /api/house` → 200 with the stored doc on success; 422 on an invalid doc; 409 if no `house_map` path is configured. Updates `_house` in place so `GET /api/house` and `room_names(_house)` see the change.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_house_api.py
import json
from fastapi.testclient import TestClient
from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}

def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))

def _valid():
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[4,0],[4,3],[0,3]]}],
         "walls": [], "features": [], "backdrop": None}]}

def test_put_house_persists_and_updates_get(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    r = c.put("/api/house", json=_valid(), headers=CSRF)
    assert r.status_code == 200
    assert c.get("/api/house").json()["floors"][0]["rooms"][0]["name"] == "sala"
    assert (tmp_path / "house.json").exists()

def test_put_invalid_doc_is_422_and_writes_nothing(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    bad = _valid(); bad["floors"][0]["rooms"][0]["polygon"] = [[0,0],[1,1]]
    r = c.put("/api/house", json=bad, headers=CSRF)
    assert r.status_code == 422
    assert not (tmp_path / "house.json").exists()

def test_put_house_requires_csrf_on_loopback(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    assert c.put("/api/house", json=_valid()).status_code == 403   # no X-Wavr-Local
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_house_api.py -v`
Expected: FAIL (405 Method Not Allowed — no PUT route yet).

- [ ] **Step 3: Add the route in `app.py` right after the `GET /api/house` handler (~line 264)**

```python
# backend/wavr/app.py — update the housemap import (line 16) to also bring in the writer:
from wavr.housemap import load_house_map, room_names, save_house_map, HouseMapError
```
```python
# backend/wavr/app.py — add immediately after the GET /api/house handler:
    @app.put("/api/house")
    async def put_house(doc: dict = Body(...), _=Depends(require_local)):
        try:
            save_house_map(cfg.house_map, doc)
        except HouseMapError as exc:
            # empty-path -> 409 (server misconfig); invalid doc -> 422.
            code = 409 if "no house_map path" in str(exc) else 422
            raise HTTPException(status_code=code, detail=str(exc))
        _house.clear()
        _house.update(doc)          # keep the in-memory map (GET, room_names) in sync
        return _house
```

Note: `Body`, `Depends`, and `HTTPException` are already imported in `app.py` (used by the camera/system routes). Confirm at the top of the file; add to the existing `fastapi` import line if any are missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_house_api.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS (everything green).

- [ ] **Step 6: Commit**

```bash
git add backend/wavr/app.py backend/tests/test_house_api.py
git commit -m "feat(api): PUT /api/house — validated, central-only, atomic; updates in-memory map"
```

---

### Task 6: Frontend — render the v2 geometry + floor selector (read path)

> Single-file HTML has no unit harness, so verification is Playwright DOM/behaviour assertions against a running backend, plus a manual smoke. Keep ALL additions inline (no external requests).

**Files:**
- Modify: `frontend/index.html` (the radar/house rendering block + a floor selector)

**Interfaces:**
- Consumes: `GET /api/house` (v2 doc), the existing `MODE` global and radar SVG transform.
- Produces: `renderHouse(house, level)` (draws room polygons, walls, stair/door markers for one floor into the radar SVG), a floor `<select id="floorSelect">`, and a `currentFloor` state. Targets keep rendering on top via the existing radar code.

- [ ] **Step 1: Add a floor selector + house layer to the radar markup**

In the radar container (search `id="radarWrap"`), add above the SVG:
```html
<div class="floorbar" id="floorbar" hidden>
  <label>Andar: <select id="floorSelect"></select></label>
</div>
```
Add a `<g id="houseLayer"></g>` as the FIRST child of the radar `<svg>` (so targets draw over it).

- [ ] **Step 2: Implement `renderHouse` + floor state (inline `<script>`)**

```javascript
let HOUSE = { version:2, units:"m", floors:[] };
let currentFloor = 0;
function floorByLevel(l){ return HOUSE.floors.find(f => f.level === l) || HOUSE.floors[0]; }
function m2px(v){ return v * RADAR_SCALE; }           // reuse the radar's meters->px scale
function renderHouse(){
  const layer = document.getElementById("houseLayer");
  if(!layer) return;
  const f = floorByLevel(currentFloor); layer.innerHTML = "";
  if(!f) return;
  for(const r of f.rooms||[]){
    const pts = (r.polygon||[]).map(p => m2px(p[0])+","+m2px(p[1])).join(" ");
    const poly = document.createElementNS("http://www.w3.org/2000/svg","polygon");
    poly.setAttribute("points", pts); poly.setAttribute("class","room-poly");
    poly.dataset.name = r.name; layer.appendChild(poly);
  }
  for(const w of f.walls||[]){
    const ln = document.createElementNS("http://www.w3.org/2000/svg","line");
    ln.setAttribute("x1",m2px(w.a[0])); ln.setAttribute("y1",m2px(w.a[1]));
    ln.setAttribute("x2",m2px(w.b[0])); ln.setAttribute("y2",m2px(w.b[1]));
    ln.setAttribute("class","wall"); layer.appendChild(ln);
  }
  for(const ft of f.features||[]){
    if(!ft.at) continue;
    const c = document.createElementNS("http://www.w3.org/2000/svg","circle");
    c.setAttribute("cx",m2px(ft.at[0])); c.setAttribute("cy",m2px(ft.at[1]));
    c.setAttribute("r",6); c.setAttribute("class","feat feat-"+ft.type); layer.appendChild(c);
  }
}
function buildFloorSelect(){
  const sel = document.getElementById("floorSelect"), bar = document.getElementById("floorbar");
  if(!sel) return;
  sel.innerHTML = "";
  for(const f of HOUSE.floors){
    const o = document.createElement("option"); o.value = f.level; o.textContent = f.name; sel.appendChild(o);
  }
  sel.value = currentFloor;
  sel.onchange = ()=>{ currentFloor = parseInt(sel.value,10); renderHouse(); };
  if(bar) bar.hidden = HOUSE.floors.length < 2;   // show only when multi-floor
}
async function loadHouse(){
  if(MODE === "simulated"){ HOUSE = DEMO_HOUSE; }     // built-in default; no backend call
  else {
    try{ const r = await fetch("/api/house"); if(r.ok) HOUSE = await r.json(); }catch{}
  }
  currentFloor = HOUSE.floors[0]?.level ?? 0;
  buildFloorSelect(); renderHouse();
}
```
Add a small `DEMO_HOUSE` constant equal to the v2 default (so the demo renders without a backend). Call `loadHouse()` during startup (where the radar initialises).

- [ ] **Step 3: Add CSS for the house layer (inline `<style>`)**

```css
.room-poly{ fill: rgba(52,211,153,.05); stroke: var(--line); stroke-width:1; }
.room-poly:hover{ fill: rgba(52,211,153,.10); }
.wall{ stroke: var(--ink); stroke-width:3; stroke-linecap:round; }
.feat-stairs{ fill:#f59e0b; } .feat-door{ fill:#38bdf8; } .feat-window{ fill:#94a3b8; }
.floorbar{ padding:8px 24px; border-bottom:1px solid var(--line); font-size:.85rem; }
.floorbar:not([hidden]){ display:block; }
```

- [ ] **Step 4: Verify rendering with Playwright against a running backend**

Start a backend on a free port (loopback), seed a 2-floor house via PUT, then assert the SVG.
```bash
cd /c/IA/wavr && WAVR_HOUSE_MAP="$PWD/scratch-house.json" WAVR_PORT=8021 .venv/Scripts/python -m wavr.serve &
```
Playwright:
- `browser_navigate` → `http://127.0.0.1:8021/`
- PUT a 2-floor doc first (via `curl` or a fetch in `browser_evaluate`).
- `browser_evaluate`:
```javascript
() => ({
  mode: MODE,
  floors: HOUSE.floors.length,
  polys: document.querySelectorAll('#houseLayer .room-poly').length,
  floorbarShown: !document.getElementById('floorbar').hidden,
})
```
Expected: `mode:"live"`, `floors:2`, `polys` = rooms on floor 0, `floorbarShown:true`. Switch `floorSelect` and re-assert polys change. Stop the backend + remove `scratch-house.json`.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): render v2 house geometry (rooms/walls/features) + floor selector"
```

---

### Task 7: Frontend — editor tools (rooms, walls, stairs), undo/redo, save

> Live-only central editor. Verification is Playwright behaviour + manual smoke.

**Files:**
- Modify: `frontend/index.html` (editor panel + tools + history + save)

**Interfaces:**
- Consumes: `HOUSE`, `currentFloor`, `renderHouse`, `m2px`, `PUT /api/house`, `MODE`.
- Produces: an editor panel (`id="houseEditor"`, gated on `MODE==="live"`); tool state; `pushHistory()` / `undo()` / `redo()` over `HOUSE` snapshots; `saveHouse()` (PUT + dirty flag).

- [ ] **Step 1: Add the editor panel markup (live-only)**

After the radar, add:
```html
<section id="houseEditor" class="hed" hidden>
  <div class="hed-tools">
    <button data-tool="select" class="ctl small">Selecionar</button>
    <button data-tool="room" class="ctl small">+ Cômodo</button>
    <button data-tool="wall" class="ctl small">+ Parede</button>
    <button data-tool="stairs" class="ctl small">+ Escada</button>
    <button id="hedDelete" class="ctl small">Apagar</button>
    <button id="hedUndo" class="ctl small">↶</button>
    <button id="hedRedo" class="ctl small">↷</button>
    <button id="hedAddFloor" class="ctl small">+ Andar</button>
    <button id="hedSave" class="ctl small primary">Salvar</button>
    <span id="hedDirty" class="hed-dirty" hidden>não salvo</span>
  </div>
</section>
```
CSS: `.hed:not([hidden]){display:block;padding:12px 24px;border-bottom:1px solid var(--line);}` and `.hed-tools{display:flex;gap:8px;flex-wrap:wrap;}`.

- [ ] **Step 2: Gate the editor on live mode + wire pointer drawing on the radar SVG**

```javascript
let TOOL = "select", DIRTY = false;
const HISTORY = [], REDO = [];
function snapshot(){ return JSON.parse(JSON.stringify(HOUSE)); }
function pushHistory(){ HISTORY.push(snapshot()); if(HISTORY.length>50) HISTORY.shift(); REDO.length=0; markDirty(true); }
function undo(){ if(!HISTORY.length) return; REDO.push(snapshot()); HOUSE = HISTORY.pop(); afterEdit(); }
function redo(){ if(!REDO.length) return; HISTORY.push(snapshot()); HOUSE = REDO.pop(); afterEdit(); }
function afterEdit(){ buildFloorSelect(); renderHouse(); markDirty(true); }
function markDirty(v){ DIRTY = v; const d=document.getElementById("hedDirty"); if(d) d.hidden = !v; }
const SNAP = 0.25;
function snap(v){ return Math.round(v/SNAP)*SNAP; }
function px2m(px){ return px / RADAR_SCALE; }

function initHouseEditor(){
  if(MODE !== "live") return;                 // central-only
  const ed = document.getElementById("houseEditor"); if(ed) ed.hidden = false;
  document.querySelectorAll('#houseEditor [data-tool]').forEach(b =>
    b.onclick = ()=>{ TOOL = b.dataset.tool; });
  document.getElementById("hedUndo").onclick = undo;
  document.getElementById("hedRedo").onclick = redo;
  document.getElementById("hedSave").onclick = saveHouse;
  document.getElementById("hedAddFloor").onclick = addFloor;
  document.getElementById("hedDelete").onclick = deleteSelected;
  wireCanvasDrawing();                        // pointer handlers below
  window.addEventListener("keydown", e => {
    if(e.ctrlKey && e.key === "z"){ e.preventDefault(); undo(); }
    if(e.ctrlKey && (e.key === "y" || (e.shiftKey && e.key === "Z"))){ e.preventDefault(); redo(); }
  });
}
```

- [ ] **Step 3: Implement the drawing operations (room drag, wall drag, stair click, delete)**

```javascript
function curFloor(){ return floorByLevel(currentFloor); }
function uid(p){ return p + Math.abs((Date.now()^(HISTORY.length*2654435761))>>>0).toString(36); }
function wireCanvasDrawing(){
  const svg = document.querySelector('#radarWrap svg'); if(!svg) return;
  let start = null;
  const at = ev => { const pt = svg.getBoundingClientRect();
    return [ snap(px2m(ev.clientX - pt.left)), snap(px2m(ev.clientY - pt.top)) ]; };
  svg.addEventListener("pointerdown", ev => {
    if(MODE!=="live") return;
    const p = at(ev);
    if(TOOL==="stairs"){ pushHistory();
      curFloor().features.push({id:uid("s"),type:"stairs",at:p,to_level:currentFloor+1}); afterEdit(); return; }
    if(TOOL==="room"||TOOL==="wall"){ start = p; }
  });
  svg.addEventListener("pointerup", ev => {
    if(MODE!=="live" || !start) return;
    const p = at(ev), f = curFloor();
    if(TOOL==="room"){ const [x0,y0]=start,[x1,y1]=p;
      const x=Math.min(x0,x1),y=Math.min(y0,y1),X=Math.max(x0,x1),Y=Math.max(y0,y1);
      if(X-x>=SNAP && Y-y>=SNAP){ pushHistory();
        f.rooms.push({id:uid("r"),name:"cômodo "+(f.rooms.length+1),polygon:[[x,y],[X,y],[X,Y],[x,Y]]}); afterEdit(); } }
    if(TOOL==="wall" && (Math.hypot(p[0]-start[0],p[1]-start[1])>=SNAP)){ pushHistory();
      f.walls.push({id:uid("w"),a:start,b:p}); afterEdit(); }
    start = null;
  });
}
let SELECTED = null;   // {kind:'room'|'wall'|'feature', id}
function deleteSelected(){
  if(!SELECTED) return; const f = curFloor(); pushHistory();
  const key = SELECTED.kind==="room"?"rooms":SELECTED.kind==="wall"?"walls":"features";
  f[key] = f[key].filter(o => o.id !== SELECTED.id); SELECTED=null; afterEdit();
}
function addFloor(){
  const levels = HOUSE.floors.map(f=>f.level); const nl = Math.max(...levels)+1;
  pushHistory();
  HOUSE.floors.push({id:uid("f"),name:(nl+"º andar"),level:nl,rooms:[],walls:[],features:[],backdrop:null});
  currentFloor = nl; afterEdit();
}
```
(Selecting an element for delete: add a `click` handler on `#houseLayer` children in `renderHouse` that sets `SELECTED` and adds a `.selected` class — wire `poly.onclick`/`ln.onclick`/`c.onclick` to set `SELECTED = {kind,id}` and re-render with a highlight.)

- [ ] **Step 4: Implement `saveHouse` (PUT with the loopback CSRF header)**

```javascript
async function saveHouse(){
  try{
    const r = await fetch("/api/house", { method:"PUT",
      headers:{ "Content-Type":"application/json", "X-Wavr-Local":"1" },
      body: JSON.stringify(HOUSE) });
    if(r.ok){ HOUSE = await r.json(); markDirty(false); }
    else { alert("Falha ao salvar ("+r.status+")"); }
  }catch{ alert("Falha de conexão ao salvar"); }
}
```
Call `initHouseEditor()` once at startup, after `loadHouse()`.

- [ ] **Step 5: Verify editor behaviour with Playwright**

Backend on a free port (as in Task 6). Assertions:
- `browser_evaluate` → editor present in live: `!document.getElementById('houseEditor').hidden` is `true`.
- Simulate adding a room programmatically through the same code path:
```javascript
() => { TOOL='room';
  const f = curFloor(); const before = f.rooms.length;
  pushHistory(); f.rooms.push({id:'rx',name:'test',polygon:[[0,0],[2,0],[2,2],[0,2]]}); afterEdit();
  return { added: f.rooms.length - before, dirty: !document.getElementById('hedDirty').hidden }; }
```
Expected `{added:1, dirty:true}`. Then call `saveHouse()`, reload, assert the room persisted (`HOUSE.floors[0].rooms.some(r=>r.name==='test')`). Then undo path: `undo()` returns rooms to `before`.
- **Demo/companion negative check:** navigate to the deployed-style demo path or force `MODE`; assert `document.getElementById('houseEditor').hidden === true` when `MODE!=="live"`.
- Manual smoke: open Wavr Desktop, draw a room on floor 0, add a floor, draw a room there, Salvar, reload → both persist; Ctrl+Z undoes.

- [ ] **Step 6: Impeccable design pass (project rule) + commit**

Run `/polish` on `frontend/index.html` then `/audit` (CLAUDE.md mandate for client-facing screens). Apply fixes.
```bash
git add frontend/index.html
git commit -m "feat(frontend): house maquette editor — rooms/walls/stairs, multi-floor, undo/redo, save (live-only central)"
```

---

### Task 8: Docs + roadmap

**Files:**
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Move Sub-plano F Phase 1 to shipped**

Add under "Shipped": "**House maquette editor (Sub-plano F Ph.1)** — multi-floor, top-down, editable geometry (room polygons, walls, stairs) in meters, persisted via `PUT /api/house` (central-only), rendered in the radar. Authored geometry as the coordinate frame; wall-occlusion fusion (B2), camera homography (spec A), and plan/CAD upload (F2/F3) are follow-ons." Note the follow-on specs under "Now / next".

- [ ] **Step 2: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs: house maquette editor Phase 1 shipped; note follow-on specs (A, B2, F2/F3)"
```

---

## Self-Review

**Spec coverage:** v2 model (T1) ✓ · migration (T1) ✓ · validation (T2) ✓ · point-in-polygon room assignment (T3) ✓ · atomic persistence (T4) ✓ · `PUT /api/house` central-only + in-memory sync (T5) ✓ · render geometry + floor selector + demo default + companion read (T6) ✓ · editor tools + undo/redo + save + live-only gating (T7) ✓ · roadmap (T8) ✓. Backdrop/F2, auto-build/F3, camera homography/A, wall-occlusion/B2 are explicitly out of scope and carried as data-model placeholders (`backdrop:null`) + roadmap notes.

**Placeholder scan:** No TBD/TODO. Backend steps carry full code + exact commands. Frontend steps carry the core JS/CSS/markup and concrete Playwright assertions (single-file HTML has no unit harness — verification is behavioural, stated honestly). The `renderHouse` selection-highlight detail in T7 Step 3 is described with the exact handler to add.

**Type consistency:** `load_house_map`/`save_house_map`/`validate_house_map`/`room_at`/`room_names`/`HouseMapError` names are consistent across T1–T5 and the route. Frontend `HOUSE`, `currentFloor`, `renderHouse`, `buildFloorSelect`, `pushHistory/undo/redo`, `saveHouse`, `curFloor`, `m2px`, `RADAR_SCALE` are consistent across T6–T7. `RADAR_SCALE`/`m2px` reuse the radar's existing meters→px transform — the implementer must confirm the exact name of that scale constant in `index.html` and align `m2px`/`px2m` to it (the one place to reconcile with existing code).
