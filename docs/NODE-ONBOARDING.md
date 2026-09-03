# Node onboarding — flash a sensor, connect it to Wavr

The turnkey path from "board in a bag" to "room lit up in fusion": flash an
ESP32 + HLK-LD2450 with the reference firmware, provision it over Wi-Fi, and
enroll it into Wavr with a one-time code. No soldering required if you use a
breakout board; ~15–20 minutes end to end including toolchain setup.

## Status — read this first

What's actually built and tested vs. what still needs real hardware, so you
don't chase a feature that isn't there:

| Piece | State |
|---|---|
| Backend (`NodeStore`, `NodeEnroller`, the 3 `/api/nodes/*` routers) | Built and covered by the suite |
| Firmware (`firmware/wavr_node/`, PlatformIO project) | Written, **not compiled** — no ESP toolchain in the dev environment. Run `pio run -e esp32dev` once before your first real flash; see `firmware/README.md`'s *Compile status* section for the two spots most likely to need a first-pass fix. |
| `app.py` mounting the routers + `config.py`'s `WAVR_NODES_ENABLED` flag + `fusion.py`'s `pir`/`node` weights | **Shipped.** `app.py` mounts the node routers behind `cfg.nodes_enabled`; `config.py` reads `WAVR_NODES_ENABLED` (requires `WAVR_MULTIDEVICE=1` too, same rationale as peer pairing — a node is a LAN device); `fusion.py` carries `pir: 0.6` and `node: 0.5` weights, both attributed at room level. A running Wavr instance with both flags set exposes `/api/nodes/*` today. |
| Frontend *Nodes* panel (Add a node / list / Disable / Remove) | **Shipped** — the Nodes tile in `frontend/index.html` (Add a node, live list, Disable, Remove) is live/central-only, and hides itself when `GET /api/nodes` isn't reachable (feature off, or not authorized). |
| Node-initiated join (`POST /api/nodes/request`/`/approve`/`/claim`, §7) | **Backend built and covered by the suite.** `firmware/wavr_node` does **not** speak it — only the code-redeem flow (§§2–6) is in the reference sketch. The Nodes panel has no pending-request form either: the Discoveries tab notices a request and routes you to Settings → Devices, but approving/denying is an admin-API call today, not a click — see §7. |

This guide documents the actual, already-tested flow. Everything on the
software side is live the moment you set both env flags; the pieces still
pending are hardware-only — you have not yet compiled the firmware, and this
project has not run the enrollment ceremony against a physical ESP32 on this
machine (see the firmware row above and §4/§6 below).

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

1. In Wavr → *Nodes → Add a node*: pick a name, a **room** (use the same room
   name/id your floor plan editor already has, since this is what fusion
   attributes the reading to), and a sensor type (`ld2450` for this build).
   The node itself never gets to choose these — a compromised node can't
   relocate itself or change what it claims to be.
2. Wavr shows a one-time code (5-minute TTL, per-source-IP rate-limited to
   10 attempts / 60s).
3. Type that code into the node's captive portal (step 5.3 above).

Equivalently, the admin API directly (run from the Wavr host itself —
loopback + an authenticated root session, the same one other admin actions
like device pairing already require):

```
curl -k -X POST https://127.0.0.1:8000/api/nodes/enroll-code \
  -H "Content-Type: application/json" \
  -d '{"name":"Living room radar","sensor_type":"ld2450","room":"living_room","transport":"native"}'
# -> {"code": "48210573"}
```

Once redeemed, the node shows up in `GET /api/nodes` (and in the Nodes panel)
as `state: "active"`; its room lights up in fusion within seconds of its
first telemetry post.

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

## 7. Node-initiated enrollment — the other way in (no code)

Everything in §§2–6 above is the **code flow**: the operator declares a
node's identity FIRST, on the trusted loopback screen, and mints a one-time
code the node redeems. There is a second way in, built on the backend today
but not yet reachable from either the reference firmware or the Nodes panel
UI.

### Which one to use

- **Code flow (§§2–6): the strongest anti-spoof, and the only one with a
  finished UI today.** The operator commits to what the node is — name, room,
  sensor type, transport — before any node exists to redeem it. This is what
  `firmware/wavr_node`'s reference sketch speaks, and what the Nodes panel's
  "Add a node" form drives end to end. Use this if you're flashing the ESP32
  build in this repo, or any time you're already at the Core anyway.
