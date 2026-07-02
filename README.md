# Wavr — Fused Home Sensing

[![tests](https://github.com/augbastos/wavr/actions/workflows/tests.yml/badge.svg)](https://github.com/augbastos/wavr/actions/workflows/tests.yml)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)

Multi-modal home presence system: fuses WiFi CSI, LAN device scan, camera CV (YOLO), and mmWave
radar into one explainable `RoomState` per room — occupancy, confidence, per-modality "why",
per-person position (x/y) and posture on a top-down house radar.

**Try it locally (no backend, no hardware):** open `frontend/index.html` — off-localhost the
dashboard self-switches to a built-in simulator (simulated data only, zero network requests).

![Wavr dashboard — position radar with posture labels, explainable per-room fusion, timeline](docs/img/demo.png)

## Architecture

```
sources (network / ruview CSI / camera / mmwave / sim)
   └─> SensingEvent (+ Target: x/y, posture)
        └─> FusionEngine (agreement × strength, explainable weights)
             └─> RoomState ─> WS /ws/live + REST ─> dashboard (cards + radar)
                  ├─> SQLite (derived state only — never frames, never targets)
                  ├─> RulesEngine / AwayMonitor ─> MQTT (opt-in, occupied/confidence/ts only)
                  └─> Narrator ─> Gemini (double opt-in; the ONLY cloud egress)
```

- **Backend:** Python 3.11, FastAPI, zero mandatory heavy deps — torch/cv2, pyserial, paho and
  genai are lazy optional extras (`[camera]`, `[mmwave]`, `[mqtt]`, `[genai]`).
- **Frontend:** single static HTML file, no build step. Off-localhost it self-switches to a
  simulator and makes zero requests to the backend.
- **Privacy posture:** loopback-only API (peer check + Host allowlist + CSRF header), cameras
  boot OFF, frames never persisted, position targets live-only (never SQLite/MQTT).

## Quickstart (network presence, zero hardware)

```powershell
cd backend; pip install -e .[dev]; cd ..
# .env at repo root:
#   WAVR_NET_MACS=<your phone's wifi MAC>
#   WAVR_FUSION_THRESHOLD=0.35   # network-only phase; revert to 0.5 when camera/CSI join
python -m uvicorn wavr.app:app --host 127.0.0.1 --port 8000
# or double-click scripts/wavr.ps1
```

Tests: `python -m pytest backend/tests -q` (195, all hardware mock-tested).

## Design stance: integration over hype

Wavr does not reimplement sensing research — it orchestrates sensing engines as plugins and is
honest about each one's confidence. Every source implements one small `SensorSource` seam
(injectable transports, lazy deps, fully mock-tested), the fusion is transparent math
(`agreement × strength`, per-modality trust weights), and the dashboard always shows *why* a room
reads occupied. When an upstream engine's headline feature turns out to be weaker than its README
(it happens), Wavr consumes what actually works and the weights tell the truth.

## Roadmap

- **mmWave bring-up** — HLK-LD2450 over USB serial (~€15): real x/y target tracking on the radar.
  Parser + source are done and tested; needs the device.
- **Camera posture live** — YOLO-pose (`[camera]` extra) on RTSP cameras: standing/sitting/lying.
- **3D house view** — extrude the `house.json` floor plan with walls (isometric SVG first) + an
  in-app floor-plan editor.
- **Cross-source track association** — fuse targets from multiple sensors in the same room.
- **Fallen-person detection** — lying + location + duration on top of the above.

## Contributing

Issues and PRs welcome. Ground rules: privacy invariants are non-negotiable (nothing leaves the
LAN except the opt-in narrator; frames are never persisted; new sources must be mock-testable
without hardware), and every PR needs green tests (`pytest backend/tests -q`). Good first
contributions: roadmap items above, or a new `SensorSource` (BLE presence, zigbee occupancy, …).

## Docs

- `PRODUCT.md` — product definition and design principles
- `docs/deploy/bring-up-and-expansion.md` — hardening, Docker, hardware tiers (mmWave LD2450,
  ESP32 CSI, camera pose), laptop → appliance migration
- `docs/superpowers/plans/` — every sub-plan (A fusion → B real sources → C camera CV →
  layers 2-4 → Docker → D position/posture radar), all executed via subagent-driven development
  with per-task adversarial review

## License

[AGPL-3.0-or-later](LICENSE) — Wavr is free and open source for personal, self-hosted, and
non-commercial use. If you run a modified version as a network service, the AGPL requires you to
publish your changes. A **commercial / dual license** (to use Wavr without the AGPL's
network-copyleft obligations) is available from the author — open an issue to enquire.
