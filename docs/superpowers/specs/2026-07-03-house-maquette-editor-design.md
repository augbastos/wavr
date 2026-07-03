# House maquette editor — design (Phase 1)

> Sub-plano F, Phase 1. Give Wavr Desktop an editable, multi-floor, top-down 2D editor for
> the "shape of the property" — rooms, walls, stairs — in meters. This authored geometry
> becomes the coordinate frame every position source (mmWave, camera homography) renders
> and reasons in. Approved via brainstorming 2026-07-03.

## Why / the honest frame

The user asked to "build my house inside Wavr so detection areas are more precise," and
asked whether the ESP32s could sense walls. They can't: WiFi CSI senses signal
perturbation (presence, motion, breathing), not geometry — reconstructing walls from CSI
is research-grade and crude. **So the map is AUTHORED (the user draws it); the live person
positions are SENSED (mmWave/camera) and overlaid.** That split is the whole design.

Phase 1 delivers the authored geometry + the editor + persistence + rendering. It does NOT
yet make the fusion *use* walls (occlusion) — that is a deliberate follow-on (spec B2).

## Scope

**In (Phase 1):**
- A v2 house-map data model: floors → rooms (polygons), walls (segments), features
  (stairs/doors/windows), in meters. Backward-compatible with the v1 rectangle map.
- `PUT /api/house` to persist the whole doc atomically, validated, central-only.
- A top-down 2D editor panel in the dashboard (Wavr Desktop / loopback central), multi-floor,
  fully editable: add/move/delete rooms, walls, stairs; undo/redo; grid snap; meters.
- Rooms-as-polygons feeding **point-in-polygon** room assignment for a target's x/y.
- Rendering the geometry in the existing radar view; companion views it read-only; the
  public demo renders the default and cannot write.

**Out (explicit — parked, but the data model accommodates them):**
- **F2 — plan as backdrop:** upload an image, set scale (mark one real measurement), trace
  over it. The model reserves `backdrop` per floor.
- **F3 — auto-build from a floor plan / CAD:** raster→walls is unreliable CV; DXF/CAD is
  parseable but needs interpretation. Ambitious, later, non-blocking.
- **Spec A — camera→position:** per-camera homography (image px → floor x/y). Needs this map.
- **Spec B2 — fusion uses geometry:** wall occlusion (a camera can't see through a wall →
  its weight is dropped for targets behind one), room-boundary snapping. The precision
  payoff of walls lands here, on top of Phase 1.

## Data model (`housemap.py` v2)

```jsonc
{
  "version": 2,
  "units": "m",
  "floors": [
    {
      "id": "f0",
      "name": "Térreo",
      "level": 0,                       // int; 0 = ground, 1 = first floor, -1 = basement
      "rooms": [
        { "id": "r1", "name": "sala", "polygon": [[0,0],[4,0],[4,3],[0,3]] }
      ],
      "walls": [
        { "id": "w1", "a": [4,0], "b": [4,3] }
      ],
      "features": [
        { "id": "s1", "type": "stairs", "at": [3.5,2.5], "to_level": 1 }
        // type ∈ {stairs, door, window}; geometry per type (point or short segment)
      ],
      "backdrop": null                   // Phase 2 placeholder: {image_ref, m_per_px, offset, opacity}
    }
  ]
}
```

- **Polygons, not rectangles**, so rooms can be any shape. A polygon is an ordered list of
  `[x,y]` vertices in meters; the editor keeps it simple (starts as a rectangle you reshape).
- **Walls** are independent segments (not derived from rooms) so the user can mark interior
  walls, half-walls, and openings precisely. Stored now, *used* by fusion in B2.
- **Features** are lightweight typed annotations. `stairs` carries `to_level` so multi-floor
  connectivity is captured (used later for cross-floor logic; Phase 1 just draws it).
- **Multi-floor**: `floors[]` with integer `level`. Sources (mmWave/camera) are per-floor.

### Backward compatibility (v1 → v2)

`load_house_map` stays non-raising and gains a migration: a v1 doc
(`{"rooms":[{name,x,y,w,h}]}` with no `version`) is converted to a single `level:0` floor
whose rooms become rectangle polygons. `DEFAULT_MAP` is re-expressed as a v2 doc. Malformed
input still falls back to the default. Every existing caller keeps working.

## Persistence + API

Geometry is **local config** (like the in-app camera list), not sensitive sensing data —
but it is still a state change, so writes are gated exactly like the other control routes.

- `GET /api/house` — already exists; returns the v2 doc. Readable by central and by the
  companion viewer (view-allowed for the `user` role, so a paired phone renders the same
  map). The **public demo is backend-less** → it never calls this; it renders a built-in
  default map client-side (same as its simulated state today).
