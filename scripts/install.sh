#!/bin/sh
# Wavr -- one-shot Linux/Raspberry Pi bootstrap. POSIX sh (dash-clean, no bashisms),
# runnable as `curl -fsSL <url>/install.sh | sh`. The intended <url> is
# https://wavr.dev/install.sh -- the domain the install website already names for
# this (site/public/install.html, assets/wavr.js's BOOTSTRAP_DOMAIN) -- but that
# page marks it "Not live yet" as of this writing. Until it's live, fetch this file
# straight from GitHub instead:
#   curl -fsSL https://raw.githubusercontent.com/augbastos/wavr/main/scripts/install.sh | sh
#
# Installs (or upgrades) a standalone Wavr into ~/.local/share/wavr and starts it,
# without touching any dev venv you may already have in a repo clone. See
# scripts/install.ps1 for the Windows twin -- same design, same reasoning, this file
# just carries the POSIX-specific gotchas.
#
# WHERE THE SOURCE COMES FROM (two modes, auto-detected):
#   * Run from inside a Wavr checkout (this script's own directory -- when resolvable
#     from $0 -- or the current directory, has sibling backend/pyproject.toml +
#     frontend/index.html) -> that checkout is installed in place, editable. Nothing
#     is downloaded.
#   * Run standalone (piped, or from an unrelated directory) -> the branch tip of
#     $WAVR_GIT_URL is downloaded as a GitHub tarball (NOT `git clone`) into
#     $WAVR_DIR/src and installed editable from there. Tarball, not clone, on
#     purpose: this repo's .git history is ~1.6GB (a past PII scrub) while the
#     working tree is ~20MB -- a branch-tip tarball is the right tool, and it means
#     this script never requires git at all (only curl or wget + tar).
#
# WHY EDITABLE, AND WHY frontend/ MUST STAY NEXT TO backend/:
# wavr.app resolves its dashboard HTML via
# `Path(__file__).resolve().parents[2] / "frontend" / "index.html"`
# (backend/wavr/app.py) -- i.e. it expects frontend/ as a SIBLING of backend/, two
# levels above the installed wavr package. A normal (non-editable) `pip install`
# copies just the wavr package into site-packages, stranding it far from any
# frontend/ directory -- the dashboard 404s. So this script (a) always keeps the
# full repo layout (never just the backend/ subdirectory) and (b) always installs
# with `pip install --editable`, matching this repo's own CI
# (`pip install -e "backend[dev]"`, .github/workflows/tests.yml).
#
# THIS SCRIPT WRITES NO .env. Configuration (LAN mode, instance name, sensitivity,
# ...) is a job for the in-app settings UI (backend/wavr/settings_store.py, new on
# this branch) once you open the dashboard -- never this installer.
#
# CAVEAT, VERIFIED ON A REAL RUN: wavr.config calls python-dotenv's load_dotenv()
# with no path, which searches from the CALLING MODULE'S OWN FILE location, not from
# this script's data dir or working directory. So running this against a local
# checkout that itself has a .env (a dev's own repo clone) loads THAT .env into this
# install's process too -- WAVR_MULTIDEVICE, GEMINI_API_KEY, whatever is in it --
# even though its data lives in a completely separate $WAVR_DIR. Concretely: a
# checkout with WAVR_MULTIDEVICE=1 and no [tls] extra installed makes the backend
# crash on start with "Local TLS needs the 'cryptography' package" -- fix with
# --extras tls, or run this against a plain download instead of that checkout. A
# freshly-downloaded standalone source tree has no .env (git-ignored, never shipped)
# and is unaffected.
#
# Never runs sudo on its own. If root is needed (a missing system package), this
# prints the exact command for your distro and stops.
#
# Idempotent: re-running upgrades the existing venv/source in place rather than
# creating a second copy; if Wavr already answers on the target port, it is left
# alone (no duplicate process, no port fight).
set -eu

# ---- defaults / flags ---------------------------------------------------------------

