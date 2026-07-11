# Wavr sensor-node firmware

Turnkey "flash a box → it feeds Wavr" nodes. First target: **ESP32 + HLK-LD2450**
(24 GHz mmWave presence/position radar). The wire contract both this firmware and
the Wavr backend build to is **[NODE_PROTOCOL.md](NODE_PROTOCOL.md)** — read it
first; it is the single source of truth.

## Layout
```
firmware/
  NODE_PROTOCOL.md          the v1 wire + state-machine contract (shared, load-bearing)
  README.md                 this file
  wavr_node/                the PlatformIO project (one canonical implementation)
    platformio.ini          esp32dev (LD2450, default) / esp32dev-pir / esp32dev-ota
    include/config.h         ALL pins + timing constants (edit this, not the .cpp files)
    src/
      main.cpp               setup()/loop() orchestration only
      wifi_provision.{h,cpp}  SoftAP captive portal + NVS creds + reconnect watchdog
      wavr_client.{h,cpp}     HTTPS to Wavr: enroll/telemetry/heartbeat/reactivate
      tls_pin.{h,cpp}         TOFU cert pinning for wavr_client's TLS connections
      kill_switch.{h,cpp}     debounced physical button/jumper read
      status_led.{h,cpp}      non-blocking status blink patterns
      ota_update.{h,cpp}      local-network-only OTA hook (ArduinoOTA/espota)
      sensors/
        sensor_driver.h        the driver interface every sensor implements
        ld2450_driver.{h,cpp}  HLK-LD2450 mmWave radar (default, first target)
        pir_driver.{h,cpp}     HC-SR501-class PIR (concrete 2nd driver, proves the seam)
```
A new sensor type (BLE beacon, environmental, a different radar…) means: write a
`SensorDriver` implementation under `src/sensors/`, add a `WAVR_SENSOR_*` build
flag in `platformio.ini` + `config.h`, and nothing else changes — provisioning,
enrollment, the HTTPS client, the kill-switch, OTA, and the status LED are all
sensor-agnostic.

## Turnkey flow (operator, minutes)
1. In Wavr → *Nodes → Add a node*: pick name + room + sensor type → Wavr shows a
   one-time enrollment code (5-min TTL).
2. Flash `wavr_node/` with PlatformIO (`pio run -e esp32dev -t upload`). Power
   the board.
3. First boot is unprovisioned → it hosts an open `wavr-node-XXXX` Wi-Fi AP with
   a captive portal (phones auto-prompt "Sign in to network"). Join it, submit
   home Wi-Fi + the Wavr URL + the enrollment code.
4. The node joins Wi-Fi, enrolls, and starts reporting. It appears in the Nodes
   panel as **active** within seconds.

## Build / flash
```
cd firmware/wavr_node
pio run -e esp32dev -t upload    # first flash, over USB
pio run -e esp32dev -t monitor   # Serial logs at 115200 baud
```
Requires [PlatformIO](https://platformio.org/) (CLI or the VS Code extension).
`platformio.ini` pulls the `espressif32` platform + ArduinoJson automatically on
first build — no manual library install.

**Compile status: NOT compiled here.** This environment has no ESP32 toolchain
(no `pio`/Arduino-ESP32 core installed), so the modules above were written
against the documented Arduino-ESP32 core APIs (`WiFi`, `WiFiClientSecure`,
`HTTPClient`, `WebServer`, `DNSServer`, `Preferences`, `ArduinoOTA`) and
ArduinoJson 6.x, and reviewed by hand for API/type correctness, but **not
verified with `pio run`**. Before flashing real hardware, run
`pio run -e esp32dev` once to catch anything a compiler would — most likely
candidates for a first-pass fix are `HTTPClient::collectHeaders`/`header()`
argument types and `strptime` availability on your specific ESP-IDF/newlib
version (see the comment above `syncTimeFromHeader` in `wavr_client.cpp`), and
`src/tls_pin.cpp`'s reach into `sslclient_context::ssl_ctx` (via `ssl_client.h`,
an Arduino-ESP32 core internal, not a documented public API — see the module
comment in `src/tls_pin.h`) and `mbedtls_sha256`'s exact signature, which
renamed to/from `mbedtls_sha256_ret` across mbedtls 2.x/3.x. **`tls_pin.cpp`
is the highest-risk file in this firmware to have a compile error in** — check
it first.

## Wiring
- **HLK-LD2450** (UART2): LD2450 `TX` → ESP32 **GPIO16** (RX2), LD2450 `RX` →
  ESP32 **GPIO17** (TX2), 256000 baud. The module is 5V logic on most breakout
  boards — the ESP32's UART pins are **not** 5V tolerant; use a 3.3V variant or
  a level shifter on RX. Power the LD2450 from 5V regardless.
- **Kill-switch**: onboard **BOOT button (GPIO0)** by default — no wiring
  needed. To use an external button or a bare jumper instead, wire it between
  any free non-strapping GPIO and GND and change `WAVR_KILL_SWITCH_PIN` in
  `config.h`; a jumper briefly bridged to GND reads identically to a button
  press.
- **Status LED**: onboard LED, **GPIO2** by default (most ESP32 DevKitC-style
  boards). Set `WAVR_LED_ACTIVE_LOW` in `config.h` if yours is wired active-low.
- **PIR** (optional 2nd driver, `esp32dev-pir` env): HC-SR501 `OUT` → **GPIO27**.

