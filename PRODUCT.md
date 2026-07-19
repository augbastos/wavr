# Product

## Register

product

## Users

- **Augusto (owner-operator):** monitors his own home on a local machine (localhost dashboard). Context: glances at room occupancy/vitals while doing other work; toggles sources on/off at will. Job: "is someone home / in that room, and why does the system think so?"
- **Portfolio viewers (recruiters, engineers):** judge the engineering quality of a multi-modal sensor-fusion system in under a minute — from the public repo + README, or by cloning and running the offline demo (`python -m wavr.serve`, simulated data, zero hardware). There is **no hosted online demo** (local-only by design).

## Product Purpose

Wavr fuses multiple sensing modalities (WiFi CSI, network scan, camera CV, simulator) into one explainable `RoomState` per room — occupancy + confidence 0..1 + per-modality "why". Success: the dashboard makes the fusion legible at a glance (confidence, modality breakdown, timeline), runs light, and never leaks real data off the LAN.

**Position and posture:**  v1 adds optional position (x/y from mmWave radar) and posture (sitting/standing/lying from camera pose estimation), displayed on the room radar when enabled. Fusion is best-source pass-through (no multi-modal track association yet) — a stepping stone to richer context without storage or privacy overhead, since targets are live-only and never persisted.

## Brand Personality

Technical-trustworthy. Precise, calm, legible — a serious measurement instrument, not a gadget. Explainability is the hero: the "why" behind every state is always visible.

## Anti-references

- Sci-fi command-center dashboards (neon glows, radar sweeps, decorative grids) — undermines trust.
- Consumer smart-home cuteness (Google Home/HomeKit softness) — this is an engineering instrument.
- SaaS hero-metric template (big number + gradient accent) and identical card grids as default scaffolding.

## Design Principles

1. **Explain, don't just display** — every state shows its evidence (per-modality breakdown, confidence, explanation string).
2. **Privacy is visible** — the mode label (real vs demo) is always on screen; the demo mode declares itself fake.
3. **Instrument calm** — data changes constantly; the UI must not flicker, shout, or animate for its own sake.
4. **Light footprint** — single static HTML file, no build step, no frameworks; must stay fast on any machine.

## Accessibility & Inclusion

WCAG AA baseline: text contrast ≥4.5:1, `prefers-reduced-motion` respected, buttons/toggles labeled, status conveyed by more than color alone.