WAVR_GIT_URL="https://github.com/augbastos/wavr.git"
WAVR_BRANCH="${WAVR_BRANCH:-main}"  # this repository's default branch (renamed from master)
WAVR_DIR="${WAVR_DIR:-$HOME/.local/share/wavr}"
WAVR_PORT="${WAVR_PORT:-8000}"
WAVR_EXTRAS=""
DO_UNINSTALL=0
DO_START=1
AUTOSTART_CHOICE=""   # "" = ask; "yes" / "no" = forced via flag

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --dir PATH        Install location (default: ~/.local/share/wavr)
  --branch NAME      Branch to fetch when run standalone (default: main)
  --port N           Backend port (default: 8000, or $WAVR_PORT)
  --extras LIST       Comma-separated pyproject extras, e.g. "camera,mqtt"
                      (see backend/pyproject.toml [project.optional-dependencies])
  --autostart        Install a systemd --user unit without asking
  --no-autostart     Skip the systemd --user unit offer entirely
  --no-start          Set everything up but do not start the backend
  --uninstall         Remove the venv, the standalone source download and the
                      systemd --user unit -- NEVER the database. Prints its path.
  -h, --help          This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) WAVR_DIR="$2"; shift 2 ;;
        --dir=*) WAVR_DIR="${1#--dir=}"; shift ;;
        --branch) WAVR_BRANCH="$2"; shift 2 ;;
        --branch=*) WAVR_BRANCH="${1#--branch=}"; shift ;;
        --port) WAVR_PORT="$2"; shift 2 ;;
        --port=*) WAVR_PORT="${1#--port=}"; shift ;;
        --extras) WAVR_EXTRAS="$2"; shift 2 ;;
        --extras=*) WAVR_EXTRAS="${1#--extras=}"; shift ;;
        --autostart) AUTOSTART_CHOICE="yes"; shift ;;
        --no-autostart) AUTOSTART_CHOICE="no"; shift ;;
        --no-start) DO_START=0; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# ---- small helpers --------------------------------------------------------------------

info()  { printf '%s\n' "$*"; }
ok()    { printf '%s\n' "$*"; }
warn()  { printf '%s\n' "$*" >&2; }
die()   { printf 'error: %s\n' "$*" >&2; exit 1; }

# Yes/no prompt that never hangs or misreads its own source as input. Under
# `curl ... | sh`, fd 0 IS the piped script text, not the terminal -- a bare `read`
# there would try to read the NEXT LINE OF THIS SCRIPT, not an answer from the user.
# Explicitly re-opening /dev/tty sidesteps that; if there is no controlling terminal
# (cron, CI, a fully non-interactive pipe) this just falls back to "no".
ask_yes_no() {
    if [ -r /dev/tty ]; then
        printf '%s [y/N] ' "$1" > /dev/tty
        reply=""
        read -r reply < /dev/tty || reply=""
    else
        reply=""
    fi
    case "$reply" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

download() {
    # $1 = url, $2 = destination file
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "need curl or wget to fetch Wavr -- install one and re-run"
    fi
}

http_ok() {
    # $1 = url -- 0 if it answers 200, 1 otherwise. Tries curl then wget; never sudo.
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -o /dev/null -m 2 "$1" 2>/dev/null
        return $?
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 2 -O /dev/null "$1" 2>/dev/null
        return $?
    fi
    return 1
}

# ---- environment: distro, arch, Raspberry Pi model -----------------------------------

show_environment() {
    ARCH="$(uname -m 2>/dev/null || echo unknown)"
    OS_ID=""
    OS_ID_LIKE=""
    OS_PRETTY=""
    if [ -r /etc/os-release ]; then
        # os-release is documented to be shell-sourceable; safe to `.` it.
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-}"
        OS_ID_LIKE="${ID_LIKE:-}"
        OS_PRETTY="${PRETTY_NAME:-}"
    fi
    info "OS: ${OS_PRETTY:-unknown} (id=${OS_ID:-?} id_like=${OS_ID_LIKE:-?}), arch: $ARCH"

    RPI_MODEL=""
    if [ -r /proc/device-tree/model ]; then
        RPI_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
        [ -n "$RPI_MODEL" ] && info "Board: $RPI_MODEL"
    fi
}

