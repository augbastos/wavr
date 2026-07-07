# Wavr — CLAUDE.md

Local, privacy-first, explainable home presence/network dashboard. Fuses network
scan / BLE / camera / mmWave signals into per-room occupancy + confidence. Public
repo, **AGPL-3.0** (github.com/augbastos/wavr). Privacy is the product — the
invariants below are load-bearing, not style.

## Read first
- `PRODUCT.md` — what Wavr is and its design principles
- `docs/ROADMAP.md` — current spec letters (A, B2, F2, F3…)
- `docs/adr/0002-privacy-boundaries-ram-only.md` — THE privacy contract
- `docs/adr/0007-desktop-shell.md` — Tauri desktop architecture

## Verified commands
```powershell
cd backend; pip install -e .[dev]; python -m wavr.serve   # loopback 127.0.0.1:8000
python -m pytest backend/tests -q                          # 1212 tests (all hardware mocked)
cd desktop; npm run dev                                    # Tauri dev (needs Rust MSVC + Node 18+)
powershell scripts/wavr-desktop.ps1                        # zero-Rust launcher (backend + browser)
# frontend/index.html opens directly — no build step; off-localhost it self-switches to simulator
```

## Invariants (never violate)
- API is **loopback-only**: 127.0.0.1 bind + peer check + Host allowlist +
  X-Wavr-Local CSRF header — hard-coded by design (ADR-0002), never made configurable.
- Cameras boot **OFF** every process start; enable is runtime-only, never persisted.
- Camera frames and pose keypoints are **never written to disk** — only derived
  signals (occupancy/confidence/explanation) persist.
- Per-person x/y targets and vitals are live-only over `/ws/live` — never SQLite,
  never MQTT, no movement history on disk.
- Off-localhost frontend = simulator with **zero network requests**. Never wire it
  to a real backend.
- Heavy sensing deps (torch, cv2, pyserial, paho, bleak, genai) stay lazy optional
  extras — the default install must not require them (CI never installs them).
- Never commit: `wavr.db*`, `.env`, `house.json` (real floor plan), `local_token`,
  `docs/competitive-analysis/` (real network PII). All gitignored — don't force.

## Gotchas
- `wavr.db` (~39MB) + `-wal` at repo root grow from a running dev server — never
  "clean up" or commit them.
- Working tree may carry WIP: camera calibration/homography (calib_store.py,
  localize.py + tests) — roadmap work, don't revert or absorb into unrelated commits.
- Desktop shell compiles clean on Windows; macOS/Linux HTTPS cert trust NOT
  implemented — Windows is the only verified target for multidevice mode.
- There is no `mobile/` dir — the Capacitor app is roadmap only (a separate local
  `wavr-mobile` sibling repo exists; don't create mobile/ here casually).
- Specialists exist for this repo (sensor-fusion-architect, computer-vision-engineer,
  spatial-geometry-engineer, wavr-lead, etc. in ~/.claude/agents) — route domain
  work to them.

## State (2026-07-06 — update when it changes)
Recent: consent-first device identity, multidevice Tauri shell (HTTPS + pinned
cert), provider-agnostic narrator (Ollama/OpenAI/Anthropic/Gemini), non-biometric
who-is-home. WIP: camera calibration/localize. No public live demo wired.
