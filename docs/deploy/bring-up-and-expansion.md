# Wavr — safe bring-up, and expanding without pain

**Goal:** run safely on an ordinary workstation today, and be able to move to a
dedicated appliance on a segmented network **at any time without rewriting
anything** — only changing where it runs.

## The principle that makes expansion cheap

Moving from a workstation to an appliance has to be a **configuration change,
not a code change**. That rests on four invariants that hold today and must not
be broken:

1. **Every deployment specific lives in env/config** — bind address, camera
   URLs, database path, model path and threshold, GPU device, fusion weights.
   Never hardcoded. (`config.py` already reads all of it from `WAVR_*`; keep it
   that way.)
2. **The service does not assume which machine it is on** — no absolute paths
   from one host, no fixed GPU index, no assumed `localhost` beyond the
   configurable bind.
3. **The safety guards live in the APP** — loopback bind, Host allowlist,
   `X-Wavr-Local` CSRF, cameras boot OFF, kill switch, derived-only storage.
   They travel with the code to any box. Network segmentation is **additive** on
   the appliance, never a replacement for them.
4. **Sources and weights are config-driven** — adding a third camera, or a new
   modality (BLE, mmWave), is configuration plus one new `SensorSource` class.
   The seam already exists (`_default_sources`, `SourceManager.register`). The
   core does not change.

While those four hold, expanding is trivial. The rest of this plan is: harden
now, containerise (the enabler), migrate later.

---

## Phase 0 — Harden the workstation deployment (works today)

**Practicality and basic safety, with no new hardware.**

1. **One `.env` as the portability seam.** Every `WAVR_*` in a single `.env`,
   already git-ignored. Secrets — RTSP camera credentials, provider API keys —
   live *only* there, never in code and never in Git. That file is what moves to
   the appliance intact.
   ```
   WAVR_NET_MACS=aa:bb:...,cc:dd:...     # device -> person
   WAVR_RUVIEW_URL=ws://localhost:3000/ws/sensing
   WAVR_CAM_CONFIDENCE=0.5
   ```
2. **Run without admin.** `arp -a`, `ping`, OpenCV/RTSP and YOLO all run as an
   ordinary user. Confirm nothing asks for elevation. A dedicated account,
   separate from your everyday one, is better still.
3. **Put the cameras on an isolated network now.** Most routers offer a guest
   SSID. Put the cameras there and **block their outbound internet access** at
   the router — a consumer camera phoning home is the privacy leak; cut it. This
   is the first step of segmentation, with hardware you already have.
4. **Loopback stays.** The dashboard remains loopback-only; reaching it from
   another device goes through an SSH tunnel, or later through the appliance.
5. **Open on demand rather than an always-on service** — because of GPU memory,
   see below. On a shared workstation the right model is: open Wavr when you
   want to watch, the process starts; close it, the process exits and the GPU
   memory is released in full. The always-on service belongs to the **appliance**
   (Phase 2), where nothing else is competing for the GPU.

### GPU memory lifecycle — used when open, released when closed

**Only the camera path (YOLO) uses GPU memory.** Network scanning, Wi-Fi CSI,
fusion, the dashboard and storage use none. The design scopes this naturally:

- **Wavr open, cameras OFF (the boot default)** → `_model()` is never called →
  **zero GPU memory.** Network and CSI presence run with the GPU untouched.
- **Turn a camera on** (a deliberate toggle) → YOLO loads on the first detection
  → GPU memory in use.
- **Close the program** (the process exits) → the driver returns **all** of it.

*Nuance:* while the process lives, the **CUDA context** holds a few hundred MB
even with every camera off, because the model stays cached in the process
(`_YOLO_MODEL`, a module global). Two ways out:

- **The full guarantee is closing the program** — the process dies and
  everything comes back. This is the recommended path on a shared machine.
