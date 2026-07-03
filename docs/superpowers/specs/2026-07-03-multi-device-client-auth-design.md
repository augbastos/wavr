# Design — Multi-device client auth (desktop-central + LAN companions)

Status: design, 2026-07-03. Implements [ADR-0006](../../adr/0006-authenticated-lan-access.md).
Not yet built. Default behaviour (loopback-only) is unchanged until this ships and is
explicitly enabled.

## Goal

Let a mobile app or a second PC on the **same Wi-Fi** connect to a Wavr desktop
"central" as an authenticated companion, with per-device permissions and instant
revocation — while keeping surveillance local and zero-cloud.

## Non-goals (explicitly out of scope)

- Cross-internet / cross-site access, VPN, tunnels, cloud relays.
- Multi-central *federation* (two centrals merging sources without double-counting,
  distributed authority) — long-horizon; this spec only lets a peer PC join as a
  `central`-role client of one root, not a merged brain.
- Any change to the privacy invariants: x/y targets and vitals stay live-only.

## Roles

| Role | Can see | Can do |
|---|---|---|
| **root central** | everything | own the DB; all control; pair + revoke devices |
| **central** (peer) | everything | full control routes; **cannot** revoke the root |
| **user** | RoomState view + alerts | read-only GETs + `/ws/live`; optional light telemetry POST |

The desktop that owns `wavr.db` is the root central (no token needed on loopback).

## Architecture

### Config (opt-in)
- `WAVR_MULTIDEVICE=1` — enables LAN binding + the auth path. Off = loopback-only, as
  today (no behaviour change for existing users).
- `WAVR_BIND=0.0.0.0` (or the LAN IP) — only honoured when multidevice is on.
- `WAVR_TLS_CERT` / `WAVR_TLS_KEY` — paths; auto-generate a self-signed cert on first
  run if absent.

### Device / token store (new `wavr/devices.py` + SQLite table)
`devices(device_id, name, role, token_hash, created_ts, last_seen_ts, revoked INTEGER)`.
Tokens are random 256-bit, returned once at pairing, stored **hashed** (never plaintext).
Only definition/metadata — no RoomState, no targets (consistent with `storage.py`).

### Pairing flow (local, no cloud)
1. On the central, an operator (loopback UI) starts pairing → server mints a short-lived
   (e.g. 2-min) one-time pairing code, shown as text + QR.
2. Companion (same Wi-Fi) `POST /api/pair {code, device_name, requested_role}` over
   HTTPS.
3. Server validates the code, creates a device row with role (`user` by default;
   `central` only if the operator pre-authorised that code as central), returns the
   token once.
4. Companion stores the token + the central's LAN address locally.

### Auth enforcement (replaces the loopback-only middleware)
The current `loopback_only` middleware becomes `loopback_or_authed`:
- Loopback peer → allow (root central), as today.
- Else, only when `WAVR_MULTIDEVICE`: peer must be in the host's local subnet **and**
  send `Authorization: Bearer <token>` matching a non-revoked device; attach the role
  to the request.
- Else → 403.
Per-route role gate: state-changing routes (`require_local` today) require `central`;
`GET` state/house/history + `/ws/live` allow `user`+.

### WebSocket auth
Browsers can't set an `Authorization` header on the WS handshake. So: companion first
`POST /api/ws-ticket` (Bearer token) → gets a short-lived single-use ticket; opens
`/ws/live?ticket=…`. Handler validates the ticket (in addition to the existing loopback
check for the local dashboard, and the Origin check from the security pass).

### Revocation
- `GET /api/devices` (central) — list devices (name, role, last_seen, revoked).
- `DELETE /api/devices/{id}` (root central) — set `revoked=1`; the token fails on its
  next request and any open WS for it is dropped.
- Leaving the Wi-Fi already removes reachability (natural bound).

### Mobile companion (thin client)
The existing single-file dashboard becomes the companion:
- A first-run screen: enter/scan the central's address + pairing code → stores token.
- A `user` build hides `central`-only controls (sources/cameras/config); shows
  RoomState, radar, alerts. Reuses everything already built.
- Optional: the phone posts light telemetry (on-Wi-Fi presence, BLE, coarse
  accelerometer) back as a `user` — feeding the central's fusion as another source.

## Privacy / security notes
- Zero-cloud preserved: pairing, tokens, and all traffic stay on the LAN.
- Targets/vitals still live-only — a `user` receives the same derived RoomState over
  authenticated WSS; nothing new is persisted.
- Tokens hashed at rest; TLS on the LAN so tokens/stream aren't sniffable; subnet check
  + revocation + opt-in-default-off bound the new surface.
- New surface **must** be re-audited (the security pass that produced ADR-era findings)
  before this is enabled by default anywhere.

## Testing (all mock-testable, no real network)
- Pairing: code lifecycle, one-time use, role assignment, token issued once.
- Auth middleware: loopback allowed; valid token in-subnet allowed; wrong/absent/revoked
  token → 403; out-of-subnet peer → 403; role gate on state-changing routes.
- Revocation: revoked token rejected next call; open WS dropped.
- WS ticket: single-use, short-lived, wrong ticket rejected.
- Privacy: a `user` session never receives persisted vitals; no targets in any
  persisted/MQTT path (regression of ADR-0002).

## Phasing
1. **Backend core** — device/token store, pairing, `loopback_or_authed` + role gate,
   revocation API, WS ticket. (Mock-tested; no UI yet.)
2. **Local TLS** — self-signed cert generation + HTTPS/WSS.
3. **Companion UI** — pairing screen + `user`-role dashboard build (PWA).
4. **Peer central** — a second PC pairing as `central`.

## Open questions
- QR encoding for pairing (address + code) — keep it a plain string first.
- Cert trust on mobile (self-signed) — first-run "trust this central" step.
- Do we want a max-devices cap / pairing rate-limit? (Probably yes, small.)
