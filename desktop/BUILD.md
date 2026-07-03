# Building Wavr Desktop (Tauri v2)

A native tray app that wraps the loopback central — see
[ADR-0007](../docs/adr/0007-desktop-shell.md) and the
[design spec](../docs/superpowers/specs/2026-07-03-tauri-desktop-shell-design.md).

> **This scaffold has not been compiled yet.** It was authored without the Rust toolchain,
> so the Rust is correct-by-convention and needs one `npm run dev` + a Quit-leaves-no-python
> check before it's merged to `master`. Until then, use the interim launcher
> (`scripts/wavr-desktop.ps1`) for the click-to-open experience.

## Prerequisites (one time)

1. **Rust** — install via [rustup](https://rustup.rs). On Windows this also needs the
   **Microsoft C++ Build Tools** (MSVC) + the Windows SDK (the rustup installer links them).
2. **Node** ≥ 18 (you have v24) — for the Tauri CLI.
3. **The Wavr backend installed** in a venv, since the MVP spawns it (it does not bundle
   Python yet):
   ```bash
   cd ..                       # repo root
   python -m venv .venv
   .venv/Scripts/pip install -e backend        # + [camera]/[mqtt]/[genai] extras as needed
   ```

## Build & run

```bash
cd desktop
npm install                 # fetches the Tauri CLI
npm run icon                # generates src-tauri/icons/* from ../frontend/icon.svg (once)

# point the shell at the venv python + repo root (dev):
export WAVR_PYTHON="$(pwd)/../.venv/Scripts/python.exe"
export WAVR_BACKEND_DIR="$(pwd)/.."

npm run dev                 # dev build: opens the window, starts the backend, live-reloads
# or
npm run build               # release: produces an installer under src-tauri/target/release/bundle/
```

On Windows PowerShell, set the env vars with `$env:WAVR_PYTHON = "...\.venv\Scripts\python.exe"`
and `$env:WAVR_BACKEND_DIR = "...\wavr"`.

## What to verify on first run (gates the merge)

- The window shows the **live dashboard** on `127.0.0.1:8000` (not the "Starting…"
  placeholder) within a couple of seconds, with full **central** controls.
- **Close the window** → it hides to the tray; `curl http://127.0.0.1:8000/api/state` still
  answers (sensing kept running).
- **Tray → Quit** → the window closes AND no python is left:
  `Get-Process python -ErrorAction SilentlyContinue` returns nothing. (This is what frees
  GPU VRAM.)

## Later (not MVP)

- **Self-contained installer:** bundle the backend as a PyInstaller one-file sidecar wired
  through `tauri-plugin-shell` so end users don't need a Python install.
- **Auto-start on login:** `tauri-plugin-autostart` (kept off by default — a sensing app
  should not silently start at boot without opt-in).
