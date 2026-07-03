# ADR-0005 — MCP control boundary (the "brain on Home Assistant")

## Status

Accepted (design) — 2026-07-03. Extends the shipped read-only MCP server toward
control, on the axis chosen for Wavr's device story ("brain on Home Assistant", not a
device-driver hub).

## Context

Wavr ships a **read-only** MCP server (`RoomState` / house map to agents). The product
direction is for the MCP to become the way an AI agent both **sees** the home and
**acts** on it — connect a camera, a mic, control a Wi-Fi lamp, etc. Two ways to get
there were weighed:

- **Axis A — Wavr becomes its own hub:** build device drivers (RTSP/ONVIF, MQTT/Tuya,
  BLE, …) and a native control plane. **Rejected** — an infinite adapter treadmill that
  duplicates a decade of Home Assistant, blurs Wavr's identity (explainable presence
  fusion), and competes where a solo project can't win.
- **Axis B — Wavr as the brain on top of Home Assistant:** the MCP **reads** HA
  entities and **triggers HA services** for control. HA already speaks 2000+ devices;
  Wavr stays the explainable-presence brain and the agent-facing orchestration layer.
  **Chosen.**

Moving the MCP from read-only to read+write is a security-boundary decision, so it gets
this ADR before code.

## Decision

1. **Control = delegation, never absorption.** Wavr does not drive any device directly.
   Actuation is a call to a Home Assistant service via HA's local API. Wavr remains the
   fusion/explainability brain; HA executes. (This is an extension of the existing
   `RoomState → RulesEngine → MQTT` seam — control lives downstream, not in the core.)
2. **Opt-in, default read-only.** The control tools sit behind an explicit flag
   (`WAVR_MCP_CONTROL`), off by default. The read-only tools remain always available.
3. **Local + authenticated.** Control runs only on the loopback / authenticated-LAN
   surface (see [ADR-0006](0006-authenticated-lan-access.md)); a remote agent cannot
   actuate without the same auth. HA is reached over the LAN with a locally-stored HA
   token — no cloud.
4. **Explicit consent for sensitive actuation.** Enabling a camera or microphone, or any
   physical actuation, requires explicit user consent — never silent. The camera
   boot-OFF invariant ([ADR-0002](0002-privacy-boundaries-ram-only.md)) holds: the MCP
   cannot silently turn a camera on. The sensitive check is applied to **both** the
   service *and the target entity* — a benign `switch.turn_on` / `scene.turn_on` aimed at
   a camera, lock, or opaque scene is refused just the same, so no non-sensitive service
   can be used as a back door. Sensitive domains: camera, media_player, lock,
   alarm_control_panel, cover, valve, siren, lawn_mower; opaque indirection (scene,
   script, automation, group) is sensitive-by-default.
5. **Scoped tools, no arbitrary control.** `get_ha_entities()` (read) and
   `call_ha_service(domain, service, target)` gated by an **allowlist** of permitted
   services + the consent rule — never "run arbitrary HA action" or code execution. The
   target must be exactly one concrete `domain.object_id`: `all`, wildcards and lists are
   refused so a single call can never actuate a whole domain. Every call (refused or
   allowed) is logged for audit.
6. **Zero exfil.** Control calls stay local (Wavr → HA on the LAN). RoomState, x/y
   targets, and vitals never leave via the MCP; nothing is sent to the cloud.

## Consequences

- A new write/control surface — **must be re-audited** (privacy + injection + consent
  bypass) before it is enabled anywhere by default.
- Needs local HA connection config (base URL + long-lived token), stored like other
  secrets — never committed.
- Keeps Wavr's identity intact: it is the explainable brain an agent talks to, which can
  also *ask HA to act* — not a half-built smart-home hub. Reinforces
  [ADR-0004](0004-defensive-only-reject-offensive.md)'s "integration over hype" stance.
- The read-only MCP is unchanged and remains the safe default.
