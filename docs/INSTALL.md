# Installing Wavr

Four ways to run Wavr, from "one command" to "I want full control". Pick the one
that matches your machine. All four end up in the same place: a local backend at
`http://127.0.0.1:8000` and a dashboard that walks you through first-run setup —
none of them ask you to edit a `.env` file.

**About the one-liner domain.** The eventual, intended URL for both bootstrap
scripts is `wavr.dev` — that's the domain the install website already names for
this exact purpose (`site/public/install.html`, `assets/wavr.js`'s
`BOOTSTRAP_DOMAIN`), and it's what `scripts/install.ps1 -InstallBaseUrl` defaults
to. **It isn't live yet** — the install website's own "one-line install script"
section says so ("Not live yet"), and that page still shows manual clone+pip steps
for Windows/Linux rather than these scripts, since it predates them. Until `wavr.dev`
is live, every command below uses the raw GitHub URL instead, which works today.

## Status — read this first

| Route | Verified how |
|---|---|
| Windows — self-contained installer (MSI/NSIS, `desktop/`, **no Python needed**) | **Built and run for real** on a Windows 11 machine with every Python interpreter stripped from `PATH`: the MSI (42.9 MB) and NSIS (41.9 MB) installers were installed **silently**, and `/healthz`, the dashboard, and the first-run setup wizard all answered/worked correctly. `.github/workflows/release.yml`'s `windows-desktop-smoke` job repeats the same silent-install → launch → `/healthz` → silent-uninstall round trip on every build, for both installer formats. **Not published anywhere yet** — see [below](#self-contained-installer-msi--nsis--no-python-needed). |
| Windows — `scripts/install.ps1` (developer/advanced bootstrap, needs Python) | **Run for real, end to end, on a Windows 11 machine**: standalone download from `github.com/augbastos/wavr` (master), fresh venv, editable install, backend start, `GET /healthz` and `GET /` both returned real responses, re-running upgraded in place without a duplicate process, `-Uninstall` removed the venv/source and left the database untouched. `-DryRun` checked separately (creates nothing). |
| Linux / Raspberry Pi (`scripts/install.sh`) | **Syntax-checked** (`sh -n`, `dash -n`) and **logic-tested on this Windows dev machine via Git Bash's dash/sh** (argument parsing, local-checkout detection, the tarball download+extract against the real repo, the uninstall path) — but **not run on an actual Linux box or real Raspberry Pi hardware**. The distro package-manager hints (apt/dnf/pacman/apk) and the systemd `--user` unit are written from documented behavior, not exercised against a live system. Treat this route as reviewed, not field-proven. |
| Docker / NAS (`scripts/wavr-docker.sh` + `docker-compose.yml`) | **Logic-tested without Docker** (this dev machine has no Docker installed): argument parsing, the "docker not found" failure path, and the empty-`.env`-creation behavior all verified directly. The `_INDEX` frontend-path fix (now a `COPY frontend/` in `backend/Dockerfile`) is **verified by reading `backend/Dockerfile` + `backend/wavr/app.py` line-by-line**, not by an actual `docker compose up` — reasoning below. |
| From source | The commands here are this repo's own, already-verified dev workflow (see the root `CLAUDE.md`, `.github/workflows/tests.yml`). |

## Which route

