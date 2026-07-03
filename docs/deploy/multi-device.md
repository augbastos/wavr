# Multi-device (desktop-central + LAN companions)

Turn a Wavr desktop into a **central** that a mobile app or a second PC on the **same
Wi-Fi** can connect to as an authenticated companion. Design: [ADR-0006](../adr/0006-authenticated-lan-access.md).
Implementation: [spec](../superpowers/specs/2026-07-03-multi-device-client-auth-design.md).

**Opt-in and default-OFF.** With `WAVR_MULTIDEVICE` unset, Wavr is strict loopback-only,
exactly as before — this whole surface is inert.

## Enable

```bash
# on the central (the desktop that owns wavr.db):
export WAVR_MULTIDEVICE=1
export WAVR_BIND=192.168.1.5      # the central's own LAN IP (defines the "same /24")
python -m uvicorn wavr.app:app --host 192.168.1.5 --port 8000
```

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

## ⚠️ Phase-1 security limitations (read before enabling)

This is Phase 1. The default (loopback-only) is unaffected, but the **enabled** path has
known gaps — safe on a **trusted home LAN**, NOT on an untrusted/public Wi-Fi:

- **No TLS yet (the big one).** Tokens, pairing codes, the `?ticket=` in the WS URL, and
  the RoomState stream travel **plaintext** on the LAN. A passive sniffer on the same
  Wi-Fi can capture and replay a token. **Do not enable `WAVR_MULTIDEVICE` on a Wi-Fi you
  don't trust** until Phase 2 (self-signed HTTPS/WSS) lands. On your own home network,
  the risk is a local attacker already on your LAN.
- **Pairing window is an open door.** `/api/pair` is reachable by any in-subnet peer
  (that's how onboarding works). It's protected by an 8-digit one-time code + a
  brute-force rate-limit, but keep pairing windows short and pair when you're watching.
- **IPv4 /24 only.** The "same Wi-Fi" check assumes an IPv4 /24 subnet. Wider subnets or
  IPv6-only LANs will (fail-closed) deny companions.

## What IS protected

- Opt-in default-off → loopback-only unchanged for everyone who doesn't enable it.
- Tokens are 256-bit, stored **hashed** (a leaked `wavr.db` can't be replayed).
- Auth is by the real socket peer + a per-device token; spoofing a `Host`/`X-Forwarded`
  header does not grant access.
- Device management (list/revoke) requires the `central` role — a `user` cannot touch it.
- Revocation drops the token on next use and closes an open live stream.
- Privacy invariants hold: x/y targets + vitals stay live-only (never persisted, never
  cloud); the device store holds only metadata + hashed tokens.

## Roadmap

- **Phase 2:** self-signed local cert → HTTPS/WSS (closes the plaintext gap).
- **Phase 3:** the mobile companion UI (pairing screen + `user`-role dashboard, PWA).
