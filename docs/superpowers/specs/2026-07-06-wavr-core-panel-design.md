# Wavr Core Panel — Design

**Date:** 2026-07-06
**Status:** Approved (Augusto, 2026-07-06)
**Depends on:** [Wavr product taxonomy](2026-07-06-wavr-product-taxonomy-design.md), [Wavr Core on the 12T Pro](2026-07-06-wavr-core-12t-pro-design.md)
**Scope:** The on-device screen UI for a dedicated phone living as a **Wavr Core** — an
always-on, glanceable home-status panel that wakes to the full dashboard on touch. First
target: a rooted Moto G9 Power (Core Standard) in a stand; applies to any Core device.

## Purpose

A Core is a dedicated always-on device with its screen visible in the room. Left on the
default browser or Android home screen, that screen is wasted. The Core Panel turns it
into a calm, at-a-glance home-status face (like a smart display / Echo Show), while
keeping the full interactive dashboard one touch away. It is the "Core's panel" the
pairing screen already references ("Enter the 8-digit code shown on the Core's panel").

## Character (locked)

- **Adaptive:** ambient glance face by default; wakes to the interactive dashboard on
  touch; returns to ambient after inactivity.
- **Landscape.** The device lives on its side in a stand (smart-display convention).
- **Calm + privacy-first.** No camera feed on the ambient face. The face reads as "all
  quiet" from across the room, not as a surveillance monitor.

## Approach — `?core` mode inside `index.html` (not a separate file)

Core mode is a flag (`?core` URL param, or an equivalent `window.WAVR_CORE`) that the
existing dashboard reads at boot. When set, it renders the **ambient face as an overlay**
on top of the live dashboard, shown by default. This **reuses everything**: the ambient
face reads the same live `RoomState` the dashboard already receives over `/ws/live`, and
`/api/health` it already polls — no duplicated WebSocket or state-parsing plumbing.

Rejected alternative: a separate `core.html` surface. Cleaner separation, but it would
duplicate the `/ws/live` connection and `RoomState` parsing in a second code path — worse
for correctness. Reuse wins. (The ambient overlay stays a well-bounded component so
`index.html`'s size cost is contained.)

## The ambient face (default view)

Presence-hero, calm, landscape:

- **Center (the hero):** occupancy — big **"N EM CASA"** / **"CASA VAZIA"**.
- **Top-left:** clock + date.
- **Top-right:** Core health — `Core ✓  Rede ✓` (degrades to a warning glyph on trouble).
- **Bottom:** active rooms (`● cozinha  ● quarto-1`) + unresolved alert count.

Every element derives from data already flowing to the dashboard (`RoomState` occupancy
per room, `/api/health`, notification/alert count). No new backend endpoints.

## Wake / idle behavior

- **Touch anywhere** → the ambient overlay dissolves, revealing the full live dashboard
  (the existing five tabs: Início / History / Rede / Dispositivos / Sistema).
- **Inactivity (~60s)** → the ambient face returns.
- Screen stays on (device `stayon`); fullscreen immersive (no Android chrome).

## Cameras

Presence-only. The Core device's cameras feed fusion/YOLO for presence detection and
**never** appear on the ambient face. A live feed is reachable only in the existing
**Câmeras** view after touch-to-wake. (Preserves the calm/privacy character.)

## Delivery tiers (the UI is identical across all three)

The Core Panel UI built here is **tier-agnostic** — the same `?core` view runs unchanged
as delivery escalates:

1. **Browser kiosk (v1, now):** the device browser opens `http://localhost:8000/?core`
   fullscreen. Zero new build. This spec's buildable target.
2. **Kiosk launcher app (next):** a lightweight app set as the Android default launcher,
   so the device **boots straight into Wavr** with no Android home screen — ~90% of the
   "dedicated appliance / it's an app" feel without a custom OS.
3. **Wavr OS (far / endgame):** a custom ROM (the G9 already runs LineageOS — a step down
   this path) that boots into Wavr as its entire UI. Large; its own spec when the trigger
   arrives.

**Wavr OS v1 scope (approved 2026-07-06):** tiers 1 **and 2** are in scope now, built as
two phases — Phase 1 the Core Panel UI (this doc's core), Phase 2 the boots-into-Wavr
layer (kiosk launcher + backend autostart on boot + locked fullscreen), on the G9 and the
12T. Tier 3 (a real custom ROM) stays far-tier with its own spec.

## Data sources (all existing)

- `/ws/live` → `RoomState` (occupancy per room) — drives the hero + active rooms.
- `/api/health` → Core/network health — drives the top-right status.
- Existing notification/alert count → drives the alert badge.

No new backend endpoints or DB changes.

## Out of scope (YAGNI for v1)

- Night dimming / auto-brightness.
- Any camera imagery on the ambient face.
- New dashboard tabs or new backend endpoints.
- The kiosk launcher app and Wavr OS (tiers 2–3) — separate specs.

## Ownership

This terminal owns Wavr Desktop + Core. Implementation of the `?core` overlay lives in
`frontend/index.html` (frontend-web-engineer). Kiosk launch/fullscreen ops belong to the
Core-device setup (this terminal), not to the UI.
