# Node onboarding — flash a sensor, connect it to Wavr

The turnkey path from "board in a bag" to "room lit up in fusion": flash an
ESP32 + HLK-LD2450 with the reference firmware, provision it over Wi-Fi, and
enroll it into Wavr with a one-time code. No soldering required if you use a
breakout board; ~15–20 minutes end to end including toolchain setup.

## Status — read this first

What's actually built and tested vs. what's still pending, so you don't chase
a feature that isn't wired yet:

| Piece | State |
|---|---|
| Backend (`NodeStore`, `NodeEnroller`, the 3 `/api/nodes/*` routers) | Built, 35 tests green |
| Firmware (`firmware/wavr_node/`, PlatformIO project) | Written, **not compiled** — no ESP toolchain in the dev environment. Run `pio run -e esp32dev` once before your first real flash; see `firmware/README.md`'s *Compile status* section for the two spots most likely to need a first-pass fix. |
| `app.py` mounting the routers + `config.py`'s `WAVR_NODES_ENABLED` flag + `fusion.py`'s `pir`/`node` weights | **Not yet applied.** Specified verbatim in the *WIRING SPEC* of `docs/superpowers/specs/2026-07-11-wavr-sensor-node-onboarding-design.md` for the lead to apply. Until it lands, a running Wavr instance does not expose `/api/nodes/*`. |
| Frontend *Nodes* panel (Add a node / list / Disable / Remove) | **Not yet built** — same wiring spec, §D. |

This guide documents the intended, already-tested flow so it's ready to
follow the moment the wiring above lands and hardware arrives. Where the UI
doesn't exist yet, the equivalent raw API call is given instead.

## 1. Shopping list

- **ESP32 dev board** — any ESP32-WROOM-32 DevKitC-style board with onboard
  USB-serial. ~€5–8.
- **HLK-LD2450** — 24 GHz mmWave position radar module. ~€10–15.
- 4 jumper wires (Dupont F-F works for most breakout headers).
- USB cable matching the board's port, for flashing and power.
- **Check your LD2450 breakout's logic level before wiring it up.** Most
  breakouts run the module at 5V; the ESP32's UART RX pin is **not** 5V
  tolerant. Either get a 3.3V-logic variant or add a logic-level shifter on
  the LD2450→ESP32 RX line. Power the LD2450 itself from 5V regardless.
- *Optional:* an external momentary push-button if you don't want to rely on
  the onboard **BOOT** button as the kill-switch (the onboard button works
  fine as-is — no extra wiring needed for the default setup).

## 2. One-time toolchain setup (PlatformIO)

Install [PlatformIO](https://platformio.org/install) — either the CLI or the
VS Code extension. Nothing else to install manually: `platformio.ini` pulls
the `espressif32` platform and ArduinoJson automatically on first build.

```
git clone <your wavr checkout, or just cd into it>
cd wavr/firmware/wavr_node
```

## 3. Wiring diagram (GPIO pins)

All pins live in `firmware/wavr_node/include/config.h` — change them there,
not in the driver `.cpp` files, if your wiring differs.

**HLK-LD2450 (UART2):**

| LD2450 | ESP32 |
|---|---|
| `TX` | **GPIO16** (RX2) |
| `RX` | **GPIO17** (TX2) — through a level shifter if the module is 5V logic |
| `5V` | 5V |
| `GND` | GND (common with ESP32) |

Baud is fixed in firmware at 256000 — nothing to configure.

**Kill-switch:** defaults to the onboard **BOOT button (GPIO0)** — no wiring
needed. To use an external button or a bare jumper instead: wire it between
any free *non-strapping* GPIO (avoid 0/2/5/12/15, sampled at boot on most
modules) and GND, then update `WAVR_KILL_SWITCH_PIN` in `config.h`. A jumper
briefly bridged to GND reads identically to a button press.

**Status LED:** onboard LED, **GPIO2**, active-high by default. Set
`WAVR_LED_ACTIVE_LOW` in `config.h` if yours is wired active-low.

**PIR (optional second sensor type, `esp32dev-pir` build env):**
HC-SR501 `OUT` → **GPIO27**.

## 4. Flashing steps

```
cd firmware/wavr_node
pio run -e esp32dev -t upload    # first flash, over USB
pio run -e esp32dev -t monitor   # serial logs @ 115200 baud
```

For a PIR node instead of the default LD2450 build:
`pio run -e esp32dev-pir -t upload`.

Watch the serial monitor on first boot — it's the fastest way to see Wi-Fi
join failures or an enrollment rejection instead of guessing from the LED
alone. Since this hasn't been run against real hardware yet (see *Status*
above), treat the first `pio run` as your actual compile check, not a
formality.