- **A Windows laptop/desktop, not a developer**: [Windows](#windows).
- **A Linux box, Raspberry Pi, or you want it to survive a reboot on its own**: [Linux / Raspberry Pi](#linux--raspberry-pi).
- **A NAS, or you already run everything in Docker**: [Docker / NAS](#docker--nas).
- **You're hacking on Wavr itself**: [From source](#from-source).

All four write nothing outside their own install directory except, on Linux, an
optional `systemd --user` unit file — and none of them ever touch camera hardware
or turn on network scanning; Wavr's own first-run screen asks about those.

## Windows

Two routes. **Neither needs a Python install any more if you use the first one** —
but the first one has nothing to download yet, so the second (`scripts/install.ps1`)
is the one that actually works today.

### Self-contained installer (MSI / NSIS) — no Python needed

`desktop/sidecar/wavr-core.spec` freezes the whole Core — FastAPI, uvicorn, SQLite,
the dashboard — into one ~40 MB executable. The Tauri desktop shell
(`desktop/src-tauri`) prefers that bundled binary over `python -m wavr.serve`
(`main.rs`'s `bundled_core()`), and `tauri.conf.json` ships it as an `externalBin`.
The result is a single native Windows installer that needs nothing else on the
machine — no Python, no pip, no venv.

**Verified for real** (2026-09-03): the MSI (42.9 MB) and NSIS (41.9 MB) installers
were built and installed **silently** on a Windows 11 machine with every Python
interpreter removed from `PATH`. `/healthz`, the dashboard, and the first-run
setup wizard all answered/worked correctly — the app never fell back to looking
for a system Python, because it never needs to. `.github/workflows/release.yml`'s
`windows-desktop-smoke` job repeats this exact round trip (silent install → launch
→ `/healthz` → silent uninstall) on every CI build, for both installer formats,
so this is not a one-off manual check.

**Unsigned — and there's nothing to download yet.** Two separate, honest caveats:

- Wavr has no code-signing certificate. Windows SmartScreen will warn ("Windows
  protected your PC") the first time you run either installer — that is expected
  for an unsigned binary from a small open-source project, not a sign of
  tampering. When a release does ship, verify the file you downloaded against the
  `SHA256SUMS.txt` published alongside it before trusting the SmartScreen bypass.
- **No release has been published.** `.github/workflows/release.yml` builds both
  installers on every version-tagged push, but the resulting GitHub Release is
  always left as an unpublished **draft** ("a human presses publish, always") —
  that step has not happened yet. There is currently no link to click and no file
  to download; this route is built and verified, not shipped.

Until a release exists, the only way to get this installer is to build it
yourself from source: see `desktop/BUILD.md` for the PyInstaller sidecar step that
has to run before `npm run tauri build`. If that sounds like more setup than you
want, use `scripts/install.ps1` below instead — it's the route that works today
without building anything.

### `scripts/install.ps1` — the developer/advanced bootstrap (needs Python)

```powershell
irm https://raw.githubusercontent.com/augbastos/wavr/master/scripts/install.ps1 | iex
```

What it does: finds an existing Python 3.11+ (the `py` launcher, or `python`/`python3`
on PATH) — if none is found it tells you exactly what to install and stops, it never
downloads or runs a Python installer for you; downloads Wavr into
`%LOCALAPPDATA%\Wavr\src` (a branch-tip archive, not a full `git clone` — this repo's
`.git` history is large from a past privacy scrub, the source tree itself is small);
creates a venv at `%LOCALAPPDATA%\Wavr\venv`; installs the backend; starts it with its
data (`wavr.db`, your floor plan) in `%LOCALAPPDATA%\Wavr`; waits for `/healthz`; opens
your browser at the dashboard.

Needs no admin rights. Writes no `.env` — open the dashboard and use its settings UI.

Re-run the same command any time to upgrade in place (it never creates a second copy,
and if Wavr is already running it leaves it alone).

```powershell
# Local file instead of the one-liner, with extra options:
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun     # show what would happen, change nothing
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Extras camera,mqtt
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall # removes the venv + source; NEVER the database
```

If you only ever ran the piped one-liner (no local file saved), passing `-Uninstall`
needs the wrapped form — a plain `irm ... | iex -Uninstall` does **not** pass the
flag through, `iex` just ignores it. This form does (verified):
```powershell
iex "& { $(irm https://raw.githubusercontent.com/augbastos/wavr/master/scripts/install.ps1) } -Uninstall"
```

**If you already have a Wavr checkout with your own `.env`** (you're a Wavr
developer): running this script from inside that checkout installs it editable
*from* that checkout rather than downloading a copy — but your checkout's `.env` gets
loaded too (Python's `dotenv` looks next to the actual source file, not at this
script's separate data directory). Concretely, a checkout with `WAVR_MULTIDEVICE=1`
and no `[tls]` extra installed will fail to start with *"Local TLS needs the
'cryptography' package"* — add `-Extras tls`, or just run the one-liner from an empty
folder instead to get a clean, `.env`-free copy.

**Troubleshooting:**

| Symptom | Fix |
|---|---|
| "No Python 3.11+ found" | Install from [python.org](https://www.python.org/downloads/windows/) (check "Add python.exe to PATH") or `winget install --id Python.Python.3.12 -e`, then re-run. |
| Backend never becomes healthy / port already in use | Something else is on port 8000 — pass `-Port 8001` (or whichever is free). |
| "Local TLS needs the 'cryptography' package" | See the checkout/`.env` note above — add `-Extras tls`. |
| Want it gone but keep your rooms/history | `-Uninstall` — it prints the exact `wavr.db` path if you also want to delete that yourself. |

## Linux / Raspberry Pi

```sh
curl -fsSL https://raw.githubusercontent.com/augbastos/wavr/master/scripts/install.sh | sh
```

Works on Debian/Ubuntu/Raspberry Pi OS, Fedora, Arch and Alpine — it detects which
one you're on (`/etc/os-release`) and, separately, whether you're on a Raspberry Pi
(`/proc/device-tree/model`) purely to report it. **This script never runs `sudo`
itself.** If Python 3.11+ or the `venv` module for it isn't installed, it prints the
exact command for your distro and stops — you run it, then re-run the installer.

```sh
curl -fsSL .../install.sh | sh -s -- --dir /opt/wavr --port 8000
# or, downloaded first:
sh install.sh --extras camera        # comma-separated pyproject extras
sh install.sh --no-start             # set up but don't start
sh install.sh --uninstall            # removes venv + source + the systemd unit; NEVER the database
```

Data lives in `~/.local/share/wavr` (override with `--dir`) — same layout as Windows:
a venv, a downloaded source copy (skipped if you're already inside a checkout), and
`wavr.db` sitting directly in that directory.

**Autostart at login is offered, never forced.** If `systemd` is present, the
installer asks (or pass `--autostart` / `--no-autostart` to decide up front, useful
for a scripted/headless provisioning run) before writing a `systemd --user` unit —
it prints the exact `systemctl --user enable --now wavr.service` command rather
than running it for you. To also start at *boot*, before anyone logs in (the usual
intent on a headless Pi), user units additionally need lingering enabled, which
needs root and is never run automatically:
```sh
sudo loginctl enable-linger "$(whoami)"
```

**Not yet field-tested** — reviewed and logic-tested on this dev machine (see the
status table above), but not run on real Linux/Pi hardware. If a distro's package
name is wrong or a step behaves differently on your system, that's the most likely
place to look; please report it rather than assume the fix is obvious.

**Troubleshooting:**

| Symptom | Fix |
|---|---|
| "No Python 3.11+ found" | The script prints your distro's exact install command (apt/dnf/pacman/apk) — run it, then re-run the installer. Debian 12 / Ubuntu 24.04 / current Raspberry Pi OS ship 3.11+ by default; older Ubuntu/Debian may need a PPA (deadsnakes) for a newer interpreter. |
| venv creation fails | Usually a missing `venv` package for your distro's Python (e.g. `python3-venv` on Debian/Ubuntu) — the script's error message names it. |
| Need curl or wget | Install one; the script refuses to guess. |
| Same `.env`-inheritance caveat as Windows | See the Windows section above — identical mechanism (`python-dotenv`), same fix (`--extras tls` or a clean download). |

## Docker / NAS

Advanced route: you already have Docker, and want Wavr as a container (e.g. on a
NAS or a Linux mini-PC/appliance). Run this **from inside a Wavr checkout**:

```sh
./scripts/wavr-docker.sh up       # build + start, wait for /healthz, print the URL
./scripts/wavr-docker.sh logs     # follow container logs
./scripts/wavr-docker.sh status   # docker compose ps
./scripts/wavr-docker.sh down     # stop + remove
```

This wraps `docker-compose.yml` at the repo root. Two things worth knowing about
that file:

- **`network_mode: host` is Linux-only.** It's what lets the container's
  `127.0.0.1` bind satisfy the app's loopback-only guard without touching any code.
  Docker Desktop on Windows/Mac does not support host networking the same way — the
  wrapper script warns (doesn't block) if it detects a non-Linux host, but for the
  actual running service on Windows/Mac, use the [Windows](#windows) or
  [Linux / Raspberry Pi](#linux--raspberry-pi) route instead and save Docker for a
  real Linux box.
- **The image has to contain `frontend/`, and now does.**
  `wavr.app` resolves its dashboard HTML two directories above the installed package
  (`Path(__file__).resolve().parents[2] / "frontend" / "index.html"`, i.e.
  `/app/frontend/index.html`). `backend/Dockerfile` used to `COPY` only `backend/`,
  so every container served the API and **404'd on `GET /`** — it built, started and
  passed `/healthz` with no user interface. It now copies `frontend/` in as well, and
  `.dockerignore` no longer excludes it.
  `docker-compose.yml` also bind-mounts `./frontend:/app/frontend:ro`, but that is now
  only a dev convenience (edit the dashboard without rebuilding); the image works
  without it. Verified by reading the path resolution against the Dockerfile's `COPY`
  layout, not by an actual `docker compose up` — this dev machine has no Docker.

Access from another device on your LAN: an SSH tunnel keeps the loopback guard
intact — `ssh -L 8000:127.0.0.1:8000 user@your-nas`, then open
`http://127.0.0.1:8000` locally. Don't rebind to `0.0.0.0`; see
`docs/deploy/bring-up-and-expansion.md`.

**The base image is lean — no camera support.** `backend/Dockerfile` doesn't install
`torch`/`opencv` (the `[camera]` extra); that's presence-by-network/Bluetooth/mmWave
only. A GPU/camera variant is planned but not built — see
`docs/deploy/bring-up-and-expansion.md`'s "Variant GPU/câmera (follow-up)". For
camera detection today, use the Windows or Linux native route instead.

`env_file: .env` needs that file to exist; the wrapper creates an empty one
automatically if it's missing (compose refuses to start otherwise) — the base
install needs no keys in it at all, add `GEMINI_API_KEY`/`WAVR_*` there only if you
want an optional feature that needs one.

## From source

For contributors, or if none of the above fits:

```sh
git clone https://github.com/augbastos/wavr.git
cd wavr
pip install -e "backend[dev]"
python -m wavr.serve                   # loopback 127.0.0.1:8000
python -m pytest backend/tests -q      # full suite; all hardware mocked
```

On Windows, `scripts\wavr-desktop.ps1` does the same thing plus opens your browser
once the backend answers. This is the same workflow the CI suite uses
(`.github/workflows/tests.yml`) and the one the root `CLAUDE.md` documents — it is
the ground truth every other route in this document is built to match, in
particular the requirement that `frontend/` stay a sibling of `backend/` (see the
`scripts/install.ps1` / `scripts/install.sh` header comments for exactly why).

## What none of these routes do

- **None writes a `.env`.** Configuration (LAN access, instance name, sensitivity,
  which sensors are on) lives in the dashboard's own settings UI
  (`backend/wavr/settings_store.py`) once you open it — never a file you're expected
  to hand-edit.
- **None enables a camera, network scan or LAN access on your behalf.** Every one of
  those stays off until you turn it on yourself, in the dashboard, same as if you'd
  run `python -m wavr.serve` by hand.
- **None phones home.** The only network traffic any installer makes is fetching
  Wavr's own source from `github.com` (skipped entirely if you already have a
  checkout) — nothing about your install, your network or your home is sent
  anywhere.
