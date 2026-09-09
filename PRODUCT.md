# Product

What Wavr is, who it is for, and the design principles it is held to. The
architecture decisions behind these choices are in `docs/adr/`.

## Users

- **The owner-operator:** monitors their own home on a local machine (localhost dashboard). Context: glances at room occupancy/vitals while doing other work; toggles sources on/off at will. Job: "is someone home / in that room, and why does the system think so?"
- **Portfolio viewers (recruiters, engineers):** judge the engineering quality of a multi-modal sensor-fusion system in under a minute — from this public repository and its README, or by cloning and running the offline demo (`python -m wavr.serve`, simulated data, zero hardware). There is **no hosted online demo** (local-only by design).

## What Wavr is

**Wavr is the spatial layer for the local network.** It takes a network that
today only carries traffic, plus whatever sensing hardware is already in the
building, and turns them into a live, explainable model of the physical space:
which rooms are occupied, how sure we are, and on what evidence.

The local network is the *foundation*, not one sensor among several. Every
device that joins it is already telling you something — that it is here, when it
arrived, when it left. Cameras, Bluetooth, radar and CSI refine that into
room-level and eventually position-level answers. They do not replace it.

## What Wavr is not

Naming these matters more than it looks: the gravitational pull on any
smart-home project is to keep adding device integrations until it is a worse
version of something that already exists.

- **Not a Home Assistant replacement.** HA is the device-integration and
  automation layer and is very good at it. Wavr is the perception layer. Where
  Wavr needs the state of, or control over, a generic smart-home entity, it goes
  through HA rather than rebuilding the ecosystem.
- **Not a Matter/MQTT/IoT-control platform.** Wavr speaks to those standards; it
  does not compete with them.
- **Not a vendor-integration project.** Wavr writes native integrations only
  where they directly improve *sensing, spatial context, discovery or Nodes*. A
  hundred adapters for lights and thermostats is somebody else's job, already
  done.
- **Not an AI product.** Wavr must be fully useful with no LLM configured
  anywhere. The MCP server is how an external AI *reads* Wavr; it is not a
  dependency, and no model is embedded.
- **Not a claim to have invented Wi-Fi sensing.** The techniques are published
  research. What Wavr contributes is operationalising them: installable on
  hardware people already own, local-first, fused honestly, and legible.

Wavr must work with none of the above present. When Home Assistant *is* on the
network, Wavr should find it and be richer for it — never require it.

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
4. **Light footprint** — a static HTML shell plus plain `<script>` modules in `frontend/js/`; no build step, no bundler, no frameworks. Must stay fast on any machine, and readable by opening the file.

## Accessibility & Inclusion

WCAG AA baseline: text contrast ≥4.5:1, `prefers-reduced-motion` respected, buttons/toggles labeled, status conveyed by more than color alone.
