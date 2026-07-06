# Wavr — Product Taxonomy Design

**Date:** 2026-07-06
**Status:** Approved (Augusto, 2026-07-06)
**Scope:** Names, roles, and buildable-now vs. queued split for the whole Wavr product family.

## Purpose

Wavr has grown from one desktop dashboard into a family of surfaces (desktop, mobile,
a dedicated appliance, physical nodes, an integration layer). Without fixed names and
role boundaries, the pieces blur into each other and parallel work steps on itself.
This spec locks the taxonomy so every future spec, plan, and terminal knows exactly
which product it touches and what that product is allowed to be.

## The five products

| Name | What it is | Role | State |
|------|------------|------|-------|
| **Wavr Desktop** | App for Windows / Linux / macOS (Windows first). | Runs as **central** or **user** per permission. | Exists — the app built so far. |
| **Wavr Mobile** | App for Android / iOS (Android first). | **Admin** or **user**; limited, generally **not** a central. | Building (separate terminal). |
| **Wavr Core** | The dedicated appliance — independent, always-on, centralizes everything. | **Admin by nature** — it *is* the network authority. Users work in its space. | Prototype on the 12T Pro. |
| **Wavr Nodes** | ESP32 + camera + physical switch, powered by outlet or USB. Amplify presence per room, talk directly to the Core. | Sensor extenders — no authority of their own. | Queued (needs ESP32 hardware). |
| **Wavr MCP** | The integration layer. The admin plugs it into agentic OS, local LLM, games, VR/XR/AR. | The single, opt-in, revocable outbound door. | Exists — MCP server + Connectors screen. |

## Mental model

**Core = brain · Nodes = limbs · Desktop/Mobile = windows into it · MCP = the door to
the digital world.** Everything local, everything under the admin. A user never talks to
a raw sensor; they talk to a central (Desktop-central or Core), which owns the fused
picture. Nodes feed the central; they never decide.

## Design principles (locked — the invariants that make Wavr *Wavr*)

1. **Core is admin by nature.** The dedicated appliance is the authority of the network.
   A Desktop can also be central, but the Core is central by definition — it's what you
   buy to stop depending on a PC or phone being awake.

2. **Node kill-switch invariant — remote-OFF but never remote-ON.** Every node has a
   mandatory physical switch. The Core (admin) can turn a node **off** remotely, but
   **cannot turn it on** remotely. Re-enabling sensing in a room requires physically
   flipping the node's switch — presence at the node. Nobody, not even the admin, can
   silently start sensing a remote room from a screen. Hardware sovereignty over software
   convenience. This is the hard line that separates Wavr from surveillance.

3. **One door out.** The MCP / Connectors screen is the single outbound surface —
   opt-in, per-service, revocable, transparent. Everything else guarantees zero egress.
   (Owned by the MCP terminal; see ownership below.)

4. **Kit or à la carte.** A Core may ship bundled with 2–3 Nodes as a starter kit; Nodes
   also sell separately. Outlet- or USB-powered, plug-and-enable per room.

## Terminal ownership (who builds what — avoids code collisions)

Wavr is one repo (`C:\IA\wavr`), so parallel terminals must not edit the same surfaces.
The split:

- **This terminal → Wavr Desktop + Wavr Core.** The desktop app, the backend/central, the
  calibration + dashboard, and the Core appliance (incl. the 12T Pro prototype path).
- **Mobile terminal → Wavr Mobile.** The Capacitor companion app (`mobile/`).
- **MCP terminal → Wavr MCP.** The MCP server + the Connectors screen + integrations.

Nodes are Core-adjacent (they talk to the Core, not the phone), so their firmware/protocol
design belongs to this terminal when it's time to build them.

## Buildable now vs. queued

**Now (in this terminal's scope):**
- Wavr Desktop — exists; ongoing polish (calibration wizard, compact meter, shell).
- The role model (central / admin / user) — exists.
- **Wavr Core prototype on the 12T Pro** — run the Wavr backend on the phone as a
  dedicated, always-on central (Termux + Python backend). Proves "Wavr independent of
  the PC." First concrete step toward the appliance.

**Queued (design now where useful, build when the trigger arrives):**
- **Wavr Mobile** — building in the mobile terminal.
- **Wavr Core productization** — 12T Pro prototype → a real dedicated appliance.
- **Wavr Nodes** — needs ESP32 hardware. The **firmware/protocol + the kill-switch
  invariant can be designed now** (its own spec) so the hardware slot is ready.

## Out of scope for this spec

Each buildable piece (Core prototype, Nodes firmware, appliance productization) gets its
**own** spec → plan → implementation cycle. This document only fixes the names, roles,
principles, and ownership. It is a reference, not an implementation plan.
