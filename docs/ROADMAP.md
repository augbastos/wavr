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
- **Read-only MCP server** — `RoomState` / house map exposed to agents; no control.
- **ADRs 0001–0006** + this roadmap; relicensed **AGPL-3.0**; a security + performance
  audit pass (vitals never persisted, WS Origin check, sqlite off the event loop,
  bounded queues, capped ping sweeps).

## Now / next — just engineering time

- **Multi-device (desktop-central + LAN companions)** — a mobile app and peer PCs on
  the same Wi-Fi connect to the desktop as authenticated `user` / `central` clients,
  with local pairing and revocation, staying local + zero-cloud
  ([ADR-0006](adr/0006-authenticated-lan-access.md),
  [design](superpowers/specs/2026-07-03-multi-device-client-auth-design.md)).
- **MCP "brain on Home Assistant"** — the read-only MCP grows read access to HA
  entities + the ability to trigger HA services for control (Wavr never becomes a
  device driver). See [ADR-0005](adr/0005-mcp-control-boundary.md) for the read→write
  control boundary (loopback + auth, explicit consent, camera boot-OFF, zero exfil).
- **Packaging** — a Tauri desktop shell (tray, auto-start) around the existing backend +
  dashboard, and the dashboard as an installable **PWA** for the mobile companion (no
  framework, no build step added).

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
