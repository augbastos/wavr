# Wavr sensor-node onboarding + fusion design (2026-07-11)

Design for the fastest turnkey "node arrives → connected to Wavr" flow, so an
ESP32 + HLK-LD2450 (and later PIR / BLE / environmental nodes) can be flashed and
feeding fusion in minutes. Backend modules and tests are already built and green;
this doc is the decision record + operator runbook + the **wiring spec** the lead
applies sequentially to `app.py`, `fusion.py`, and the frontend (those files are
intentionally NOT touched by this change).

Built here (new files only):
- `backend/wavr/nodes.py` — `NodeStore`, `NodeEnroller`, `node_event()`.
- `backend/wavr/api_nodes.py` — 3 router factories (public / node-authed / admin).
- `backend/tests/test_nodes.py`, `backend/tests/test_api_nodes.py` — 34 tests, green.
- `firmware/NODE_PROTOCOL.md` — shared wire contract; `firmware/wavr_node/` —
  PlatformIO reference firmware (modular: Wi-Fi/captive-portal, Wavr HTTPS
  client, kill-switch, status LED, local-network OTA, LD2450 + PIR sensor
  drivers) + generic driver template; `firmware/README.md`.

---

## 1. Onboarding / pairing path

Two lanes; **Wavr-native enrollment is the default**, MQTT is interop-only.