# Distro family, for package-manager guidance only -- never used to decide whether to
# run anything as root; every branch below just PRINTS a command.
distro_family() {
    combined="$OS_ID $OS_ID_LIKE"
    case " $combined " in
        *" debian "*|*" ubuntu "*|*" raspbian "*) echo "debian" ;;
        *" fedora "*|*" rhel "*|*" centos "*) echo "fedora" ;;
        *" arch "*|*" manjaro "*) echo "arch" ;;
        *" alpine "*) echo "alpine" ;;
        *) echo "unknown" ;;
    esac
}

print_python_install_hint() {
    family="$(distro_family)"
    warn ""
    warn "No Python 3.11+ found. Install it yourself, then re-run this script."
    case "$family" in
        debian)
            warn "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
            warn "  (Debian 12 / Ubuntu 24.04 / current Raspberry Pi OS ship python3 >=3.11 by"
            warn "  default. On an OLDER Ubuntu/Debian, your default python3 may be too old --"
            warn "  check with 'python3 --version'; if so you need a newer interpreter, e.g."
            warn "  via the deadsnakes PPA: sudo add-apt-repository ppa:deadsnakes/ppa &&"
            warn "  sudo apt install -y python3.11 python3.11-venv)"
            ;;
        fedora)
            warn "  sudo dnf install -y python3 python3-pip"
            ;;
        arch)
            warn "  sudo pacman -S --needed python python-pip"
            ;;
        alpine)
            warn "  sudo apk add python3 py3-pip py3-virtualenv"
            ;;
        *)
            warn "  Use your distro's package manager to install python3 (>=3.11) + its venv/pip"
            warn "  packages. This script does not know this distro well enough to guess the"
            warn "  exact command -- check 'cat /etc/os-release' and your distro's docs."
            ;;
    esac
    warn ""
    warn "This script never installs system packages for you -- it only prints the command."
}

# ---- Python discovery ------------------------------------------------------------------

