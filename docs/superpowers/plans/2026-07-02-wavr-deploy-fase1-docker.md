# Wavr Deploy Fase 1 — Dockerize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Package the Wavr backend as a portable container so migrating laptop → Jetson/appliance is `pull + .env + docker compose up`, no code change. Ship a lean base `Dockerfile`, a `docker-compose.yml`, a `.dockerignore`, and concrete build/run docs — with the loopback-security posture preserved and the GPU/camera image documented as a follow-up variant.

**Architecture:** A lean `python:3.11-slim` image installs the backend package with its BASE deps only (fastapi/uvicorn/websockets/dotenv) — it runs everything except live camera CV (network + ruview + sim + fusion + rules/away + narration). The container uses `network_mode: host` and binds `127.0.0.1:8000` so the in-app loopback guard (peer must be 127.0.0.1) keeps working UNCHANGED — no app code change. LAN access to the dashboard is via SSH tunnel (the documented posture). The camera/GPU path (torch + cv2, `[camera]` extra) is a heavier separate image variant, documented but not built here.

**Tech Stack:** Docker, docker-compose, Python 3.11-slim base.

## Global Constraints

- Platform: authored on Windows 11, but the container TARGET is Linux (Jetson/mini-PC appliance) — `network_mode: host` works there. On Windows Docker Desktop, host networking is limited; dev-on-Windows continues to run uvicorn directly (not Docker). Document this.
- **SECURITY — preserve the loopback guard, no app change:** the app rejects any request whose peer isn't `127.0.0.1`/`::1`. The container must bind `127.0.0.1:8000` under `network_mode: host` so localhost access on the host has a real loopback peer. Do NOT bind `0.0.0.0` in a bridge network (all requests would 403 from the Docker gateway) and do NOT relax the in-app guard.
- **SECRETS:** the RTSP creds / GEMINI_API_KEY live in the mounted `.env` (`env_file`), NEVER baked into the image. `.dockerignore` MUST exclude `.env`, `.venv`, `*.db`, `.git`, `__pycache__`, `.superpowers`, and the `frontend/` deploy artifacts that don't belong in the backend image.
- **PERSISTENCE:** the SQLite DB (`WAVR_DB`) lives on a mounted volume so it survives container restarts. Derived-only (already enforced by `storage.py`).
- **Lean base:** the base image must NOT install `[camera]` (torch/cv2 — gigabytes) or `[mqtt]`/`[genai]` unless enabled. Those are opt-in extras; the base runs network/ruview/sim/rules/away/narration-503. Document how to build the GPU/camera variant.
- Docker is NOT installed in the authoring environment — the implementer CANNOT run `docker build`/`docker run`. Validate by careful authoring + `hadolint`/`docker build --check` ONLY if available; otherwise document build/run as the manual verification step. Do NOT claim a successful build that wasn't run.
- Only new infra files + the deploy doc touched. NO backend/Python/frontend code change. `fusion.py`/app untouched.

**Branch:** `deploy-fase1-docker` off `master`.

**Existing structure:** backend package at `backend/wavr/`, `backend/pyproject.toml` (`[project] name="wavr"`, base deps fastapi/uvicorn[standard]/python-dotenv/websockets; optional extras `dev`/`camera`/`mqtt`/`genai`). Module entry `wavr.app:app` (`app = create_app()` at import). `create_app` opens `Storage(cfg.db_path)` + `CameraStore(cfg.db_path)` at construction → `WAVR_DB` should point into the mounted volume. Config reads `WAVR_*` env + `GEMINI_API_KEY` via `load_dotenv()`.

---

### Task 1: Dockerfile + docker-compose + .dockerignore + deploy docs

**Files:**
- Create: `backend/Dockerfile`
- Create: `docker-compose.yml` (repo root)
- Create: `.dockerignore` (repo root)
- Modify: `docs/deploy/bring-up-and-expansion.md` (Fase 1 section — concrete build/run + caveats)

**Interfaces:** none (infra config; no Python API).

- [ ] **Step 1: Write `.dockerignore`** (repo root)

```
.git
.gitignore
.venv
**/__pycache__
**/*.pyc
*.db
.env
.superpowers
.wrangler
.playwright-mcp
docs
frontend
node_modules
```

> `frontend/` is deployed separately (Cloudflare Pages) and served by the backend via a path lookup only in dev; the container image is the backend API. If you want the container to also serve the dashboard same-origin, KEEP `frontend/` OUT of `.dockerignore` and COPY it — but that's optional; note the choice. Default: exclude it (API-only image; dashboard via Pages or a tunnel to the host). If including it, the app's `_INDEX` path (`parents[2]/frontend/index.html`) must resolve inside the image — verify the COPY layout matches.

- [ ] **Step 2: Write `backend/Dockerfile`** (lean base)

```dockerfile
# Wavr backend — lean base image (network + ruview + sim + fusion + rules/away + narration).
# The camera/GPU path (torch+cv2, [camera] extra) is a separate heavier variant — see docs.
FROM python:3.11-slim

# Non-root user (least privilege; the app needs no root).
RUN useradd --create-home --uid 10001 wavr
WORKDIR /app

# Install deps first (layer caching): copy only the package metadata + source.
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/wavr /app/backend/wavr
RUN pip install --no-cache-dir -e /app/backend

# Data dir for the SQLite DB (mounted as a volume at runtime).
RUN mkdir -p /data && chown -R wavr:wavr /data /app
USER wavr

ENV WAVR_DB=/data/wavr.db
EXPOSE 8000

# Bind loopback ONLY: with network_mode: host (Linux appliance) this keeps the in-app
# loopback-peer guard working. LAN access to the dashboard is via SSH tunnel.
CMD ["python", "-m", "uvicorn", "wavr.app:app", "--host", "127.0.0.1", "--port", "8000"]
```

