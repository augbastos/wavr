# Wavr — Fused Home Sensing

[![tests](https://github.com/augbastos/wavr/actions/workflows/tests.yml/badge.svg)](https://github.com/augbastos/wavr/actions/workflows/tests.yml)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11-blue.svg)](backend/pyproject.toml)
[![privacy](https://img.shields.io/badge/privacy-loopback--only-black.svg)](#privacy-posture)

**Your home, understood — without giving it away.**

Wavr fuses four independent sensing modalities — network scan, WiFi CSI, camera pose, and mmWave
radar — into one **explainable** `RoomState` per room: occupied or not, a confidence score, and the
per-modality *why* behind it. Add position (x/y) and posture on a top-down radar, over a floor plan
you draw yourself. It runs on your own hardware, sends nothing to the cloud, and degrades
gracefully — network scan alone works, and it sharpens as you plug in more sensors.

> **Try it in 30 seconds, no backend and no hardware:** open [`frontend/index.html`](frontend/index.html)
> in a browser. Off-localhost the dashboard self-switches to a built-in simulator — simulated data
> only, zero network requests — so you can judge the UI and the fusion story immediately.

![Wavr dashboard — position radar with posture labels, explainable per-room fusion, and a live timeline](docs/img/demo.png)

---

## Why this repo is worth 60 seconds

A quick map from what's in here to what it demonstrates:

- **Real sensor fusion, not glue** — a transparent `confidence = agreement × strength` model
  (`backend/wavr/fusion.py`) that weighs multiple noisy sources, decays the vote of stale ones, and
  can always explain its output in one sentence.
- **Plugin architecture with a clean seam** — every modality implements one small `SensorSource`
  interface (injectable transports, lazy optional deps). Adding BLE or zigbee is a new file, not a
  rewrite.
- **Async Python at the edges** — FastAPI + WebSockets streaming live `RoomState`, SQLite kept off
  the event loop, bounded queues, capped scan sweeps.
- **Security taken seriously** — loopback-by-default, peer + Host allowlist + CSRF checks, local TLS
  with auto self-signed certs, hashed device tokens, single-use WS tickets, and an audited,
  consent-gated control boundary. Multiple security-audit passes are recorded in the ADRs.
- **Testable without a lab** — **500 tests, green in CI**, with every hardware modality
  (camera, mmWave, BLE, MQTT) fully mock-tested. `pip install`, `pytest`, done — no device required.
- **Documented decisions** — seven ADRs record the trade-offs (why audit mmWave instead of forking,
  why RAM-only frames, why this is explicitly *not* a medical device, why control is refusal-gated).

---

## Architecture

```
sources (network / ruview CSI / camera / mmwave / BLE / sim)
   └─> SensingEvent (+ Target: x/y, posture)
        └─> FusionEngine  (confidence = agreement × strength, explainable weights)
             └─> RoomState ──> WS /ws/live + REST ──> dashboard (cards + radar)
                  ├─> SQLite       (derived state only — never frames, never targets)
                  ├─> RulesEngine / AwayMonitor ──> MQTT   (opt-in; occupied/confidence/ts only)
                  └─> Narrator     ──> Gemini  (double opt-in; the ONLY path off the LAN)
```

- **Backend** — Python 3.11 + FastAPI. Zero mandatory heavy deps: `torch`/`cv2`, `pyserial`, `paho`,
  `bleak` and `google-generativeai` are lazy optional extras (`[camera]`, `[mmwave]`, `[mqtt]`,
  `[ble]`, `[genai]`), so a base install stays tiny and boots on a Raspberry Pi.
- **Frontend** — a single static HTML file, no build step, installable as a PWA (offline shell).
  Off-localhost it self-switches to a simulator and makes zero requests to the backend.
- <a id="privacy-posture"></a>**Privacy posture** — loopback-only by default (peer check + Host
  allowlist + CSRF header). Cameras boot **OFF**; frames are never persisted; position targets are
  live-only and never touch SQLite or MQTT. The opt-in Gemini narrator is the single, double-gated
  path off the LAN.
- **Multi-device** — the desktop is the central; a phone or second PC on the same Wi-Fi pairs as an
  authenticated, revocable, **read-only** companion over local TLS — still zero cloud.
- **Control (opt-in, default-OFF)** — an MCP "brain on Home Assistant" reads HA entities and can ask
  HA to run a service through an allowlist + consent gate. Camera/lock/scene are refused even if
  allowlisted, mass actuation is blocked, every call is audit-logged. Wavr never becomes a device driver.

## The fusion, in one paragraph

Each source emits a `SensingEvent` with its own confidence. The engine computes, per room,
`agreement` (the fraction of trusted mass that says "present") times `strength` (the single best
present piece of evidence, `weight × the source's own confidence`). A source that goes stale or dies
automatically loses its vote, so the fused number drops honestly instead of trusting a dead reading.
The result is a bounded `0..1` confidence plus a human-readable explanation string —
`"camera 0.82 · network 0.60 → 74% occupied"` — surfaced directly in the dashboard. **The fusion is
never a black box: the dashboard always shows why a room reads occupied.**

## Sensing modalities — honest status

| Modality | What it gives you | Status today |
|---|---|---|
| **Network scan** | Presence from known device MACs on the LAN | **Runs now**, zero hardware |
| **Simulator** | Scripted scenarios (wifi-drop, fall, multi-target) | **Runs now**, drives the local demo |
| **BLE presence** | Host Bluetooth adapter as a modality | **Runs now** (`[ble]` extra) |
| **WiFi CSI (ruview)** | Motion/occupancy from channel-state info | Source written + mock-tested; needs a CSI-capable NIC/ESP32 |
| **mmWave radar** | Real per-person x/y tracking (HLK-LD2450, ~€15 USB) | Parser + `SensorSource` **done and tested**; needs the physical device |
| **Camera pose** | Standing / sitting / lying via YOLO-pose over RTSP | Source + safety gates written; live pose needs a GPU (`[camera]` extra) |

Everything in the "needs hardware" rows is fully **mock-tested** — the code path is exercised in CI
against fake transports, so bring-up is "plug in the device", not "write the integration".

## Stack

`Python 3.11` · `FastAPI` · `Uvicorn` · `WebSockets` · `SQLite` · `pytest` / `pytest-asyncio` ·
`GitHub Actions` (CI) · lazy extras: `OpenCV` + `Ultralytics YOLO` (camera), `pyserial` (mmWave),
`paho-mqtt` + Home Assistant auto-discovery, `bleak` (BLE), `google-generativeai` (narrator),
`MCP` (agent control), `cryptography` (local TLS) · **Frontend:** vanilla HTML/JS PWA (no framework,
no build) · **Desktop:** Tauri shell (tray + auto-start) around the same backend + dashboard.

## Run it

**Network presence (zero hardware):**

```powershell
cd backend; pip install -e .[dev]; cd ..
# .env at repo root:
#   WAVR_NET_MACS=<your phone's wifi MAC>
#   WAVR_FUSION_THRESHOLD=0.35   # network-only phase; revert to 0.5 once camera/CSI join
python -m uvicorn wavr.app:app --host 127.0.0.1 --port 8000
# or just double-click scripts/wavr.ps1
```

Open <http://127.0.0.1:8000> for the live dashboard.

**Tests** (all mock-tested, no hardware):

```bash
python -m pytest backend/tests -q      # 500 tests, green in CI
```

**Desktop app + LAN companions:** see [`docs/deploy/multi-device.md`](docs/deploy/multi-device.md)
(`python -m wavr.serve` brings up local TLS + pairing) and the Tauri shell in [`desktop/`](desktop/).

## Design stance: integration over hype

Wavr does not reimplement sensing research — it orchestrates sensing engines as plugins and is honest
about each one's confidence. Every source implements one small `SensorSource` seam (injectable
transports, lazy deps, fully mock-tested), the fusion is transparent math (`agreement × strength`,
per-modality trust weights), and the dashboard always shows *why* a room reads occupied. When an
upstream engine's headline feature turns out weaker than its README (it happens), Wavr consumes what
actually works and the weights tell the truth.

## Roadmap (short version)

**Shipped:** multi-modal fusion, network + BLE + simulator sources, sensor-health trust decay,
multi-device (desktop-central + authenticated LAN companions, local TLS, pairing/revocation), the
installable PWA companion, MQTT Home Assistant auto-discovery, a read-only MCP server plus the gated
MCP "brain on Home Assistant", and the in-app **house editor** (draw multi-floor rooms/walls/stairs,
saved via `PUT /api/house`).

**Next:** camera→position homography (place people on your drawn map), walls in the fusion
(occlusion weighting), mmWave LD2450 bring-up on real hardware, live YOLO-pose posture, cross-source
track association, and fallen-person detection on top of it. Full detail and rejected ideas:
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Docs

- [`PRODUCT.md`](PRODUCT.md) — product definition and design principles
- [`docs/deploy/bring-up-and-expansion.md`](docs/deploy/bring-up-and-expansion.md) — hardening,
  Docker, hardware tiers (mmWave LD2450, ESP32 CSI, camera pose), laptop → appliance migration
- [`docs/adr/`](docs/adr/) — architecture decision records 0001–0007 (RuView audit, privacy
  boundaries, not-a-medical-device, defensive-only, MCP control boundary, authenticated LAN access,
  desktop shell)

## Contributing

Issues and PRs welcome. Ground rules: privacy invariants are non-negotiable (nothing leaves the LAN
except the opt-in narrator; frames are never persisted; new sources must be mock-testable without
hardware), and every PR needs green tests (`pytest backend/tests -q`). Good first contributions:
roadmap items above, or a new `SensorSource` (zigbee occupancy, presence sensor, …).

## License

[AGPL-3.0-or-later](LICENSE) — Wavr is free and open source for personal, self-hosted, and
non-commercial use. If you run a modified version as a network service, the AGPL requires you to
publish your changes. A **commercial / dual license** (to use Wavr without the AGPL's
network-copyleft obligations) is available from the author — open an issue to enquire.