- **Optional, to leave Wavr open without holding memory:** when the LAST camera
  is switched off, drop the model (`_YOLO_MODEL = None` plus
  `torch.cuda.empty_cache()`). That recovers most of it without exiting; the
  residual CUDA context only goes at exit.

**Code prerequisites before pointing this at a real camera** — each its own
small change:

- keep-alive in `CameraSource`, so a transient error does not kill the camera
  (reconnect, the way the CSI source already does);
- `SourceManager._run` must remove its own task when it finishes — a dead source
  must never report `active=True`, because the ON/OFF indicator is a safety
  control;
- a `threading.Lock` around `_model()` (two concurrent first-detections would
  otherwise load YOLO twice), and validation that `cam_interval > 0`.

---

## Phase 1 — Containerise (the enabler)

**This is the highest-leverage move for "expand at any time".** Once the backend
is a Docker image plus a `.env`, moving to ANY box is `pull`, copy `.env`,
`docker run`. The same environment on the workstation and on the appliance —
"works on my machine" stops being a category.

**Status:** `backend/Dockerfile`, `docker-compose.yml` and `.dockerignore` are in
the repository. The base image is lean — no torch, no OpenCV — covering network,
CSI, simulation, fusion, rules, away and narration. The camera/GPU variant is a
follow-up, below.

### Build and run (Linux appliance)

```bash
# On the Linux appliance:
cp /path/to/.env .env            # your WAVR_* values
docker compose up -d --build     # lean base image, starts on 127.0.0.1:8000
# Dashboard from another device on the LAN: SSH tunnel (keeps the loopback guard)
ssh -L 8000:127.0.0.1:8000 user@appliance   # then open http://127.0.0.1:8000
```

### Why `network_mode: host` plus a `127.0.0.1` bind

The app's loopback guard (`_LOOPBACK_HOSTS` in `wavr/app.py`) trusts the real
peer of the connection. With `network_mode: host` on Linux, the process inside
the container sees the host's network stack, so binding `127.0.0.1:8000`
preserves the guard **without touching code**.

Do not bind `0.0.0.0` on a bridge network: the Docker gateway would appear as a
non-loopback peer and the guard would reject every request with 403, including
the legitimate ones. Reaching it from another device on the LAN goes through an
SSH tunnel, which keeps the connection to the container local — the same
loopback-only posture as Phase 0.

### Caveat: Docker Desktop on Windows and macOS

`network_mode: host` does not behave as it does on Linux — Docker Desktop runs
containers in a VM and host networking is limited. **On Windows, keep running
`uvicorn` directly** (the Phase 0 way). Docker is the appliance/Linux path;
elsewhere it is for testing that the image builds, not for running the service.

### GPU/camera variant (follow-up)

The base image is lean and therefore covers network and CSI presence but **not**
camera detection. For a real camera: build a variant that installs
`pip install -e backend[camera]` on top of an `nvidia/cuda` base (torch with CUDA
support), uncomment the `deploy.resources.reservations.devices` stanza in
`docker-compose.yml`, and install `nvidia-container-toolkit` on the host. The
image is considerably larger; treat it as a separate deliverable.

### Secrets

`.env` is never copied into the image. It is mounted at runtime through
`env_file` in the compose file, and `.dockerignore` excludes it explicitly from
the build context, alongside `.venv`, `*.db` and `.git`. No credential ends up
in any image layer.

After this phase, workstation and appliance run **the same image** — the
difference is the `.env` and the network.

---

## Position radar — hardware

Extending from presence to position (x/y) and posture, with minimal hardware
that works from plain Python.

- **Tier R0 — one room, no soldering (~€15-20):** one HLK-LD2450 (~€10-15), a
  CP2102/CH340 USB-TTL adapter (~€3-5) and four female-female jumpers (5V, GND,
  TX, RX — note the LD2450 runs its UART at 256000 baud). It plugs straight into
  the machine: `WAVR_MMWAVE_PORT=COM3`, `WAVR_MMWAVE_ROOM=living`,
  `pip install -e backend[mmwave]`, restart, and targets appear on the radar. No
  ESP32, no firmware.