## 5. First-boot Wi-Fi provisioning

A freshly flashed board is **unprovisioned**:

1. Power it. It opens an **open** (no password) Wi-Fi access point named
   `wavr-node-XXXX` (`XXXX` = the last two bytes of its MAC). The status LED
   fast-blinks (~150ms) while it's waiting.
2. Connect to that AP from your phone or laptop — most phones auto-prompt
   "Sign in to network" (captive portal).
3. On the portal page, submit:
   - your home Wi-Fi SSID + password (stored in NVS on the node, never sent
     to Wavr),
   - Wavr's LAN URL, e.g. `https://192.168.1.20:8000`,
   - the **enrollment code** from step 6 below (mint it right before this
     step — it expires in 5 minutes).
4. The node joins Wi-Fi, redeems the code (`POST /api/nodes/enroll`), stores
   its bearer token in NVS, and starts reporting. LED goes to a slow blink
   (~500ms, "connecting/enrolling") and then a brief blip every ~2s once
   active.

## 6. How the node appears in Wavr — the consent step

This is the actual pairing ceremony, inverted from Mobile/Peer pairing
because the node is headless: **the operator declares identity, the node
only redeems a code.**

1. In Wavr → *Nodes → Add a node* (once the panel lands — see *Status*):
   pick a name, a **room** (use the same room name/id your floor plan
   editor already has, since this is what fusion attributes the reading
   to), and a sensor type (`ld2450` for this build). The node itself never
   gets to choose these — a compromised node can't relocate itself or
   change what it claims to be.
2. Wavr shows a one-time code (5-minute TTL, per-source-IP rate-limited to
   10 attempts / 60s).
3. Type that code into the node's captive portal (step 5.3 above).

**Before the panel lands**, the equivalent is the admin API directly (run
from the Wavr host itself — loopback + an authenticated root session, the
same one other admin actions like device pairing already require):

```
curl -k -X POST https://127.0.0.1:8000/api/nodes/enroll-code \
  -H "Content-Type: application/json" \
  -d '{"name":"Living room radar","sensor_type":"ld2450","room":"living_room","transport":"native"}'
# -> {"code": "48210573"}
```

Once redeemed, the node shows up in `GET /api/nodes` (and, once wired, the
Nodes list) as `state: "active"`; its room lights up in fusion within
seconds of its first telemetry post.

### TLS trust — client-side TOFU (what the firmware actually does)

The node pins Wavr's TLS certificate **trust-on-first-use**, so a later
on-path attacker can't transparently intercept its bearer token:

- The **first enroll connection is the only one** made with `setInsecure()`.
  On a genuine `200`+token response, the firmware captures the certificate
  that connection presented, SHA-256-fingerprints it, PEM-encodes it, and
  persists it to NVS (`firmware/wavr_node/src/tls_pin.{h,cpp}`).
- **Every later** telemetry/heartbeat/reactivate call uses `setCACert(pinned)`
  — a non-matching live cert (a MitM, *or* a legitimate Wavr cert rotation)
  fails the TLS handshake itself, so the token is never written to the wire.
  `postJson()` additionally hard-refuses any bearer call while no pin exists.

**Residual you must know:** TOFU trusts whatever cert it sees on that *first*
enroll. A MitM present during the very first enroll could pin its own cert.
Close that window out-of-band: the node prints its captured fingerprint on the
Serial console / SoftAP page — compare it against Wavr's own certificate
fingerprint (shown on the hub) before trusting the node. The enroll one-time
code (5-min TTL, per-IP rate-limited, minted only from a trusted loopback
session) and the per-node bearer token remain the enrollment + identity
boundary on every call.

**Cert rotation:** if Wavr's certificate changes, the node fails closed
(shows as unreachable, never bricks, never resets) until you re-enroll it —
hold the kill-switch ≥3s to factory-reset (wipes the pin + creds) and provision
again. See the "TLS trust (TOFU)" sections in `firmware/NODE_PROTOCOL.md` and
`firmware/README.md` for the wire-level detail.

## 7. Kill-switch (physical + remote-OFF-never-ON)

This is an invariant, not a convenience: **Wavr can turn a node off remotely,
never on.** Turning it back on always requires someone physically at the
device.

- **Disable** (`POST /api/nodes/{id}/disable`, admin/loopback): the node's
  telemetry is rejected immediately at ingest (`423`) — it never reaches
  fusion — and the node is told to `sleep` on its next heartbeat poll
  (≤30s later). The board stays on Wi-Fi and keeps heartbeating; it just
  stops sensing.