**Native (default, for anything we flash — the ESP32+LD2450 target).** A node is
onboarded with a one-time enrollment **code** (mint on trusted loopback → node
redeems once over the LAN), a node-side pinned Wavr cert **fingerprint** (TOFU —
captured and enforced entirely in firmware, see §4 item 4; no backend change),
and a **per-node bearer token** (256-bit, stored hashed). The code/token half of
this reuses the exact server-side primitives already proven in the codebase
(`pairing.PairingManager`'s one-time / TTL / per-IP-rate-limited code;
`DeviceStore`'s hashed token pattern); the TOFU half is new firmware code that
follows the SAME fingerprint format `tls.format_fingerprint` uses (SHA-256,
uppercase colon-hex) purely so a human can eyeball-compare the two, not shared
code (firmware can't import the Python module). It inverts the ceremony because a headless ESP32 can't read a code off a
screen: the operator declares name/room/sensor-type on Wavr's loopback *Add a
node* screen and copies the code into the node's SoftAP captive portal. The node
POSTs `/api/nodes/enroll {code}` (unauth, in-subnet, code-bounded — same posture
as `/api/pair`) and receives its token.

*Why not MQTT/HA-discovery as the default?* Wavr's existing MQTT/HA surface
(`mqtt_publisher.py`/`ha_discovery.py`) is **outbound** — Wavr publishes its state
as HA entities. Ingesting sensor data inbound over MQTT means any broker-authorized
client can publish to a node's topic: there is no per-device secret, so the
anti-spoof story (the whole reason the kill-switch exists) is weak. So MQTT is the
**interop lane** for sensors we don't control the firmware of, is **off by
default**, and every MQTT node is created with `transport="mqtt"` → a hard
confidence **cap of 0.7** and an `mqtt` badge in the UI. The strong lane (token +
pinned TLS) is the default; the weak lane is explicit and capped.

## 2. How a node's signal enters fusion (modality + weight)

A node is a presence **modality**, resolved from its operator-declared
`sensor_type` via `SENSOR_MODALITY` in `nodes.py`:

| sensor_type      | modality | weight | count-capable |
|------------------|----------|--------|---------------|
| `ld2450`,`mmwave`| `mmwave` | **0.9 (existing)** | yes |
| `pir`            | `pir`    | **0.6 (ADD)** | no |
| `ble_beacon`     | `ble`    | 0.7 (existing) | no |
| `generic`        | `node`   | **0.5 (ADD)** | no |
| `environmental`  | `""`     | — (never fused as presence) | no |

The concrete first target (LD2450) therefore fuses as **`mmwave`, weight 0.9** —
sitting exactly where the task asked, identical to the wired serial `MmWaveSource`,
and count-capable (radar resolves discrete targets). It reuses the already-tested
`parse_ld2450_frame` server-side (the node forwards raw frames), so there is one
LD2450 parser, not a firmware copy that can drift.

**Transport trust is handled by a confidence cap in `nodes.py`, not by a lower
weight** — a verified native node's evidence is as trustworthy as the serial
sensor for the presence question, so weights stay clean and the attackable-transport
risk (MQTT) scales the *evidence* (`confidence_cap`) instead. This mirrors the
"fusion stays a pure function over per-source signals; precedence/trust lives in
the source layer" principle.

`node_event()` builds the `SensingEvent` with **room + modality taken from the
trusted record, never the payload** (see §4), confidence clamped to `[0,1]` then to
the cap, and count set only for radar-class sensors.

## 3. Kill-switch state machine (remote-OFF-never-ON)

State lives in `NodeStore.state` (`active`/`disabled`/`revoked`), persisted in the
`nodes` table.

- `active → disabled`: `NodeStore.disable()` via `POST /api/nodes/{id}/disable`
  (loopback-admin). **Remote-OFF is allowed.** Effect is immediate and layered:
  telemetry is rejected at ingest (`423`, never reaches fusion) AND the node is
  told `sleep` on its next heartbeat (so the hardware goes dark too).
- `disabled → active`: **only** `NodeStore.reactivate(node_id, press_count)` via
  `POST /api/nodes/reactivate`, which is **node-initiated** and gated on a strictly
  increasing physical `press_count` (a BOOT-button press). There is deliberately
  **no `enable()` on the store and no admin enable route** — the invariant is
  enforced by absence, and a replayed press_count is inert.
- `→ revoked`: `NodeStore.revoke()` via `DELETE /api/nodes/{id}` (admin). Terminal;
  token hash cleared. No press_count can resurrect it — re-flash + re-enroll.

**Shown in Wavr:** the Nodes panel renders each node's state as a colored chip
(active green / disabled amber / revoked grey), last-seen, room, modality,
transport badge. A disabled node shows **"Press the button on the device to
re-enable"** — never an Enable button (there is no such endpoint to call).

*Honest tradeoff:* a fully-compromised node could lie about physical presses — that
is the firmware trust boundary. The server-enforceable guarantee is that Wavr's own
control plane offers no remote-enable; the operator's recourse against a rogue node
is **revoke** (terminal), which no token can undo.

## 4. Anti-spoof (genuine node vs. rogue injector)

1. **Per-node bearer token** (256-bit, hashed at rest). Every telemetry/heartbeat/
   reactivate call verifies it via `NodeStore.get_by_token`; no token → `401`,
   wrong/revoked → `403`. A rogue can't guess a 256-bit secret.
2. **Server-assigned room + modality + trust.** The frame supplies only raw
   readings; `node_event` stamps room/modality/cap from the enrolled record, so a
   compromised node can't relocate itself or masquerade as `camera`-weight. Proven
   by `test_node_event_room_and_modality_from_record_not_payload`.
3. **Monotonic telemetry `seq`** (`NodeStore.record_seq`): a captured-and-replayed
   frame is rejected (`409`).
4. **TOFU cert pinning, node-side, entirely client-local (no backend change).**
   `firmware/wavr_node/src/tls_pin.{h,cpp}`: the node's very first enroll call is
   the ONLY connection made with no TLS verification at all; the instant that
   call returns a genuine 200 + token, the node captures the certificate it was
   just presented, SHA-256-fingerprints it, and pins it (`setCACert()`) into NVS.
   Every later telemetry/heartbeat/reactivate call is built against that pin, so
   the TLS handshake itself fails closed on a different cert (MitM'd, or Wavr's
   cert legitimately rotated) — `postJson()` sees this exactly like "Wavr
   unreachable", never sends the bearer token, and never treats it as a kill/
   revoke signal. **Honest residual: a "first-use trust window"** — an attacker
   already on-path DURING that one first enroll (not after) can still intercept
   the enrollment code/token; nothing short of a pre-shared secret (which this
   product deliberately doesn't have) can close that. This is a *node-side*
   defense; there is no corresponding server-side enforcement change.
5. **Transport cap** (native 1.0 / mqtt 0.7) bounds how much an interop node can
   ever move fusion.

Next hardening (not day-1): per-frame HMAC with an enroll-derived shared secret,
so even a token leak on the wire is caught. Token-over-pinned-TLS matches the
peer/device model and is sufficient for v1.

## 5. First target + generic template

- **ESP32 + HLK-LD2450**: firmware reads LD2450 frames (Serial2 @ 256000), forwards
  raw frames, respects the sleep/revoked heartbeat, reactivates on button press,
  re-provisions on a 3-s hold. Reference in `firmware/wavr_node/` (PlatformIO
  project; the LD2450 driver lives in `src/sensors/ld2450_driver.cpp`).
- **Generic template**: a new sensor implements the `SensorDriver` interface
  (`src/sensors/sensor_driver.h`) and swaps `sensorTypeHint()` +
  `buildTelemetry()` only — `pir_driver.{h,cpp}` is a second, working driver
  proving the seam. PIR/BLE/environmental reuse all of
  provisioning/enroll/heartbeat/kill-switch. Modality/weight/count-capability follow
  from the operator-chosen `sensor_type` (§2) with zero firmware trust.

---

## WIRING SPEC — for the lead to apply sequentially (do NOT let this doc edit these)

### A. `backend/wavr/config.py`
Add a `nodes_enabled` flag mirroring `peers_enabled`:
- dataclass field `nodes_enabled: bool` and, in the loader,
  `nodes_enabled=os.getenv("WAVR_NODES_ENABLED", "").lower() in ("1","true","yes")`.

### B. `backend/wavr/fusion.py` — `DEFAULT_WEIGHTS`
Add two entries (existing `mmwave`/`ble` unchanged):
```python
DEFAULT_WEIGHTS = {"camera": 1.0, "mmwave": 0.9, "wifi_csi": 0.85, "ble": 0.7,
                   "network": 0.5, "sim": 0.6, "pir": 0.6, "node": 0.5}
```
`pir` is presence-only (not in `COUNTING_MODALITIES` — leave that set as-is; `mmwave`
already counts, and a node's `pir`/`node`/`ble` events never set `count`).

### C. `backend/wavr/app.py`
1. Imports:
   ```python
   from wavr.nodes import NodeStore, NodeEnroller
   from wavr.api_nodes import (build_nodes_public_router,
       build_nodes_ingest_router, build_nodes_admin_router)
   ```
2. Fail-fast (same rule as peers — a node is a LAN device needing multidevice):
   after the existing `peers_enabled and not multidevice` check, add
   `if cfg.nodes_enabled and not cfg.multidevice: raise RuntimeError(...)`.
3. Construct near `_peer_store`:
   ```python
   _node_store = NodeStore(cfg.db_path) if cfg.nodes_enabled else None
   _node_enroller = NodeEnroller(_node_store) if cfg.nodes_enabled else None
   ```
4. Middleware exemption — the node data-plane paths carry NODE tokens, not Device
   tokens, so they must bypass the device-token gate (they self-verify in-handler).
   Extend the in-subnet-exempt tuple at the `/api/pair`,`/api/peers/redeem` check
   (currently app.py ~line 1149) to also include the node routes:
   ```python
   if request.url.path in ("/api/pair", "/api/peers/redeem",
           "/api/nodes/enroll", "/api/nodes/telemetry",
           "/api/nodes/heartbeat", "/api/nodes/reactivate"):
   ```
   (Enroll is code-bounded like `/api/pair`; telemetry/heartbeat/reactivate 401 in
   the handler without a valid node token. The admin routes are NOT listed here —
   they get the loopback-root gate below.)
5. Mount routers where peers are mounted (after `require_local`/`require_root` exist):
   ```python
   if cfg.nodes_enabled:
       app.include_router(build_nodes_public_router(_node_store, _node_enroller))
       app.include_router(build_nodes_ingest_router(_node_store, _ingest))
       app.include_router(build_nodes_admin_router(
           _node_store, _node_enroller,
           admin_deps=[Depends(require_local), Depends(require_root)]))
   ```
   `_ingest` is the app's existing `async def _ingest(event)` — the same fusion seam
   `SourceManager` feeds, so a node frame enters fusion by the identical path a
   local source does. (Next evolution: model each active node as a
   `SourceManager` queue-source so `set_enabled` also gates it; the direct
   `_ingest` call + ingest-time state check is the simpler, provable v1.)

### D. Frontend `frontend/index.html` — Nodes panel (single-file, no build step)
Under the existing device/peer admin area, add a **Nodes** section (loopback-root):
- *Add a node* form → `POST /api/nodes/enroll-code {name, sensor_type, room,
  transport}` → show the returned `code` + a short "enter this in the node's setup
  page (5 min)".
- `GET /api/nodes` list → per node: name, room, modality, transport badge,
  `state` chip, last-seen; a **Disable** button (`POST /api/nodes/{id}/disable`) and
  a **Remove** button (`DELETE /api/nodes/{id}`). For `state==disabled`, render the
  text *"Press the button on the device to re-enable"* — never an Enable control.

### E. Docs
Add `WAVR_NODES_ENABLED` (requires `WAVR_MULTIDEVICE=1`) to the env/config docs
alongside `WAVR_PEERS_ENABLED`.

---

## Operator runbook (turnkey)
1. Set `WAVR_MULTIDEVICE=1` and `WAVR_NODES_ENABLED=1`; restart Wavr (LAN bind +
   local TLS come from multidevice, already in place).
2. Nodes → *Add a node* → name/room/sensor type → copy the code.
3. Flash `firmware/wavr_node/` with PlatformIO (`pio run -e esp32dev -t upload`);
   power the board.
4. Join its `wavr-node-XXXX` Wi-Fi; enter home Wi-Fi + Wavr URL + code.
5. Node appears **active** and its room lights up in fusion within seconds.
6. To pause it: *Disable* (data drops immediately). To resume: press the board's
   BOOT button. To retire it: *Remove* (revoke) and re-flash to reuse.
