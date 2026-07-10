# Wavr — Roadmap

> This roadmap deliberately separates **what's buildable now** from **long-horizon
> vision**. Tiers are ordered by what they actually cost — engineering time,
> ~€15 of hardware, a GPU / open research, or a whole company plus regulatory
> work. Later tiers are direction, not commitments.

## Shipped (2026-07-03)

- **Defensive LAN inventory ("Wavr Net")** — device inventory (offline OUI vendor +
  device type), rogue-device alerts, opt-in port/speed/WOL utilities, wired to
  `/api/inventory` + `/api/alerts` and a live-only dashboard panel. Defensive only
  ([ADR-0004](adr/0004-defensive-only-reject-offensive.md)).
- **Chaos-simulator scenarios** — scripted wifi-drop / camera-flicker / multi-target /
  fall scenarios with a live-only-invariant regression test.
- **BLE presence source** — the host Bluetooth adapter as a new modality (lazy `bleak`).
- **Sensor-health → trust** — a stale/dead source's weight decays automatically, so
  `confidence = agreement × strength` stays honest; per-source health in the dashboard.
- **MQTT Home Assistant auto-discovery** — occupancy/confidence appear as native HA
  entities (opt-in; derived state only, never targets or vitals).
- **Read-only MCP server** — `RoomState` / house map exposed to agents.
- **Multi-device (desktop-central + LAN companions)** — a mobile app and peer PCs on the
  same Wi-Fi connect to the desktop as authenticated `user` / `central` clients: local
  8-digit pairing (rate-limited), per-device hashed tokens, single-use WS tickets,
  revocation, an in-subnet real-peer check, and a **companion viewer** (token-authed,
  read-only dashboard). Opt-in, default-OFF, zero-cloud. **Local TLS** (auto self-signed
  cert via `python -m wavr.serve`) closes the plaintext-sniff gap
  ([ADR-0006](adr/0006-authenticated-lan-access.md)). Security-audited (C1/H1/M1–M3 fixed).
- **MCP "brain on Home Assistant"** — read access to HA entities **plus** a gated control
  tool that asks HA to run a service (Wavr never becomes a device driver). Opt-in
  (`WAVR_MCP_CONTROL`, default-OFF), allowlist + consent refusal on both the service and
  the **target entity**, camera boot-OFF held, every call audit-logged, zero exfil
  ([ADR-0005](adr/0005-mcp-control-boundary.md)). Write surface security-audited.
- **PWA companion** — the dashboard is an installable PWA (manifest + service worker),
  caching only the shell, zero external requests.
- **House maquette editor (Sub-plano F Ph.1)** — multi-floor, top-down, editable
  geometry (room polygons, walls, stairs) in meters, persisted via `PUT /api/house`
  (central-only), rendered in the radar. Authored geometry as the coordinate frame;
  wall-occlusion fusion (spec B2), camera homography (spec A), and plan/CAD upload
  (specs F2/F3) are follow-ons.
- **Fall / no-motion suspicion (research demo)** — a "lying" posture (from the existing
  YOLO-pose heuristic) that persists outside an operator-marked bed/rest zone for a
  configurable dwell fires one edge-triggered, room+duration-only alert into the shared
  alert stream. Bed/rest zones are drawn per room in the map editor and saved via the
  same `PUT /api/house`. Opt-in, default-OFF (`WAVR_FALL_DETECT`); a room-level dwell
  rule, not full cross-frame track association — still explicitly a research
  demonstration, never a certified medical/fall-detection device
  ([ADR-0003](adr/0003-not-a-medical-device.md)).
- **ADRs 0001–0006** + this roadmap; relicensed **AGPL-3.0**; a security + performance
  audit pass (vitals never persisted, WS Origin check, sqlite off the event loop,
  bounded queues, capped ping sweeps).

## Now / next — just engineering time

- **House maquette follow-ons** — camera homography for plan-to-camera alignment
  (spec A), wall-occlusion fusion weighting (spec B2), plan-as-backdrop rendering
  (spec F2), auto-build geometry from plan/CAD upload (spec F3).
- **Packaging** — a Tauri desktop shell (tray, auto-start) around the existing backend +
  dashboard, so the "Wavr desktop is the central" story ships as one installable app
  (the mobile-companion PWA is already installable; this is the desktop wrapper).

## Months — ~€15 of hardware, still a solo weekend

- **mmWave LD2450 bring-up** — HLK-LD2450 over USB serial for real per-person
  x/y ([ADR-0001](adr/0001-ruview-audit-mmwave-over-fork.md)). The parser and
  `SensorSource` are **already written and mock-tested**; this tier is just
  running it on the physical device.

## Years — needs a GPU or open research

- **Live camera YOLO-pose** — standing/sitting/lying from RTSP cameras via the
  `[camera]` extra; cameras stay boot-OFF and frames are never persisted
  ([ADR-0002](adr/0002-privacy-boundaries-ram-only.md)).
- **Cross-source track association** — fuse targets from multiple sensors in one
  room (Kalman filtering + Hungarian assignment) instead of best-source
  pass-through. Would sharpen the fall/no-motion dwell rule (Shipped, above) from
  room-level to per-person.
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
