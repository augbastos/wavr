# Wavr — Roadmap

> This roadmap deliberately separates **what's buildable now** from **long-horizon
> vision**. Tiers are ordered by what they actually cost — engineering time,
> ~€15 of hardware, a GPU / open research, or a whole company plus regulatory
> work. Later tiers are direction, not commitments.

## Now — in progress (just engineering time)

- **Defensive LAN inventory ("Wavr Net")** — scan the LAN the host is already on
  for device-presence as a house-level occupancy hint. Defensive only
  (see [ADR-0004](adr/0004-defensive-only-reject-offensive.md)).
- **Chaos-simulator scenarios + public demo** — scripted multi-room scenarios
  driving the simulator; the off-localhost demo runs on synthetic data and makes
  zero LAN requests ([ADR-0002](adr/0002-privacy-boundaries-ram-only.md)).
- **These ADRs** — RuView audit, privacy boundaries, non-medical scope,
  defensive-only stance.
- **Read-only MCP server** — expose current `RoomState` / history to agents as a
  read-only surface (no control, no writes).

## Months — ~€15 of hardware, still a solo weekend

- **mmWave LD2450 bring-up** — HLK-LD2450 over USB serial for real per-person
  x/y ([ADR-0001](adr/0001-ruview-audit-mmwave-over-fork.md)). The parser and
  `SensorSource` are **already written and mock-tested**; this tier is just
  running it on the physical device.
- **MQTT Home Assistant discovery** — publish occupancy/confidence via MQTT
  discovery so Wavr appears as native HA entities (opt-in; derived state only,
  never targets or vitals).
- **Dashboard as a PWA** — package the existing single static frontend as an
  installable Progressive Web App (no framework, no build step added).

## Years — needs a GPU or open research

- **Live camera YOLO-pose** — standing/sitting/lying from RTSP cameras via the
  `[camera]` extra; cameras stay boot-OFF and frames are never persisted
  ([ADR-0002](adr/0002-privacy-boundaries-ram-only.md)).
- **Cross-source track association** — fuse targets from multiple sensors in one
  room (Kalman filtering + Hungarian assignment) instead of best-source
  pass-through.
- **Real fall detection** — lying posture + location + duration, on top of pose
  and track association. A research demo, **not** a certified safety system
  ([ADR-0003](adr/0003-not-a-medical-device.md)).
- **Local LLM narrator** — replace the opt-in Gemini narrator with a fully local
  model (e.g. Ollama), removing the only cloud egress entirely.

## Vision — needs a company + regulatory work (not a solo weekend)

> Long-horizon direction only. These require staffing, capital, certification,
> and/or clinical validation. Listed to show where the architecture *could* go —
> **not** as roadmap commitments.

- **B2B eldercare / school deployments** — multi-site, supported installs.
- **Clinical-grade vitals** — validated breathing/heart measurement. Would cross
  the medical-device line and everything that implies (CE marking, QMS, clinical
  evaluation) — see [ADR-0003](adr/0003-not-a-medical-device.md).
- **Hardware product with tiers** — a purpose-built sensor appliance rather than
  a laptop + USB radar.
- **VR/AR spatial SDK** — expose live room/target geometry to spatial apps.
- **6G JCAS sensing** — joint communication-and-sensing as the radio layer
  evolves; entirely dependent on external standards and hardware.

## Rejected — will not build

- **Offensive network tooling** — entering/surveilling networks the host is not
  authorized on. Rejected on legal and identity grounds; see
  [ADR-0004](adr/0004-defensive-only-reject-offensive.md).
