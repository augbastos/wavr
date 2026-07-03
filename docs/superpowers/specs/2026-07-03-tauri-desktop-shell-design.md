# Wavr Desktop — Tauri v2 shell (design)

> Status: scaffold on branch `tauri-shell` (not merged). Needs the Rust toolchain to
> build; authored on a machine without it, so the Rust is correct-by-convention and
> **must be compiled + run once before merge**.

## Goal

Package the existing Wavr backend + dashboard as one native desktop app: a tray-resident
**central** that starts sensing when opened and can be reached by LAN companions (the
mobile PWA / a second PC). Closes the "Wavr desktop is the central" story from the roadmap
— the mobile companion is already an installable PWA; this is the desktop wrapper.

## What it is NOT

- Not a rewrite. The backend (FastAPI) and the dashboard (`frontend/index.html`) are
  unchanged. Tauri is a native window + tray + process lifecycle around them.
- Not a new network surface. The shell runs the backend in its **default loopback mode**
  (`python -m wavr.serve`, `WAVR_MULTIDEVICE` unset → HTTP on `127.0.0.1:8000`). All the
  existing loopback/central guarantees hold. LAN access stays a separate, explicit opt-in
  (the user exports `WAVR_MULTIDEVICE=1` + runs the LAN launcher) — the desktop shell does
  not silently open the LAN.

## Architecture

```
┌─ Wavr Desktop (Tauri, Rust) ──────────────────────────────┐
│  setup():                                                  │
│    1. spawn  python -m wavr.serve   (child process)        │
│         cwd = backend/ , stdout/stderr piped to a log      │
│    2. poll   http://127.0.0.1:8000/api/state  until 200    │
│         (retry ~15s, fail → error dialog + quit)           │
│    3. create webview window → External(127.0.0.1:8000)     │
│  tray: Open Wavr · Quit                                     │
│  window close  → hide to tray (backend keeps sensing)      │
│  Quit          → kill child → process exits → VRAM freed    │
└────────────────────────────────────────────────────────────┘
          │ loopback HTTP/WS (same-origin)
          ▼
   FastAPI backend  ──►  FusionEngine ──► RoomState ──► dashboard
   (the webview loads the backend's OWN index.html, so the frontend
    sees a loopback host and shows full `central` controls)
```

### Why spawn, not bundle (MVP)

Bundling a full CPython + torch/opencv venv into the installer is heavy and platform-
specific. For the MVP the shell **spawns an already-installed backend**, resolved in this
order:

1. `WAVR_PYTHON` env var (absolute path to the venv's python), else
2. `../.venv/Scripts/python.exe` relative to the app (dev layout), else
3. `python` on `PATH`.

Backend working dir: `WAVR_BACKEND_DIR` or `../backend` relative to the app. Port:
`WAVR_PORT` (default 8000). This keeps the shell honest and small; full self-contained
packaging (PyInstaller one-file sidecar wired via `tauri-plugin-shell`'s sidecar) is a
documented follow-up, not MVP.

### Lifecycle = the on/off control plane

- **Open / launch** → backend starts → sensing on.
- **Close window** → hide to tray, backend keeps running (the central is meant to stay on).
- **Quit (tray)** → the child backend is killed; the process exiting releases GPU VRAM
  (the camera source's `release_model()` + process-exit path), exactly as documented for
  the standalone backend. No orphaned python.

### Security / privacy invariants preserved

- Backend runs loopback-only (multidevice off) → no new listener, no LAN exposure from the
  shell. The webview origin is `http://127.0.0.1:8000`, so the dashboard's own 3-way
  detection resolves to `central` (full controls), same as opening it in a browser today.
- Tauri CSP restricts the webview to `connect-src` / `img-src` of `http://127.0.0.1:8000`
  and `ws://127.0.0.1:8000` (+ `'self'`); no external origins. Matches the dashboard's
  existing zero-external-request rule.
- No secrets in the shell. The backend reads its own `.env` as always; the shell passes
  through only `WAVR_PYTHON` / `WAVR_BACKEND_DIR` / `WAVR_PORT`.

## Components (branch `tauri-shell`, under `desktop/`)

| File | Purpose |
|------|---------|
| `desktop/src-tauri/tauri.conf.json` | Tauri v2 app config: window, bundle id, tray, CSP. |
| `desktop/src-tauri/Cargo.toml` | Rust deps: `tauri` v2 (+ `tray-icon`), `serde`, `ureq` (health poll). |
| `desktop/src-tauri/build.rs` | Standard `tauri_build::build()`. |
| `desktop/src-tauri/src/main.rs` | Entry: spawn backend, health-poll, tray, window, quit-kills-child. |
| `desktop/src-tauri/icons/` | App/tray icons (generated from `frontend/icon.svg` at build). |
| `desktop/package.json` | Tauri CLI via npm (`npm run tauri dev` / `build`). |
| `desktop/BUILD.md` | Prereqs (rustup + MSVC build tools) + build/run steps. |
| `scripts/wavr-desktop.ps1` | **Interim, works today:** starts the backend + opens the browser (the "click-to-open" experience without waiting for the Rust build). |
| `docs/adr/0007-desktop-shell.md` | The decision record. |

## Testing

- The shell adds no Python surface, so the backend test suite is unaffected.
- `tauri.conf.json` / `Cargo.toml` / `package.json` are validated as well-formed JSON/TOML.
- Manual acceptance (once Rust is installed): `npm run tauri dev` → window shows the live
  dashboard on loopback; close → hides to tray, `/api/state` still served; Quit → no
  python process left (`Get-Process python`), GPU VRAM back.
- The interim `wavr-desktop.ps1` is runnable today and is the fallback if the Tauri build
  is deferred.

## Open decisions (for review when awake)

1. **Bundle Python later?** MVP spawns an installed venv. Confirm whether the portfolio
   goal wants a true one-click installer (PyInstaller sidecar) or the dev-spawn is enough.
2. **Auto-start on login?** Tauri autostart plugin is available; left OFF by default (a
   sensing app that silently starts at boot deserves an explicit opt-in).
3. **Directory name** `desktop/` vs `src-tauri/` at root — used `desktop/` to keep the
   Rust/Node shell clearly separate from the Python `backend/`.