All pins are `#define`s in `wavr_node/include/config.h` — change them there,
not in the driver/module `.cpp` files.

## How a node announces itself
A fresh node is invisible to Wavr until an operator is physically at it: it
boots into an **open** SoftAP (`wavr-node-XXXX`, named from its own MAC) with a
captive-portal page. The AP has no password — the actual secret is the
one-time, 5-minute-TTL enrollment code the operator copies from Wavr's *Add a
node* screen and types into that page, alongside home Wi-Fi credentials and
the Wavr LAN URL. The node then joins Wi-Fi and calls
`POST /api/nodes/enroll` with that code; Wavr returns a per-node bearer token
(returned exactly once) that authenticates every telemetry/heartbeat/reactivate
call after that. Room, sensor type, and trust level are **operator-declared on
Wavr's trusted loopback screen at code-mint time** — the node itself never gets
to claim any of those (see NODE_PROTOCOL.md's anti-spoof rationale).

## TLS trust (TOFU) — protecting the bearer token in transit
Wavr has no public CA (every instance serves a fresh self-signed cert, see
`backend/wavr/tls.py`), so there is nothing for a normal CA-chain check to
validate. This firmware pins instead, entirely client-side, via
`src/tls_pin.{h,cpp}`:
- The **enroll call above is the only connection ever made with no TLS
  verification at all** — the node has nothing to pin yet.
- The instant that call returns a genuine token, the node captures the
  certificate it was just presented, stores its SHA-256 fingerprint + the
  certificate itself in NVS, and Serial-prints the fingerprint
  (`pio run -e esp32dev -t monitor` during first boot shows it) — compare it,
  out of band, against Wavr's own serving-cert fingerprint before trusting the
  node's data.
- **Every call after that** (telemetry/heartbeat/reactivate) pins to exactly
  that certificate: a different one — a MitM's, or a legitimately
  rotated/regenerated Wavr cert — makes the TLS handshake itself fail, so the
  bearer token is never sent on that connection or any other. This surfaces to
  the firmware as an ordinary "Wavr unreachable" (retry, never a kill signal),
  never a factory-reset.
- **Honest limit — first-use trust window**: an attacker already on-path
  *during* that one first enroll call can still intercept the enrollment code
  and the first token; nothing short of a pre-shared secret (which this
  product deliberately does not provision) closes that. What this DOES close
  is every connection after it, which is the realistic, common case. If
  Wavr's cert ever legitimately changes, every already-pinned node needs a
  factory-reset + re-enroll (physical ≥3s hold, or an admin `DELETE
  /api/nodes/{id}`) — there is no automatic re-pin, by design.

See `firmware/NODE_PROTOCOL.md`'s "TLS trust (TOFU)" section for the full
mechanism.

## Kill-switch (remote-OFF-never-ON)
- *Disable* in Wavr → the node's telemetry is dropped immediately at ingest
  and it is told to `sleep` on its next `/api/nodes/heartbeat` poll (≤30s
  later). On `sleep` the node stops sensing (the driver is no longer polled,
  not just "stop sending") and drops to a slower, **low-power** heartbeat
  cadence (`WAVR_HEARTBEAT_DISABLED_MS` in `config.h`, default 2 min instead
  of 30s) — less radio/HTTPS activity while it waits. The radio stays up so
  it can still be reactivated.
- Re-enable requires a **physical short press** at the node (onboard BOOT
  button or your wired jumper/button) — this bumps a monotonic `press_count`,
  persisted to NVS on every press (before the network call, so it survives a
  reboot without needing extra presses to catch back up to what the server
  remembers), and calls `POST /api/nodes/reactivate`. This is the **only**
  disabled→active edge in the system; Wavr has no remote enable anywhere in
  the API (see `wavr/api_nodes.py` — there is deliberately no such route).
- *Revoke* is terminal: the node's token is killed server-side (its hash is
  cleared, so the node can never authenticate again) and no `press_count` can
  undo it. **The node factory-resets itself automatically**, no physical
  action needed: its very next heartbeat gets rejected with `401`/`403`
  (a revoked token can't get a friendlier response), which the firmware
  treats identically to an in-body `revoked` command — it wipes Wi-Fi creds,
  the Wavr URL, and the token from NVS and reboots back into SoftAP setup for
  re-flash/re-enrollment. Separately, holding the kill-switch input **≥3s**
  at any time triggers the same factory reset manually/locally.

## OTA updates
`ota_update.{h,cpp}` wires up `ArduinoOTA` — **local network only**, no
internet/cloud update server. Once a node has an initial USB-flashed build and
is on Wi-Fi:
```
cd firmware/wavr_node
pio run -e esp32dev-ota -t upload   # set upload_port to the node's LAN IP first
```
gated by the password in `config.h` (`WAVR_OTA_PASSWORD`, kept in sync with
`platformio.ini`'s `--auth`). There is no over-the-air path that doesn't
originate from someone already on the LAN — nothing here phones home.

## Status LED legend
| State | Pattern |
|---|---|
| Provisioning (SoftAP open) | fast blink (~150ms) |
| Connecting / enrolling | slow blink (~500ms) |
| Active (sensing) | brief blip every ~2s, mostly off |
| Disabled (remote-OFF) | slow pulse (~1.5s) |
| Error (unreachable / enroll failed) | rapid blink (~100ms) |
