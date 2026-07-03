# ADR-0007 — Desktop shell (Tauri): a native window around the loopback central

## Status

Accepted (design) — 2026-07-03. Scaffold on branch `tauri-shell`, not merged; the Rust
must be compiled + run once on a machine with the toolchain before it lands on `master`.

## Context

Wavr's runtime model is a desktop **central** (fusion + real sources + heavy CV) that
lighter companions connect to ([ADR-0006](0006-authenticated-lan-access.md)). The mobile
companion already ships as an installable PWA. The desktop side is still "open a terminal,
run uvicorn, open a browser tab" — fine for a developer, wrong for the product story of
"install Wavr, it lives in your tray, it's the central."

## Decision

Wrap the **existing** backend + dashboard in a **Tauri v2** desktop shell. No rewrite: the
FastAPI backend and `frontend/index.html` are unchanged.

1. **Loopback central, spawn-not-bundle.** The shell spawns `python -m wavr.serve` in its
   default mode (`WAVR_MULTIDEVICE` unset → HTTP on `127.0.0.1:8000`) and points the
   webview at that URL. The webview origin is loopback, so the dashboard's own detection
   resolves to `central` (full controls) with zero code change. LAN exposure stays a
   separate, explicit opt-in — the shell never silently sets `WAVR_MULTIDEVICE`.
2. **No new network surface, no new secrets.** The shell adds no listener and reads no
   credentials; the backend reads its own `.env` as always. Tauri CSP pins the webview to
   `127.0.0.1:8000` (http + ws) and `'self'` only — nothing external, matching the
   dashboard's zero-external-request rule.
3. **Lifecycle = the on/off control plane.** Launch → backend up → sensing on. Closing the
   window hides to tray (the central is meant to stay on). **Quit** kills the backend
   child; the process exiting releases GPU VRAM (the camera source's `release_model()` +
   process-exit path) — no orphaned python, VRAM back for games.
4. **MVP spawns an installed backend; full packaging is later.** Bundling CPython +
   torch/opencv is heavy and platform-specific. The MVP resolves the venv python via
   `WAVR_PYTHON` → dev-relative `.venv` → `PATH`. A one-click self-contained installer
   (PyInstaller one-file sidecar wired through `tauri-plugin-shell`) is a documented
   follow-up, not a blocker.

## Consequences

- A new **build toolchain** (Rust/Cargo + platform build tools; Node for the Tauri CLI) is
  required to build the shell — but only the shell. The Python backend and its tests are
  untouched and need none of it.
- An **interim launcher** (`scripts/wavr-desktop.ps1`) gives the "click-to-open" experience
  today (start backend + open browser) without waiting for the Rust build, so the product
  story is not gated on installing Rust.
- Identity intact: Wavr stays the local, explainable fusion brain; the shell is packaging,
  not a new capability. Reinforces the privacy-first stance — the desktop app is loopback
  by default, same as everything else.
- **Must be verified before merge:** authored without the toolchain, so the scaffold is
  correct-by-convention; a single `npm run tauri dev` + Quit-leaves-no-python check gates
  the merge.