- **Re-enable — physical only.** A short press on the kill-switch input
  (onboard BOOT button, or your wired button/jumper) bumps a monotonic
  `press_count` in NVS and calls `POST /api/nodes/reactivate`. This is the
  **only** disabled→active edge that exists — there is no admin or remote
  enable route anywhere in `wavr/api_nodes.py`; the invariant is enforced by
  that route simply not existing, not by a permission check that could be
  bypassed.
- **Long hold (≥3s)** on the same input: factory-reset. Wipes Wi-Fi creds,
  Wavr URL, and the bearer token from NVS, and reboots the node back into
  SoftAP setup (§5). Use this to decommission, re-home to a different room,
  or recover a node.
- **Remove** (`DELETE /api/nodes/{id}`, admin): terminal revoke. The token is
  killed server-side immediately and no `press_count` can undo it.

  One honest gap to know about: revoking does **not** currently make the
  physical node auto-factory-reset itself. `NODE_PROTOCOL.md` describes a
  `revoked (or 403)` heartbeat outcome that should trigger a self-reset, but
  the shipped firmware's heartbeat handler only reacts to an explicit
  `{"command":"revoked"}` body — a `403` (which is what a revoked node
  actually gets, since its token no longer resolves at all) is instead
  treated as "Wavr unreachable, retry later" and does nothing. In practice:
  after you revoke a node, its telemetry/heartbeats are already rejected
  server-side (so it can't feed fusion), but the board itself will keep
  quietly retrying with a dead token until you physically factory-reset it
  (long hold) or re-flash it. Don't expect it to reset itself.

## 8. Troubleshooting

**Node not appearing in Wavr**
- Confirm `WAVR_MULTIDEVICE=1` and `WAVR_NODES_ENABLED=1` are set and Wavr
  was restarted (nodes need the LAN bind + local TLS multidevice already
  provides; the app is meant to fail fast at startup otherwise). If
  `WAVR_NODES_ENABLED` doesn't do anything yet, the wiring spec hasn't
  landed — check with whoever's applying it (see *Status*).
- Enrollment code expired (5-min TTL). Mint a fresh one and re-submit via
  the node's portal. If the node already failed and dropped back to SoftAP,
  just reconnect to `wavr-node-XXXX` and try again with the new code.
- Wrong Wavr URL in the portal — must include `https://` and the port
  (e.g. `https://192.168.1.20:8000`), and the node needs to actually reach
  that host (check router AP-isolation / firewall rules between the node
  and the Wavr machine).
- Node stuck on SoftAP (fast-blink LED, ~150ms) — Wi-Fi credentials were
  wrong, or the 20s join timeout was hit. Check
  `pio run -e esp32dev -t monitor` for the actual failure.
- More than 10 failed enrollment attempts from the same source IP within
  60s locks that IP out temporarily — wait a minute if you mistyped the
  code repeatedly.

**Sensor noisy / false positives**
- LD2450 frames are parsed server-side with the same tested parser the
  wired serial source uses — noise near furniture/walls is inherent to
  mmWave, not something to tune on the node.
- Double-check wiring isn't swapped: LD2450 `TX`→ESP32 **GPIO16**, LD2450
  `RX`→ESP32 **GPIO17**, common GND.
- **Two LD2450 nodes in the same room:** fusion holds one slot per
  `(room, modality)` — a second `mmwave` node in the same room overwrites
  the first (last-writer-wins), it doesn't average with it. Give each radar
  its own room.
- If you're onboarding a sensor you don't control the firmware of (MQTT
  interop lane instead of a flashed node), its confidence is hard-capped at
  0.7 regardless of what it reports — that's intentional, not a bug to
  chase.

**Re-pairing / recovery**
- **Node lost or misbehaving:** *Remove* it in Wavr (`DELETE
  /api/nodes/{id}`) so it stops being trusted server-side, then physically
  factory-reset it (hold the kill-switch ≥3s) or re-flash it, and mint a
  fresh code to re-enroll. Remember revoke alone won't make the board reset
  itself (§7).
- **Moving a node to a different room:** there's no "edit room" call — room
  is fixed at enrollment to keep the anti-spoof guarantee that a node can't
  self-relocate. Revoke, factory-reset the physical node, and re-enroll with
  a fresh code declaring the new room.
- **Suspected compromised/cloned token:** revoke immediately (terminal),
  factory-reset the physical device it belonged to, re-enroll fresh.

## Reference

- Wire contract (source of truth for firmware ↔ backend): `firmware/NODE_PROTOCOL.md`
- Firmware layout, build envs, OTA, LED legend: `firmware/README.md`
- Design record, fusion weights, anti-spoof rationale, and the WIRING SPEC
  for `app.py`/`fusion.py`/frontend:
  `docs/superpowers/specs/2026-07-11-wavr-sensor-node-onboarding-design.md`
