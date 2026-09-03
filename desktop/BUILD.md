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
3. **The Wavr backend installed** in a venv — needed for `npm run dev` (which spawns
   `python -m wavr.serve` directly) and for hacking on the backend itself. The
   **release installer no longer needs this at runtime** (see "Building the
   self-contained installer" below) — a machine with no Python at all can still run
   the installed app — but you still want a venv for day-to-day shell development:
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

## Building the self-contained installer (release)

`npm run build` / `npm run tauri build` produces an MSI and an NSIS installer via
`bundle.externalBin` in `tauri.conf.json`, which points at
`desktop/src-tauri/binaries/wavr-core-<target-triple>.exe` — **a file that does not
exist in a fresh checkout.** `desktop/src-tauri/binaries/` is gitignored on purpose
(it holds a ~40 MB generated binary that has no business being committed), so you
must freeze it yourself first:

```bash
cd ..                       # repo root, same venv as above
pip install pyinstaller
python -m PyInstaller desktop/sidecar/wavr-core.spec --distpath desktop/sidecar/dist --noconfirm
mkdir -p desktop/src-tauri/binaries
# Tauri resolves externalBin by target triple, e.g. x86_64-pc-windows-msvc:
TRIPLE=$(rustc -vV | sed -n 's/^host: //p')
cp desktop/sidecar/dist/wavr-core.exe "desktop/src-tauri/binaries/wavr-core-${TRIPLE}.exe"

cd desktop
npm run icon                # bundle icons are gitignored too (generated)
npm run tauri build
```

Skip the PyInstaller step and `tauri build` fails with `resource path
binaries\wavr-core-<triple>.exe doesn't exist` — that message means exactly this,
not a Tauri misconfiguration. `.github/workflows/release.yml`'s `windows-desktop`
job runs these same steps (plus a smoke test of the frozen sidecar's `/healthz`
and dashboard) on every tagged release build; read it if you want the
exact, CI-verified sequence.

**Installed layout is three files**, not one: `wavr-desktop.exe` (the shell, ~7 MB),
`wavr-core.exe` (the frozen Core, ~38 MB), and an uninstaller. The shell binary is
named after the Cargo package (`wavr-desktop`, from `[package] name` in
`Cargo.toml`), **not** after `tauri.conf.json`'s `productName` ("Wavr Desktop") —
a check or script that greps for the product name instead of the binary name will
find nothing.

**Unsigned.** Wavr has no code-signing certificate; Windows SmartScreen warns on
first run of either installer format. This is the one item still genuinely
outstanding from the original MVP scope, not something to route around locally.
