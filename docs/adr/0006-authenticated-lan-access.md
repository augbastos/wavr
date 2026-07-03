# ADR-0006 — Authenticated LAN access (desktop-central, mobile/peer companions)

## Status

Accepted (design) — 2026-07-03. Relaxes the "loopback-only, always" absolute of
[ADR-0002](0002-privacy-boundaries-ram-only.md) **only under explicit opt-in**; the
default stays loopback-only.

## Context

Wavr's runtime model is a desktop/PC **central** (the brain: fusion, real sources,
heavy CV) with lighter **companions** — a mobile app, and possibly other PCs — on the
**same Wi-Fi**. This mirrors the Spotify desktop-plus-companion split: the desktop has
full power; the phone is a low-tier viewer + light sensor + alert receiver.

Today the API is **loopback-only**: a hard-coded peer check (ADR-0002) that is the
load-bearing privacy control. A same-Wi-Fi phone or peer PC cannot reach it. To support
the desktop-central model we must let authenticated LAN peers connect **without**
breaking the two things that define Wavr: surveillance stays local, and nothing goes to
the cloud.

Constraints the product owner set for this model:

- Companions work **only on the same Wi-Fi** — leave the network, lose access.
- Being on the Wi-Fi is **not** sufficient — a device needs the right permission.
- Surveillance stays local; **zero cloud** — so, unlike Spotify Connect, pairing is
  peer-to-peer on the LAN, with no external broker.

## Decision

1. **Opt-in, default unchanged.** Multi-device sits behind an explicit flag
   (`WAVR_MULTIDEVICE`). Off → Wavr is loopback-only exactly as today.
2. **Loopback OR authenticated LAN peer.** When enabled, the peer check accepts a
   request if it is loopback, **or** the peer is in the host's local subnet **and**
   presents a valid, non-revoked per-device token. On-Wi-Fi alone is never enough.
3. **Local pairing, no cloud.** The central shows a short-lived pairing code / QR; the
   companion submits it over the LAN and receives a per-device token. Nothing leaves
   the LAN.
4. **Roles + hierarchy.** Each device token carries a role: `central` (full control —
   sources, cameras, config, and pairing/revoking others) or `user` (read-only
   RoomState view + alerts, optional light telemetry). The DB-owning desktop is the
   root central; it grants `central` or `user` to peers. A second PC can join as a peer
   `central` (full) or as a `user`.
5. **Revocation.** The root central can revoke any device instantly; a revoked token is
   rejected on the next request. Access is also implicitly bounded to the Wi-Fi
   (off-network = unreachable).
6. **Local TLS.** LAN traffic (tokens, live stream) runs over HTTPS/WSS with a
   locally-generated self-signed cert, so tokens and RoomState aren't sniffable on the
   Wi-Fi.
7. **Privacy invariants unchanged.** x/y targets and vitals stay live-only (ADR-0002) —
   a `user` sees the same derived RoomState the dashboard shows, over authenticated WSS,
   never persisted, never cloud.

## Consequences

- New attack surface (a LAN-exposed API) — mitigated by: opt-in default-off, mandatory
  per-device tokens, subnet restriction, role scoping, instant revocation, local TLS.
- A browser WebSocket handshake can't carry an `Authorization` header, so the token
  rides a short-lived ticket / subprotocol (see the spec).
- New moving parts: a device/token store (hashed at rest), a pairing flow, a revocation
  UI, local cert generation.
- This is the enabler for the mobile companion and a multi-central topology. It does
  **not** enable cross-internet / cross-site access — federation across different LANs
  (VPN/tunnel, distributed identity) is deliberately out of scope and stays in the
  roadmap's long-horizon tier.