- **Request flow (below): convenience, backend-only today, weaker anti-spoof
  by design.** A board that already has network reach — a custom node you're
  building yourself, an MQTT-interop sensor, or just testing with `curl` —
  asks to join first; a human approves it afterward from whatever it has (an
  IP, a cert fingerprint, and whatever it claims about itself). This
  deliberately gives up the code flow's strongest guarantee — the operator
  commits to an identity *before* the device exists at all — for the
  convenience of reviewing a board that's already plugged in, from an inbox,
  instead of round-tripping a code through it first.

### How it works today (admin API — no UI yet)

**Honest gap, stated up front:** the Discoveries tab *notices* a pending
request and offers an Approve/Deny card, but clicking either one today only
marks that discovery decided and (on Approve) routes you to Settings →
Devices — it does **not** call the node API itself. The Nodes panel there has
"Add a node" (the code flow) and the enrolled-nodes list, but no
pending-request form yet. Until that ships, approving or denying a request
means calling the admin API directly — the same loopback + authenticated-root
session every other node admin action already requires.

1. **The node asks.** `POST /api/nodes/request` — unauthenticated, in-subnet,
   rate-limited to 5 requests per source IP per 5 minutes:
   ```
   curl -k -X POST https://127.0.0.1:8000/api/nodes/request \
     -H "Content-Type: application/json" \
     -d '{"name_hint":"hallway thing","sensor_hint":"ld2450"}'
   # -> {"node_id": "...", "request_id": "...", "status": "pending", "poll_after_ms": 5000}
   ```
   `name_hint`/`sensor_hint` are CLAIMS the node makes about itself — Wavr
   stores them apart from the fields fusion trusts and shows them to the
   operator only as a suggestion, never as fact. **Save `request_id`** — it
   is returned exactly once and is the node's only way to collect its token
   later.
2. **The operator approves or denies**, supplying the real, load-bearing
   fields — the node's own hints never populate these on their own:
   ```
   curl -k -X POST https://127.0.0.1:8000/api/nodes/<node_id>/approve \
     -H "Content-Type: application/json" \
     -d '{"name":"Hallway radar","sensor_type":"ld2450","room":"hallway","transport":"native"}'
   # or, to refuse instead:
   curl -k -X POST https://127.0.0.1:8000/api/nodes/<node_id>/deny
   ```
3. **The node collects its token** by polling `POST /api/nodes/claim` with
   the `request_id` it saved in step 1:
   ```
   curl -k -X POST https://127.0.0.1:8000/api/nodes/claim \
     -H "Content-Type: application/json" \
     -d '{"request_id":"<request_id from step 1>"}'
   # pending  -> {"status": "pending", "poll_after_ms": 5000}
   # approved -> {"status": "approved", "node_id": "...", "token": "<returned exactly once>"}
   ```
   The token exists in memory only, for 10 minutes after approval
   (`CLAIM_PICKUP_SECONDS`). If nothing claims it in that window — the Core
   restarted, the node was slow — the node quietly returns to PENDING instead
   of getting stuck; approve it again and the next claim hands out a fresh
   token.

None of this changes the anti-spoof invariant §6 already states: the node's
own `name_hint`/`sensor_hint` are never what lands in the trusted `name`/
`sensor_type`/`room`/`transport` columns — only what the operator types at
step 2 does.

## 8. Kill-switch (physical + remote-OFF-never-ON)

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

## 9. Troubleshooting

**Node not appearing in Wavr**
- Confirm `WAVR_MULTIDEVICE=1` and `WAVR_NODES_ENABLED=1` are set and Wavr
  was restarted (nodes need the LAN bind + local TLS multidevice already
  provides; the app refuses to start if `WAVR_NODES_ENABLED` is on without
  `WAVR_MULTIDEVICE`).
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
  itself (§8).
- **Moving a node to a different room:** there's no "edit room" call — room
  is fixed at enrollment to keep the anti-spoof guarantee that a node can't
  self-relocate. Revoke, factory-reset the physical node, and re-enroll with
  a fresh code declaring the new room.
- **Suspected compromised/cloned token:** revoke immediately (terminal),
  factory-reset the physical device it belonged to, re-enroll fresh.

## Reference

- Wire contract (source of truth for firmware ↔ backend): `firmware/NODE_PROTOCOL.md`
- Firmware layout, build envs, OTA, LED legend: `firmware/README.md`
- Interoperability contract (roles, TOFU pinning, anti-spoof invariants) at
  the protocol level: `docs/WAVR-PROTOCOL.md` §7
- Design rationale (fusion weights, kill-switch invariant, anti-spoof choices):
  the module docstrings in `backend/wavr/nodes.py` and `backend/wavr/api_nodes.py`
