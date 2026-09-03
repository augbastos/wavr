#!/bin/sh
# Wavr -- Docker/NAS route (the advanced install path). Thin wrapper around
# `docker compose` for docker-compose.yml at the repo root. POSIX sh, no bashisms.
#
# Unlike scripts/install.sh, this does NOT fetch source on its own -- it assumes you
# already have the repo (git clone, or a copy) on the box that will run Docker, e.g.
# a NAS or a Linux appliance. Run it from inside that checkout:
#   ./scripts/wavr-docker.sh up
#
# network_mode: host (docker-compose.yml) is LINUX-ONLY, on purpose -- it's what lets
# the container's 127.0.0.1 bind satisfy wavr.app's loopback-peer guard without
# touching any code (see docker-compose.yml's own comment, and
# docs/deploy/bring-up-and-expansion.md). Docker Desktop on Windows/Mac does not
# support real host networking the same way, so this script warns (not blocks) when
# `uname -s` isn't Linux.
#
# The base image (backend/Dockerfile) is lean: no torch/cv2, so no camera detection --
# that's an explicitly separate, not-yet-built variant (see
# docs/deploy/bring-up-and-expansion.md "Variant GPU/camera (follow-up)"). This
# script does not pretend that variant exists.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
ENV_FILE="$REPO_ROOT/.env"
PORT=8000   # fixed: backend/Dockerfile's CMD hardcodes `uvicorn ... --port 8000`,
            # bypassing wavr.serve (and WAVR_PORT) entirely -- see serve.py's own
            # "F1" note on this. Changing it means editing the Dockerfile's CMD.

info() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: wavr-docker.sh [up|down|logs|status]

  up      Build (if needed) and start Wavr in the background, then wait for /healthz.
  down    Stop and remove the container.
  logs    Follow container logs.
  status  Show `docker compose ps`.

Run from inside a Wavr checkout (uses ../docker-compose.yml relative to this script).
EOF
}

ACTION="${1:-up}"
case "$ACTION" in
    up|down|logs|status) : ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown action: $ACTION" >&2; usage >&2; exit 1 ;;
esac

[ -f "$COMPOSE_FILE" ] || die "docker-compose.yml not found at $COMPOSE_FILE -- run this from inside a Wavr checkout"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "docker (with the compose plugin) or docker-compose not found. Install Docker: https://docs.docker.com/engine/install/"
fi

if [ "$(uname -s 2>/dev/null || echo unknown)" != "Linux" ]; then
    warn "WARNING: this host is not Linux. docker-compose.yml uses network_mode: host,"
    warn "which Docker Desktop on Windows/Mac does not support the same way real Linux"
    warn "does (see docker-compose.yml's own comment). The container may build fine but"
    warn "not be reachable at 127.0.0.1:$PORT. On Windows/Mac, prefer scripts/install.ps1"
    warn "or scripts/install.sh (native run) instead of Docker for the actual service;"
    warn "this script is meant for a Linux box (NAS, mini-PC, Pi, appliance)."
fi

# `env_file: .env` requires the file to exist. It carries no defaults of its own --
# base install boots fine on config.py's built-in defaults -- so an empty file is a
# complete no-op, created only so compose doesn't refuse to start on a bare checkout.
if [ ! -f "$ENV_FILE" ]; then
    : > "$ENV_FILE"
    info "Created an empty $ENV_FILE (compose requires the file to exist; add WAVR_*/GEMINI_API_KEY here later if you want them -- never committed, .gitignored)."
fi

http_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -o /dev/null -m 2 "$1" 2>/dev/null
        return $?
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 2 -O /dev/null "$1" 2>/dev/null
        return $?
    fi
    return 1
}

case "$ACTION" in
    down)
        (cd "$REPO_ROOT" && $COMPOSE -f "$COMPOSE_FILE" down)
        ;;
    logs)
        (cd "$REPO_ROOT" && $COMPOSE -f "$COMPOSE_FILE" logs -f)
        ;;
    status)
        (cd "$REPO_ROOT" && $COMPOSE -f "$COMPOSE_FILE" ps)
        ;;
    up)
        (cd "$REPO_ROOT" && $COMPOSE -f "$COMPOSE_FILE" up -d --build)
        url="http://127.0.0.1:$PORT"
        info "Waiting for $url/healthz ..."
        i=0
        healthy=0
        while [ "$i" -lt 60 ]; do
            if http_ok "$url/healthz"; then
                healthy=1
                break
            fi
            i=$((i + 1))
            sleep 1
        done
        if [ "$healthy" -eq 1 ]; then
            info "Wavr is up: $url"
        else
            warn "Wavr did not answer at $url/healthz within 60s."
            warn "Check logs: $0 logs"
            exit 1
        fi
        ;;
esac
