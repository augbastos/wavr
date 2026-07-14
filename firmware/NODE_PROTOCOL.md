# Wavr Node Protocol v1 (shared contract)

This file is the **single source of truth** for the wire contract between a Wavr
sensor node (firmware) and a Wavr instance (`backend/wavr/api_nodes.py` +
`backend/wavr/nodes.py`). Firmware and backend MUST stay byte-compatible with the
schemas below. If they diverge, a node silently never appears or its data is
rejected.

A **node** is a headless sensor the operator owns and flashes (first target:
ESP32 + HLK-LD2450). It is NOT a companion phone and NOT a peer Wavr instance. A
node can only ever PUSH readings for the ONE room/modality the operator assigned
it at enrollment.

All endpoints require Wavr to be running with `WAVR_MULTIDEVICE=1` and
`WAVR_NODES_ENABLED=1` (a node is a remote LAN device, so it needs the LAN bind +
local TLS that multidevice provides). All calls are over **HTTPS** to the Wavr
LAN address. TLS trust is **trust-on-first-use (TOFU), enforced entirely by the
firmware, no backend change**: the first enroll call is the only connection made
with no certificate verification at all; the instant it returns a genuine token,
the node pins the SHA-256 fingerprint of the certificate it was just presented
(`firmware/wavr_node/src/tls_pin.{h,cpp}`) and every later call refuses to
complete its TLS handshake — and therefore never sends the bearer token — against
any other certificate. See "TLS trust (TOFU)" below for the full mechanism and
its honestly-disclosed limits.

---

## Enrollment lane (Wavr-native, the DEFAULT)

Physical presence is required to enable a node: a freshly flashed node boots
**unprovisioned** and comes up as a Wi-Fi SoftAP (`wavr-node-XXXX`). The operator
connects to it once and submits, via its captive portal:

- home Wi-Fi SSID + password (stored in NVS, never leaves the node),
- the Wavr LAN base URL (e.g. `https://192.168.1.20:8000`),
- a one-time **enrollment code** the operator copied from Wavr's *Add a node*
  screen (loopback-root; the operator ALSO chose there the node's name, room, and
  sensor type — the node never chooses those).

The node then joins Wi-Fi and calls:

### `POST /api/nodes/enroll`  (unauthenticated, in-subnet, code-bounded)
```json
{ "code": "48210573", "cert_fingerprint": "<node's own pubkey/cert sha256, optional>" }
```
Response (token returned exactly once — persist it in NVS):
```json
{ "node_id": "….", "token": "…." }
```
`403` = invalid/expired code. Codes are one-time, TTL 5 min, per-IP rate-limited.
This request `cert_fingerprint` field is the NODE's own optional cert
fingerprint, recorded by the server but not yet enforced (future mTLS use) —
it is unrelated to the node's TOFU pin of *Wavr's* cert described below; the
reference firmware always sends it empty.

The node stores `token` and presents it as `Authorization: Bearer <token>` on
every call below — but only after independently confirming, over the SAME
connection, that Wavr's TLS certificate matches what it pinned at enroll (see
"TLS trust (TOFU)" below). A cert mismatch means the token is never sent at
all, on that connection or any other, until the node is re-enrolled.

---

## TLS trust (TOFU)

Wavr has no public CA — every instance serves a fresh self-signed certificate
(`backend/wavr/tls.py`). There is nothing for ordinary CA-chain validation to
check against, so the node pins the certificate itself instead, entirely
client-side (no backend change; the server does not know or care that the node
does this):

1. **First enroll only** (`POST /api/nodes/enroll` above): the node connects
   with NO certificate verification at all — it has nothing to pin yet. This is
   the one, unavoidable trust-first-use moment.
2. The instant that call returns a genuine `200` with a parseable `token`, the
   node reads the certificate that connection actually presented, computes its
   SHA-256 fingerprint, and persists both the fingerprint and the certificate
   into NVS (`firmware/wavr_node/src/tls_pin.{h,cpp}`).