find_python() {
    # Prints a python path for the first interpreter >=3.11 found, or nothing.
    # Checked via `sys.version_info` (never by parsing --version text) so this is
    # exact regardless of how a distro formats its version banner.
    for name in python3.13 python3.12 python3.11 python3 python; do
        candidate="$(command -v "$name" 2>/dev/null || true)"
        [ -n "$candidate" ] || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# ---- source resolution (local checkout vs standalone tarball download) ---------------

is_wavr_checkout() {
    # $1 = candidate directory
    [ -f "$1/backend/pyproject.toml" ] && [ -f "$1/frontend/index.html" ]
}

resolve_repo_root() {
    # $0 is a real path when this script is run as a file (`sh install.sh`,
    # `./scripts/install.sh`); under `curl ... | sh` there is no script file and $0 is
    # typically just "sh", so this branch naturally falls through to the cwd check.
    case "$0" in
        */*)
            script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || script_dir=""
            if [ -n "$script_dir" ]; then
                parent="$(cd "$script_dir/.." 2>/dev/null && pwd)" || parent=""
                if [ -n "$parent" ] && is_wavr_checkout "$parent"; then
                    printf '%s\n' "$parent"
                    return 0
                fi
            fi
            ;;
    esac
    here="$(pwd)"
    if is_wavr_checkout "$here"; then
        printf '%s\n' "$here"
        return 0
    fi
    return 1
}

extract_tarball() {
    # $1 = tarball path, $2 = destination (recreated fresh)
    archive="$1"; dest="$2"
    rm -rf "$dest"
    mkdir -p "$dest"
    if tar -xzf "$archive" -C "$dest" --strip-components=1 2>/dev/null; then
        return 0
    fi
    # Fallback for a tar without --strip-components support: extract to a scratch
    # dir, then copy the single top-level folder's contents up.
    tmp="$dest.extract-tmp"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    tar -xzf "$archive" -C "$tmp"
    inner="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    [ -n "$inner" ] || die "downloaded archive had no top-level folder (unexpected GitHub tarball layout)"
    cp -a "$inner"/. "$dest"/
    rm -rf "$tmp"
}

resolve_source() {
    if repo_root="$(resolve_repo_root)"; then
        info "Found a Wavr checkout at $repo_root -- installing from it (no download)." >&2
        printf '%s\n' "$repo_root"
        return 0
    fi

    case "$WAVR_GIT_URL" in
        https://github.com/*)
            owner_repo="${WAVR_GIT_URL#https://github.com/}"
            owner_repo="${owner_repo%.git}"
            ;;
        *)
            die "WAVR_GIT_URL must be a github.com https URL for the standalone (no local checkout) path; got: $WAVR_GIT_URL"
            ;;
    esac
    archive_url="https://github.com/${owner_repo}/archive/refs/heads/${WAVR_BRANCH}.tar.gz"
    src_dir="$WAVR_DIR/src/wavr"

    info "No local checkout found -- downloading Wavr ($WAVR_BRANCH) from $archive_url ..." >&2
    mkdir -p "$WAVR_DIR/src"
    tarball="$(mktemp "${TMPDIR:-/tmp}/wavr-src.XXXXXX")" || die "mktemp failed"
    download "$archive_url" "$tarball"
    extract_tarball "$tarball" "$src_dir"
    rm -f "$tarball"
    printf '%s\n' "$src_dir"
}

# ---- systemd --user unit (offered, never forced) --------------------------------------

offer_autostart() {
    # $1 = venv python, $2 = data dir, $3 = port
    if ! command -v systemctl >/dev/null 2>&1; then
        info "systemd not found -- skipping the autostart offer (start Wavr manually or via cron/rc.local)."
        return 0
    fi
    case "$AUTOSTART_CHOICE" in
        no) return 0 ;;
        yes) : ;;
        *)
            if ! ask_yes_no "Install a systemd --user unit so Wavr starts at login?"; then
                info "Skipping autostart. Re-run with --autostart any time to add it."
                return 0
            fi
            ;;
    esac
    unit_dir="$HOME/.config/systemd/user"
    unit_file="$unit_dir/wavr.service"
    mkdir -p "$unit_dir"
    cat > "$unit_file" <<EOF
[Unit]
Description=Wavr local sensing backend (loopback-only)
After=network.target

[Service]
Type=simple
Environment=WAVR_PORT=$3
Environment=WAVR_DB=$2/wavr.db
WorkingDirectory=$2
ExecStart="$1" -m wavr.serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    ok "Wrote $unit_file"
    info "Enable it yourself:"
    info "  systemctl --user daemon-reload && systemctl --user enable --now wavr.service"
    info "To also start at BOOT without logging in first (needs sudo, not run for you):"
    info "  sudo loginctl enable-linger $(whoami)"
}

# ---- uninstall --------------------------------------------------------------------------

do_uninstall() {
    venv_dir="$WAVR_DIR/venv"
    src_dir="$WAVR_DIR/src"
    db_path="$WAVR_DIR/wavr.db"
    house_map="$WAVR_DIR/house.json"
    unit_file="$HOME/.config/systemd/user/wavr.service"

    for d in "$venv_dir" "$src_dir"; do
        if [ -e "$d" ]; then
            rm -rf "$d"
            ok "Removed $d"
        fi
    done

    if [ -f "$unit_file" ]; then
        if command -v systemctl >/dev/null 2>&1; then
            systemctl --user disable --now wavr.service >/dev/null 2>&1 || true
        fi
        rm -f "$unit_file"
        ok "Removed $unit_file"
    else
        info "No systemd --user unit installed -- nothing to remove there."
    fi

    # NEVER delete data. Refuse and say exactly where it is.
    if [ -f "$db_path" ]; then
        warn "Your database is untouched: $db_path"
        warn "This script never deletes it. To remove it yourself:  rm -f \"$db_path\""
    fi
    if [ -f "$house_map" ]; then
        warn "Your floor plan is untouched: $house_map"
    fi
    ok "Uninstall complete."
}

# ---- main ---------------------------------------------------------------------------

if [ "$DO_UNINSTALL" -eq 1 ]; then
    do_uninstall
    exit 0
fi

show_environment

PYTHON="$(find_python)" || { print_python_install_hint; exit 1; }
info "Using Python: $PYTHON"

SOURCE_DIR="$(resolve_source)"
BACKEND_DIR="$SOURCE_DIR/backend"

mkdir -p "$WAVR_DIR"
VENV_DIR="$WAVR_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"

if [ -x "$VENV_PY" ]; then
    info "Reusing existing venv at $VENV_DIR"
else
    info "Creating venv at $VENV_DIR ..."
    venv_err="$(mktemp "${TMPDIR:-/tmp}/wavr-venv-err.XXXXXX")" || die "mktemp failed"
    if ! "$PYTHON" -m venv "$VENV_DIR" 2>"$venv_err"; then
        cat "$venv_err" >&2
        rm -f "$venv_err"
        family="$(distro_family)"
        warn ""
        warn "venv creation failed. Your Python's 'venv' module may be a separate package."
        case "$family" in
            debian) warn "  sudo apt install -y python3-venv" ;;
            fedora) warn "  sudo dnf install -y python3-libs" ;;
            *) warn "  Install your distro's venv package for this Python, then re-run." ;;
        esac
        exit 1
    fi
    rm -f "$venv_err"
fi

if [ -n "$WAVR_EXTRAS" ]; then
    # Braces are load-bearing: $VAR[ reads as an array expansion (SC1087).
    pip_target="${BACKEND_DIR}[${WAVR_EXTRAS}]"
else
    pip_target="$BACKEND_DIR"
fi
info "Installing Wavr (editable) from $BACKEND_DIR ..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install --upgrade --editable "$pip_target"

DB_PATH="$WAVR_DIR/wavr.db"
URL="http://127.0.0.1:$WAVR_PORT"

if [ "$DO_START" -eq 1 ]; then
    if http_ok "$URL/healthz"; then
        ok "Wavr is already running at $URL -- not starting a second copy."
    else
        info "Starting Wavr backend on $URL ..."
        # Subshell + exec: the backgrounded process IS python (no lingering shell),
        # and `cd` here never affects this installer script's own working directory.
        (
            cd "$WAVR_DIR"
            WAVR_DB="$DB_PATH" WAVR_PORT="$WAVR_PORT" exec "$VENV_PY" -m wavr.serve
        ) > "$WAVR_DIR/wavr.log" 2>&1 &

        i=0
        healthy=0
        while [ "$i" -lt 30 ]; do
            if http_ok "$URL/healthz"; then
                healthy=1
                break
            fi
            i=$((i + 1))
            sleep 1
        done
        if [ "$healthy" -eq 1 ]; then
            ok "Wavr is up."
        else
            warn "Wavr did not answer at $URL within 30s -- check $WAVR_DIR/wavr.log"
            exit 1
        fi
    fi
else
    info "Skipping start (--no-start). WAVR_DB would be: $DB_PATH"
fi

offer_autostart "$VENV_PY" "$WAVR_DIR" "$WAVR_PORT"

printf '\n'
ok "Wavr install dir : $WAVR_DIR"
ok "Dashboard        : $URL"
info "Re-run this same command any time to upgrade in place."
info "Uninstall (keeps your data): sh scripts/install.sh --uninstall  (or --dir $WAVR_DIR --uninstall if run standalone)"