- **Tier R1 — a remote room (+€6-9 per room):** LD2450 plus a cheap ESP32. The
  TCP/MQTT transport is a new `frames` generator; the class and the parser do
  not change, because that seam already exists.
- **Tier R2 — the CSI experiment (~€25):** two ESP32-S3 boards. When the CSI
  frames carry pose/targets, `normalize_ruview` already accepts them. Research,
  not a deliverable.
- **Tier R3 — posture from cameras you already have (€0):** an RTSP camera,
  `pip install -e backend[camera]` (~5GB, torch with CUDA) and `pose=True` at
  camera bring-up gives sitting/standing/lying on the radar — without x/y
  position, since the homography is a follow-up.

**Calibration, stated honestly:** the LD2450's x/y are in the SENSOR's frame
(mounted on a wall, looking into the room). V1 assumes the sensor sits at the
origin corner looking along +y; per-room offset and rotation are a small
follow-up once the hardware is in place.

### Bring-up notes

- **Serial transport for the LD2450 — two known issues, deliberately deferred:**
  the serial flow has a race between close and read during shutdown (worst case,
  a frozen event loop on Windows), and the buffer is not persisted between
  frames. Both are known in the component's protocol. Bring-up **must** include
  cutting power during streaming, not only the happy read path, so the freeze
  surfaces before this reaches anything that matters.
- **Sign-magnitude decode:** the decoding convention follows the ESPHome
  `ld2450` component (x/y/vx/vy). **Confirm against the real device** that the
  bit interpretation is right, especially for negative values and at quadrant
  boundaries.

### Privacy

**Targets (x/y position) are LIVE-ONLY by decision:** they flow over the
WebSocket to the dashboard and are never persisted to SQLite nor published to
MQTT. A movement history on disk would be a privacy liability Wavr refuses by
design. Only occupancy confidence — occupied or not, per room — is stored.

---

## Phase 2 — A dedicated appliance, when you want one

Migration is changing where it runs, not what it is.

- **Hardware:** a small board with an embedded GPU (a Jetson Orin Nano class
  device, roughly $250-500) removes the conflict of the GPU living in a machine
  you use for other things. A mini-PC with a GPU works equally well. Either runs
  the same image as Phase 1.
- **Network:** a dedicated VLAN for the cameras and the Wavr box; egress
  firewalled; the dashboard reachable only from the trusted LAN. A compromised
  workstation then cannot reach the cameras at all.
- **Always on:** `restart: unless-stopped`, so the dashboard is a bookmark with
  no start step.
- **The whole migration:** flash the box, install Docker and the NVIDIA toolkit,
  copy `.env`, `docker compose up -d`. Because everything is configuration,
  **no line of code changes.**

---

## Why this gives you expansion without pain

| To add… | Cost, given the design | Touches the core? |
|---|---|---|
| A third or fourth camera | add it in the dashboard's Cameras section, persisted to SQLite | No |
| A new modality (BLE, mmWave) | one `SensorSource` class plus registration | No — the seam exists |
| A different box | `pull` + `.env` + `docker run` | No |
| Network segmentation | router/VLAN configuration | No — the app's guards travel |
| Different sensitivity or weights | `.env` (`WAVR_*`) | No |
| The dashboard as a native app | Tauri around the same HTML | No |

No expansion requires rewriting the core, because the core — fusion plus
sources — is agnostic about where it runs and how many sources it has. That is
what the common-source, config-driven architecture was for.

## Recommended order

1. **Now:** Phase 0 — harden the workstation, and the three code prerequisites.
2. **Next:** Phase 1 — Docker, which unlocks portability. From there, expanding
   is trivial.
3. **When budget or usage asks for it:** Phase 2 — dedicated box and VLAN.