3. **Every call after that** (telemetry, heartbeat, reactivate) pins to exactly
   that certificate. A different certificate — a MitM's, or Wavr's own
   legitimately rotated/regenerated cert — makes the TLS handshake itself fail;
   the bearer token, which is only ever written to the wire after a completed
   handshake, is never sent. The firmware treats this failure exactly like any
   other "Wavr unreachable" condition (retry next tick) — a cert mismatch is
   never itself a kill/revoke signal, so it can never brick or factory-reset a
   node on its own.

**Honest residual — the first-use trust window.** TOFU cannot protect the
enrollment call itself: an attacker already on the LAN and on-path *during*
that first, few-second exchange can still intercept the enrollment code and
the first token, the same way any first-contact trust scheme can be beaten by
an attacker who is there from the very first handshake. Nothing closes that
without a secret the node has before it has ever talked to Wavr, which this
product deliberately does not provision (see the Enrollment lane above — the
one-time code IS that secret, and it is only as safe as the LAN it's typed
into). What TOFU DOES close is every connection after that one — the
realistic, common case, since it does not require an attacker to be
positioned during one specific ≤5-minute window.

**If Wavr's certificate ever legitimately changes** (reinstall, manual cert
regeneration, `~/.wavr/cert.pem` deleted), every already-pinned node's
handshakes start failing permanently — there is no automatic re-pin, by
design (an unpinning-on-any-change firmware would defeat the point). Recovery
is the same factory-reset + re-enroll path already used for a revoked node: a
physical ≥3 s hold at the node, or `DELETE /api/nodes/{id}` from Wavr.

## Data plane (node bearer token required)

### `POST /api/nodes/telemetry`
Header: `Authorization: Bearer <token>`
```json
{
  "seq": 1234,                       // REQUIRED int, strictly increasing per node
  "ld2450_frames": ["aaff0300…55cc"] // LD2450/mmWave nodes: raw 30-byte report frames, hex
}
```
For non-LD2450 sensors send decoded fields instead of `ld2450_frames`:
```json
{ "seq": 1234, "presence": true, "motion": 0.4,
  "targets": [ {"id":1, "x":1.0, "y":0.5, "velocity":0.3} ] }
```
Responses: `200 {"accepted":true}` · `400` non-int seq · `409` stale/replayed seq
· `423` node is disabled (stop sending, honor the sleep heartbeat) · `401/403`
missing/invalid token.

Rules the backend enforces (do not rely on self-reported values):
- **room and modality come from the enrolled record**, never from the payload.
- LD2450 raw frames are parsed **server-side** with the already-tested
  `parse_ld2450_frame`, so the firmware forwards frames as-is (no on-device parse
  to drift). Frame = `AA FF 03 00` + three 8-byte target slots + `55 CC` (30 B).
- confidence is clamped to `[0,1]` then to the node's transport cap (native 1.0,
  mqtt 0.7). Only radar-class sensors set a person count.

Recommended cadence: 1 telemetry POST every ~0.5–1 s while sensing.

### `POST /api/nodes/heartbeat`  (every ~30 s while active; a DISABLED node
polls at a slower **low-power** cadence instead — see `WAVR_HEARTBEAT_DISABLED_MS`
in `config.h` — until reactivated or revoked)
Header: `Authorization: Bearer <token>`, empty body. Response:
```json
{ "command": "ok", "state": "active" }
```
`command` is the kill channel to the hardware: `ok` is what the current backend
sends (see `_HEARTBEAT_COMMAND` in `api_nodes.py`); the reference firmware's
parser also still accepts the legacy value `run` as a synonym, purely for
backward compatibility with older Wavr versions — new backend code should never
emit `run`. `ok`/`run` = sense normally; `sleep` = the operator disabled you —
stop the sensor (the driver itself is no longer polled) and go dark, at the
slower heartbeat cadence, until reactivated; `revoked` = your token is dead,
factory-reset and re-enroll.

