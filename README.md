# Wavr — Fused Home Sensing

Multi-modal home presence system: fuses WiFi CSI, LAN device scan, camera CV (YOLO), and mmWave
radar into one explainable `RoomState` per room — occupancy, confidence, per-modality "why",
per-person position (x/y) and posture on a top-down house radar.

**Public demo (simulated data only):** https://wavr-3ef.pages.dev

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

Tests: `python -m pytest backend/tests -q` (138, all hardware mock-tested).

## Docs

- `PRODUCT.md` — product definition and design principles
- `docs/deploy/bring-up-and-expansion.md` — hardening, Docker, hardware tiers (mmWave LD2450,
  ESP32 CSI, camera pose), laptop → appliance migration
- `docs/superpowers/plans/` — every sub-plan (A fusion → B real sources → C camera CV →
  layers 2-4 → Docker → D position/posture radar), all executed via subagent-driven development
