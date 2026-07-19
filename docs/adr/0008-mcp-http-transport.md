# ADR-0008 — MCP over streamable-HTTP (Slice 1: secure read transport)

Status: **accepted** (2026-07-06, owner; shipped). Extends [ADR-0005](0005-mcp-control-boundary.md)
(MCP control boundary) and [ADR-0006](0006-authenticated-lan-access.md) (authenticated LAN access).
Full design detail: [`docs/mcp-http-transport-spec.md`](../mcp-http-transport-spec.md).

## Context

Wavr already exposes a **stdio** MCP server (ADR-0005) so a local agent can read `RoomState` and
the house map, with a gated control tool. But a paired *networked* agent (a phone, a second box)
had no way to query presence over the LAN — stdio is same-host only. The demand is real (an agent
on the paired central asking "who is home, which rooms are occupied") and must not weaken any
existing boundary.

## Decision

Mount an **in-app, read-only MCP server over streamable-HTTP** at `/mcp`, behind the **same LAN
auth as every other route** (ADR-0006: paired + cert-pinned; require_local/require_scope). It is:

- **Opt-in, default-OFF** — the `mcp-http` connector (registry gate); an absent row egresses/serves
  nothing, byte-identical to before. Available only when the mount is actually wired (multidevice
  ON + the `[mcp]` extra).
- **Read-only** — no control tool over HTTP. The gated HA-control tool stays stdio-only (ADR-0005).
- **Minimized by default** — a `user`-role device is denied `/mcp` entirely; a paired agent's
  default reach is coarse current state (rooms, bare `person_count` with no identity/geometry/
  vitals, house map/status). Network inventory (vendor/type/ip/make/model only), occupancy history
  (clamped ≤24h, room-level, no identity), the alert stream (kind/severity/room/ts) and the HA
  entity list each need an **explicit per-agent grant**.
- **PII-stripped** — person labels are removed from the MCP read path (same rule as the stdio
  server). No faces, no per-room identity.

## Consequences

- One more egress/serve surface — gated by the connector kill-switch and the pairing auth, so it
  cannot serve while the operator has it off or the device is unpaired.
- The control boundary (ADR-0005) is untouched: HTTP is read-only; actuation remains stdio + the
  allowlist/consent-refusal path.
- Slice 2 (broader tool grants, streaming subscriptions) is deferred; this ADR covers the secure
  read transport only. The design spec carries the wire-level detail and the threat model.