**In practice a revoked node never sees the `revoked` body.**
`NodeStore.revoke()` clears the token hash server-side (anti-resurrection —
see `wavr/nodes.py`), so a revoked node can no longer authenticate *at all*:
this same call instead comes back a bare `401`/`403` with no parseable
command. The firmware MUST treat that status on this route identically to an
in-body `revoked` (factory-reset) — this is the exact case the reference
firmware previously got wrong (it collapsed every non-200 response, network
failure included, into one generic "unreachable", so a revoked node kept
retrying forever instead of resetting). A connection failure with **no HTTP
response at all** (DNS/TCP/TLS error, timeout, Wavr mid-restart) is the only
condition that should be treated as `unreachable` — keep the current state,
retry next tick, never sleep/reset on a network blip.

### `POST /api/nodes/reactivate`  (the ONLY re-enable, node-initiated)
Header: `Authorization: Bearer <token>`
```json
{ "press_count": 3 }
```
`press_count` is a monotonic counter the firmware bumps **only on a physical
button press**, persisted to NVS on every single press (`main.cpp`'s
`savePressCount()`, called before the network round-trip) — so it survives a
reboot/power-loss and stays above whatever high-water mark the server last
actually accepted, never restarting at 0 while the server remembers a higher
value. Response: `{ "state": "active" }`. This is the sole path from
`disabled` back to `active` — Wavr has **no remote enable**. A replayed or
non-increasing `press_count` does nothing. `429` = rate-limited (see below).

**Residual trust boundary**: the server can verify the caller holds *this
node's own* bearer token and that `press_count` is a fresh high-water mark, but
it cannot verify the increment came from an actual finger on an actual button —
a fully compromised node's firmware could lie about that. The blast radius is
bounded: a lying node can only re-enable *itself*, never another node, never
skip `disabled`, and never resurrect a `revoked` node. The endpoint is also
rate-limited per node (`REACTIVATE_MAX_ATTEMPTS` calls per
`REACTIVATE_WINDOW_SECONDS`, see `wavr/nodes.py`) as an abuse brake against a
compromised/buggy node hammering the store — `429` means slow down, it is not
itself a security control. For an operator who no longer trusts a node's
credential or firmware, `DELETE /api/nodes/{id}` (revoke) is the real,
terminal recourse — no `press_count` can undo it.

---

## Kill-switch state machine (firmware side)

```
  (flash)
     │ unprovisioned → SoftAP → operator submits Wi-Fi + code
     ▼
  ENROLLING ──enroll 200──▶ ACTIVE ──telemetry──▶ (feeds Wavr fusion)
                              │  ▲
                heartbeat     │  │ POST /reactivate {press_count↑}   (physical button)
                "sleep"       ▼  │
                            DISABLED  (sensor off, radio still heartbeats)
                              │
                heartbeat "revoked" / 403
                              ▼
                            REVOKED → factory-reset NVS → re-flash/re-enroll
```

Remote-OFF-never-ON: the operator can push a node to DISABLED from Wavr, but only
a physical press at the node (a strictly higher `press_count`) returns it to
ACTIVE.

---

## Generic template seam (PIR / BLE beacon / environmental)

The reference firmware (`firmware/wavr_node/`, a PlatformIO project) is written so
a new sensor reuses everything (Wi-Fi provisioning, enroll, token storage,
heartbeat, reactivate, kill-switch, OTA, status LED) and only requires a new
`SensorDriver` implementation under `src/sensors/` (see `sensor_driver.h`), which
swaps two things:

1. `sensorTypeHint()` — one of `ld2450`, `mmwave`, `pir`, `ble_beacon`,
   `generic`, `environmental` (must match `SENSOR_MODALITY` in `wavr/nodes.py`).
   This is purely informational on the node side — the operator chooses the real
   `sensor_type` at enroll-code time on Wavr's trusted screen, never the node.
2. `buildTelemetry()` — fill the JSON body: LD2450 nodes push `ld2450_frames`;
   decoded sensors push `presence`/`motion`/`targets`. Everything else is shared.
   `firmware/wavr_node/src/sensors/pir_driver.{h,cpp}` is a second, concrete
   driver proving the seam holds, not just LD2450.

A non-presence sensor (`environmental`) still enrolls and heartbeats, but its
telemetry is accepted and NOT fused (it has no presence modality) — it is there
for future telemetry surfaces, not occupancy.
