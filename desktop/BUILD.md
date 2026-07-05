# Building Wavr Desktop (Tauri v2)

A native tray app that wraps the loopback central — see
[ADR-0007](../docs/adr/0007-desktop-shell.md) and the
[design spec](../docs/superpowers/specs/2026-07-03-tauri-desktop-shell-design.md).

> **Status: multidevice-aware, compiles clean (2026-07-06).** `cargo build` is clean (no
> warnings) on Rust stable MSVC. The one remaining check is visual and needs a display —
> see "Verify on first run" below. The interim launcher (`scripts/wavr-desktop.ps1`)
> remains a zero-Rust click-to-open alternative.

## Modes: loopback-HTTP vs multidevice-HTTPS

The shell reads the SAME effective config the backend reads — process env first, else
`WAVR_BACKEND_DIR/.env` (mirroring python-dotenv's `override=False`) — and adapts to
`WAVR_MULTIDEVICE`:

- **off (default):** backend is plain HTTP on `127.0.0.1`. Probe + webview over `http`.
- **on:** the desktop is the LAN central. The backend binds HTTPS/WSS on `WAVR_BIND`
  (e.g. `0.0.0.0`) with a self-signed local cert (`wavr/tls.py`; SANs
  `localhost` / `127.0.0.1` / `<LAN-IP>`, default `~/.wavr/cert.pem`, or
  `WAVR_TLS_CERT` / `WAVR_TLS_DIR`). This shell still talks ONLY to the LOOPBACK side —
  `https://127.0.0.1:<port>` — and trusts the cert by **pinning it exactly**:
  - the readiness probe uses a rustls verifier that accepts only the on-disk cert's DER
    (never trust-all);
  - on Windows, the WebView2 `ServerCertificateErrorDetected` handler sets `AlwaysAllow`
    **only** when the request authority is exactly `127.0.0.1:<port>` **and** the presented
    cert is byte-identical to the on-disk cert — everything else is cancelled.
  - **Requirements:** the `[tls]` backend extra (`pip install -e backend[tls]`) so the
    backend can generate the cert, and a modern WebView2 Evergreen runtime
    (`ICoreWebView2_14`, runtime ≥ 1.0.1245). macOS/Linux HTTPS-webview trust is **not**
    implemented yet (Windows is the target); their probe pinning still works.

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
  placeholder) within a couple of seconds, with full **central** controls. In multidevice
  mode this is `https://127.0.0.1:8000` and the window must render WITHOUT a cert warning
  (the scoped pin allowed it); if the placeholder never advances, the pin/probe rejected
  the cert.
- **Close the window** → it hides to the tray; the backend keeps answering (sensing kept
  running): `curl http://127.0.0.1:8000/api/state`, or in multidevice mode
  `curl -k https://127.0.0.1:8000/api/state`.
- **Tray → Quit** → the window closes AND no python is left:
  `Get-Process python -ErrorAction SilentlyContinue` returns nothing. (This is what frees
  GPU VRAM.)

## Later (not MVP)

- **Self-contained installer:** bundle the backend as a PyInstaller one-file sidecar wired
  through `tauri-plugin-shell` so end users don't need a Python install.
- **Auto-start on login:** `tauri-plugin-autostart` (kept off by default — a sensing app
  should not silently start at boot without opt-in).