- `PUT /api/house` — **new**. Body = the full house doc. Steps: validate → write atomically
  (temp file + `os.replace`). Gated: **central-only** (`require_central` under multi-device;
  loopback + `X-Wavr-Local` CSRF header otherwise), live-only. Returns the stored doc.
- **Undo/redo is client-side**: the editor keeps a bounded history stack of doc snapshots;
  the server only ever persists the current doc. Keeps the server simple and stateless.
- Storage: a writable `house.json` at the configured path (`WAVR_HOUSE_MAP`, already the
  read source). Chosen over a SQLite store because it is a single human-inspectable doc and
  reuses the existing `load_house_map` read path; atomic replace avoids torn writes.

### Validation (rejects, does not sanitize silently)

- Top-level: `version` int, `units == "m"`, `floors` a non-empty list.
- Floor: unique `id` + unique `level`; `name` a string.
- Room: `polygon` ≥ 3 vertices, each `[finite x, finite y]`; non-self-intersecting is
  **not** enforced in Phase 1 (documented limitation — the editor discourages it).
- Wall: `a`/`b` finite points. Feature: known `type`, finite geometry.
- Reasonable caps (e.g. ≤ 64 floors, ≤ 512 rooms/floor, ≤ 4096 walls/floor) so a bad PUT
  can't blow up memory. On any failure → `422` with a machine-readable reason; nothing is
  written.

## Editor UI (single-file `frontend/index.html`, new panel)

- Lives in the dashboard, **live-only central** (MODE === "live"); hidden in demo. The
  companion (token viewer) may *render* the map but the editing tools are central-only.
- **Top-down SVG canvas** reusing the radar's SVG conventions (same meters→px transform),
  with a **floor selector** (tabs or a dropdown: Térreo / 1º / …; add/remove floor).
- **Tools:** select/move · add room (drag a rectangle → editable polygon, drag vertices to
  reshape, right-drag to add/remove a vertex) · add wall (click-drag a segment) · add
  stairs/door/window (place an element) · delete. **Undo/redo** (Ctrl+Z / Ctrl+Y over the
  client history stack). **Grid snap** (0.25 m default) with a meters readout.
- **Save** → `PUT /api/house`; a dirty indicator until saved. The live radar overlays
  targets on the *same* geometry, so editing and monitoring share one coordinate frame.
- Zero external requests (the whole-file invariant): all SVG/DOM, no libraries.

## How Phase 1 improves detection (and where it stops)

- **Built now, wired later:** `room_at` does **point-in-polygon** room assignment — accurate
  for L-shapes / non-rectangular rooms where the old `x,y,w,h` rectangle was wrong. It is a
  tested building block but is **not yet wired into fusion**: today fusion still assigns a
  target to the room its source reports (`event.room`), not by geometry. Wiring point-in-
  polygon assignment (so a source's x/y picks the room) lands with spec A/B2. What Phase 1
  delivers now is the **authored coordinate frame** that makes camera homography (spec A) and
  mmWave x/y meaningful and comparable — plus the editor + rendering.
- **Not yet (B2):** using walls for occlusion / weighting. Phase 1 stores walls and draws
  them; it does not change fusion weights. This is called out so "more precise detection"
  is not oversold — the wall-driven precision is the next spec.

## Testing

- **Backend (pytest, offline):** v1→v2 migration; `DEFAULT_MAP` is valid v2; malformed →
  default (non-raising); `PUT` validation table (bad polygon, dup level, non-finite,
  over-cap → 422, nothing written); atomic write leaves no partial file on failure;
  point-in-polygon room assignment (inside / on-edge / outside, concave room).
- **Route gating:** `PUT /api/house` refused off-loopback without central role / CSRF
  (reuses the multi-device integration harness).
- **Frontend:** the mode invariants — editor tools present only in live, absent in demo;
  companion renders but cannot PUT — asserted in the existing style; plus a manual editor
  smoke (draw a room on each of two floors, save, reload, geometry persisted).

## Privacy / security

- Geometry is authored config, not sensing data: no targets, vitals, frames, or MACs are
  involved. `house.json` is local; nothing leaves the box.
- Writes are central-only + CSRF + loopback (multi-device: `require_central`), matching the
  camera-config and control routes. The companion viewer is read-only; the public demo
  renders the default and has no write path.
- Live-only editor UI: the whole panel is gated on `MODE === "live"`, so it never appears
  in the backend-less demo.

## Files touched

- `backend/wavr/housemap.py` — v2 model, migration, validation helpers, point-in-polygon.
- `backend/wavr/app.py` — `PUT /api/house` (gated), wired to the store/writer.
- `frontend/index.html` — the editor panel + tools + floor selector + save; radar renders
  the v2 geometry.
- `backend/tests/test_housemap.py` (+ route test) — the cases above.
- `docs/ROADMAP.md` — Sub-plano F Phase 1 from backlog → in progress/shipped on merge.
