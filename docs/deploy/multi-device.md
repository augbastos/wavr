# Multi-device (desktop-central + LAN companions)

Turn a Wavr desktop into a **central** that a mobile app or a second PC on the **same
Wi-Fi** can connect to as an authenticated companion. Design: [ADR-0006](../adr/0006-authenticated-lan-access.md).
Implementation: [spec](../superpowers/specs/2026-07-03-multi-device-client-auth-design.md).

**Opt-in and default-OFF.** With `WAVR_MULTIDEVICE` unset, Wavr is strict loopback-only,
exactly as before — this whole surface is inert.

## Enable

```bash
# on the central (the desktop that owns wavr.db):
pip install -e backend[tls]       # cryptography, for the local self-signed cert
export WAVR_MULTIDEVICE=1
export WAVR_BIND=192.168.1.5      # the central's own LAN IP (defines the "same /24")
python -m wavr.serve             # binds HTTPS/WSS on $WAVR_BIND:${WAVR_PORT:-8000}
```

`python -m wavr.serve` is the launcher: with `WAVR_MULTIDEVICE` on it auto-generates
(or reuses) a local self-signed cert under `~/.wavr/` — CN `wavr`, SANs `localhost`,
`127.0.0.1`, and your `WAVR_BIND` IP, ~397-day validity — and serves the API over
**HTTPS/WSS**. Bring your own cert instead by setting `WAVR_TLS_CERT` / `WAVR_TLS_KEY`
(both must exist). Port is `WAVR_PORT` (default 8000).

With `WAVR_MULTIDEVICE` unset, `python -m wavr.serve` serves plain HTTP on
`127.0.0.1` exactly as before — no cert is generated and `cryptography` is never
imported, so the base install needs no extra.

## Pair a companion (local, no cloud)

1. On the central, mint a one-time pairing code (role `user` by default, or `central`).
2. On the companion (same Wi-Fi), `POST /api/pair {code, device_name}` → returns a
   per-device **token once**. Store it; send it as `Authorization: Bearer <token>` on
   API calls.
3. For the live stream: `POST /api/ws-ticket` (with the token) → a short single-use
   ticket → open `/ws/live?ticket=<ticket>`.

**Roles:** `central` = full control (toggle sources/cameras, manage devices); `user` =
read-only (view RoomState + alerts). **Revoke** any device: `DELETE /api/devices/{id}`
(central only) — the token dies on its next request and any open stream is dropped.

## Security limitations (read before enabling)

The default (loopback-only) is unaffected. The **enabled** path is hardened but still
has known limits — safe on a **trusted home LAN**:

- **TLS is on (Phase 2 — closes the plaintext gap).** When you launch with
  `python -m wavr.serve`, tokens, pairing tickets, the `?ticket=` in the WS URL, and the
  RoomState stream all ride **HTTPS/WSS** with the local self-signed cert. A passive
  sniffer on the Wi-Fi can no longer capture and replay a token. **One-time trust
  prompt:** because the cert is self-signed (no public CA — there's zero cloud by
  design), the companion will warn on first connect; accept/trust it once and pin it.
  (If you launch the old way, `python -m uvicorn wavr.app:app --host ...`, there is no
  TLS — use `wavr.serve` so the cert is applied.)
- **Pairing window is an open door.** `/api/pair` is reachable by any in-subnet peer
  (that's how onboarding works). It's protected by an 8-digit one-time code + a
  brute-force rate-limit, but keep pairing windows short and pair when you're watching.
- **IPv4 /24 only.** The "same Wi-Fi" check assumes an IPv4 /24 subnet. Wider subnets or
  IPv6-only LANs will (fail-closed) deny companions.

## What IS protected

- Opt-in default-off → loopback-only unchanged for everyone who doesn't enable it.
- **Local TLS** (via `python -m wavr.serve`): tokens + RoomState stream are no longer
  sniffable/replayable on the LAN — the self-signed cert closes the plaintext gap.
- Tokens are 256-bit, stored **hashed** (a leaked `wavr.db` can't be replayed).
- Auth is by the real socket peer + a per-device token; spoofing a `Host`/`X-Forwarded`
  header does not grant access.
- Device management (list/revoke) requires the `central` role — a `user` cannot touch it.
- Revocation drops the token on next use and closes an open live stream.
- Privacy invariants hold: x/y targets + vitals stay live-only (never persisted, never
  cloud); the device store holds only metadata + hashed tokens.

## Roadmap

- **Phase 2 (done):** self-signed local cert → HTTPS/WSS via `python -m wavr.serve`
  (closes the plaintext gap; needs a one-time trust prompt on the companion).
- **Phase 3:** the mobile companion UI (pairing screen + `user`-role dashboard, PWA).