> If Step 1 kept `frontend/` in the image, add `COPY frontend /app/frontend` and confirm `wavr/app.py`'s `_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"` resolves to `/app/frontend/index.html` given `wavr` is at `/app/backend/wavr/` (parents[2] of `/app/backend/wavr/app.py` = `/app` → `/app/frontend`). If excluding frontend, the `GET /` route will 500 on a missing file — that's acceptable for an API-only image; note it (dashboard served elsewhere).

- [ ] **Step 3: Write `docker-compose.yml`** (repo root)

```yaml
services:
  wavr:
    build:
      context: .
      dockerfile: backend/Dockerfile
    # network_mode: host keeps the in-app loopback guard valid (peer stays real loopback
    # on localhost access). Linux/appliance only; on Docker Desktop Windows/Mac host
    # networking is limited — run uvicorn directly there instead (see docs).
    network_mode: host
    env_file:
      - .env            # WAVR_* + GEMINI_API_KEY; never baked into the image
    volumes:
      - wavr-data:/data # SQLite persists across restarts (derived RoomState only)
    restart: unless-stopped

    # --- GPU/camera variant (opt-in): uncomment when using a torch+cv2 image (see docs).
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: 1
    #           capabilities: [gpu]

volumes:
  wavr-data:
```

- [ ] **Step 4: Update the deploy doc** — in `docs/deploy/bring-up-and-expansion.md`, replace the Fase 1 bullet list with concrete, verified-shape instructions:

Add build/run:
```bash
# On the Linux appliance (Jetson/mini-PC):
cp /path/to/.env .env            # your WAVR_* + GEMINI_API_KEY
docker compose up -d --build     # builds the lean base image, starts on 127.0.0.1:8000
# Dashboard from another device on the LAN: SSH tunnel (keeps the loopback guard intact)
ssh -L 8000:127.0.0.1:8000 user@appliance   # then open http://127.0.0.1:8000 locally
```

Document, in that section:
- **Why `network_mode: host` + `127.0.0.1` bind:** preserves the loopback-peer guard with no code change; LAN access via SSH tunnel (matches the documented loopback-only posture).
- **Windows caveat:** Docker Desktop host networking is limited — on the Windows dev laptop, run `uvicorn` directly (the current way), not Docker. Docker is the appliance/Linux path.
- **GPU/camera variant:** the base image is lean (no torch/cv2). To run live camera detection, build a variant that installs `[camera]` on an `nvidia/cuda` base (or `pip install -e backend[camera]` with a CUDA-enabled torch), and uncomment the `deploy.resources` GPU stanza in compose + install `nvidia-container-toolkit` on the host. Note this is a large image and a follow-up.
- **Secrets:** `.env` is mounted via `env_file`, never in the image; `.dockerignore` excludes it.

- [ ] **Step 5: Validate what can be validated (no Docker in this env)**

Docker is NOT installed here — you cannot `docker build`. Do these instead and REPORT honestly:
- Re-read the Dockerfile/compose for syntax + the layer/`COPY` path correctness (does `pip install -e /app/backend` find `pyproject.toml` + `wavr`? does the `_INDEX` note match if frontend is included?).
- Confirm `.dockerignore` excludes `.env`/`.venv`/`*.db`/`.git`/`.superpowers` (no secrets or bloat in the build context).
- If `hadolint` or `docker` happens to be on PATH, run `hadolint backend/Dockerfile` / `docker build --check`; otherwise state clearly that a live build was NOT performed and is the manual verification step.
- Run the Python suite once to confirm NO code was accidentally changed: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (expect 110/110 unchanged).

- [ ] **Step 6: Commit**

```powershell
git add backend/Dockerfile docker-compose.yml .dockerignore docs/deploy/bring-up-and-expansion.md
git commit -m "feat: Dockerize backend (lean base, network_mode host preserves loopback guard, GPU variant documented)"
```

---

## Definition of Done
- [ ] `backend/Dockerfile` builds a lean non-root Python 3.11 image installing the base package (no torch/cv2), binding `127.0.0.1:8000`.
- [ ] `docker-compose.yml` uses `network_mode: host` + `env_file: .env` + a persistent SQLite volume + `restart: unless-stopped`; GPU stanza present but commented (opt-in).
- [ ] `.dockerignore` excludes secrets (`.env`), the venv, DBs, git, and scratch — the build context carries no credentials.
- [ ] The deploy doc has concrete `docker compose up` instructions, the SSH-tunnel LAN-access note, the Windows-Docker-Desktop caveat, and the GPU/camera-variant follow-up.
- [ ] No backend/frontend code changed; Python suite still 110/110. The report states honestly that a live `docker build` was NOT run (Docker absent) and is the manual verification step.

## Next
Manual: install Docker Desktop (or on the Jetson), `docker compose up -d --build`, verify the dashboard via SSH tunnel. Then the GPU/camera image variant for live detection. Fase 2 = the dedicated appliance on a VLAN.
