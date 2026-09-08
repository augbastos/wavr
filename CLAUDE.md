# Wavr — CLAUDE.md

Local, privacy-first, explainable home presence/network dashboard. Fuses network
scan / BLE / camera / mmWave signals into per-room occupancy + confidence. Public
repo, **AGPL-3.0** (github.com/augbastos/wavr). Privacy is the product — the
invariants below are load-bearing, not style.

## Read first
- `PRODUCT.md` — what Wavr is and its design principles
- `docs/WAVR-PROTOCOL.md` — the wire contract (transport, auth, roles, pairing, nodes)
- `docs/FRONTEND-MODULES.md` — where every frontend module lives and why the order is pinned
- `docs/adr/0002-privacy-boundaries-ram-only.md` — THE privacy contract, **with its own
  amendments section**: invariants 1 and 7 were superseded on purpose (see ADR-0006 below)
- `docs/adr/0006-authenticated-lan-access.md` — the opt-in that relaxes loopback-only
- `docs/adr/0007-desktop-shell.md` — Tauri desktop architecture
- `docs/seams.md` — the extension seams (sub-plans B/C/…)

There is **no roadmap document**. `docs/ROADMAP.md` was deleted; several ADRs and the odd
module docstring still cite it and its spec letters (A, B2, F2, F3…). Those references are
dead — don't go looking, and don't recreate the file to satisfy one.

## Verified commands
```powershell
cd backend; pip install -e .[dev]; python -m wavr.serve   # loopback 127.0.0.1:8000
python -m pytest backend/tests -q                          # full suite; all hardware mocked
cd desktop; npm run dev                                    # Tauri dev (needs Rust MSVC + Node 18+)
powershell scripts/wavr-desktop.ps1                        # zero-Rust launcher (backend + browser)
# frontend/index.html opens directly — no build step; off-localhost it self-switches to simulator
```

## Invariants (never violate)
- API is **loopback-only by default**, and LAN access is one explicit opt-in, never a
  silent one. Unset `WAVR_MULTIDEVICE` = the original invariant: 127.0.0.1 bind, peer
  check, Host allowlist, X-Wavr-Local CSRF header, and any non-loopback caller gets 403
  in code — so it holds even under `--host 0.0.0.0`. Set it (ADR-0006, which explicitly
  supersedes ADR-0002 invariant 1) and `serve.py` binds `WAVR_BIND` over local-TLS
  HTTPS/WSS and a same-/24 peer presenting a valid per-device token is admitted, capped
  by its person's current role. Loopback is always `root`. Peers and nodes REQUIRE
  multidevice and `app.py` refuses to start otherwise. What must never happen is the
  default drifting, or a second path to the LAN that is not this flag.
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

## State (2026-07-18 — update when it changes)
Recent: consent-first device identity, multidevice Tauri shell (HTTPS + pinned
cert), provider-agnostic narrator (Ollama/OpenAI/Anthropic/Gemini), non-biometric
who-is-home, agentic "when-this→do-that" routines + guest mode + transparency
endpoint, MCP-over-HTTP read transport (ADR-0008), and the **net_doctor** deep
network-discovery diagnosis (`net_doctor.py` + `connectors/diag.py`): honest cause
discrimination (never blames the router without proven host multicast viability),
per-router fix guides, and an **opt-in, default-OFF** MAC-redacted diagnostics-report
send (the `diagnostics` connector — the newest egress surface). WIP: camera
calibration/localize. No hosted online demo (local-only by design).
