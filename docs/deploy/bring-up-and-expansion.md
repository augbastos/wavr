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
2. **Run without admin — with one exception worth knowing before Phase 2.**
   `arp -a`, `ping`, OpenCV/RTSP and YOLO all run as an ordinary user, and a
   dedicated account separate from your everyday one is better still. The
   exception is the raw DHCP collector: `AF_PACKET`/`SOCK_RAW` needs `CAP_NET_RAW`
   on Linux, and the fallback binds UDP/67, a privileged port. On the Linux
   appliance of Phase 2 that is a capability to grant deliberately
   (`CAP_NET_RAW`), not a reason to run the whole Core as root.
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

**Turning the last camera off already releases the model.** `CameraSource`
counts running loops, and when the count reaches zero it calls `release_model()`
— which drops `_YOLO_MODEL` and `_POSE_MODEL` and calls
`torch.cuda.empty_cache()` (`backend/wavr/sources/camera.py`). You do not have
to close the program to get the memory back.

*What closing the program additionally recovers:* the **CUDA context** itself,
a few hundred MB the driver holds for as long as the process lives, whether or
not a model is loaded. Nothing short of exiting releases that. So on a machine
shared with anything else that wants the GPU, closing Wavr is still the complete
answer; toggling the cameras off is the large part of it.

**The code prerequisites this section used to list are done.** They are kept
here as a record of what "pointing this at a real camera" actually required,
each with where it landed — a document that still asks for finished work teaches
its reader to stop believing it:

- **keep-alive in `CameraSource`**, so a transient error does not kill the
  camera. Done: it reconnects after `reconnect_delay` and reports health while
  it retries (`sources/camera.py`).
- **a dead source must never report `active=True`**, because the ON/OFF
  indicator is a safety control. Done, and more strongly than asked: the
  `SourceManager` now *supervises* rather than merely reaps — a failed source is
  restarted with backoff and the health behind the restart is published
  (`sourcemanager.py`).
- **a lock around the lazy model load**, so two concurrent first-detections
  cannot load YOLO twice. Done: `_MODEL_LOCK`, with double-checked locking
  (`sources/camera.py`).
- **validation that `cam_interval > 0`.** Done: `WAVR_CAM_INTERVAL` is clamped
  to a floor and a bad value is logged rather than obeyed (`config.py`). Zero
  turned the detection loop's sleep into a busy loop, which presents as "the
  machine got slow" and never as a bad setting.

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

Docker's own documentation supports host networking on Docker Desktop 4.34 and
later, with a stated limit: it works at layer 4, and protocols below TCP/UDP are
not supported. So "unsupported" would be wrong; "not the same thing as on Linux"
is the accurate reading, and this repository has **not tested** a `127.0.0.1`
bind inside a host-networked container on Docker Desktop.

Until somebody does, **on Windows keep running `uvicorn` directly** (the Phase 0
way). Docker is the appliance/Linux path; elsewhere treat it as a check that the
image builds, not as the way to run the service.

### GPU/camera variant (follow-up)

The base image is lean and therefore covers network and CSI presence but **not**
camera detection. For a real camera: build a variant that installs
`pip install -e backend[camera]` on top of an `nvidia/cuda` base (torch with CUDA
support), uncomment the `deploy.resources.reservations.devices` stanza in
`docker-compose.yml`, and install `nvidia-container-toolkit` on the host. The
image is considerably larger; treat it as a separate deliverable.

### Secrets

`.env` is never copied into the image. `env_file` in the compose file reads it
**on the host at start-up** and injects the values as environment variables —
nothing is mounted — and `.dockerignore` excludes it explicitly from the build
context, alongside `.venv`, `*.db` and `.git`. No credential ends up in any image
layer.

After this phase, workstation and appliance run **the same image** — the
difference is the `.env` and the network.

---

## Position radar — hardware

Extending from presence to position (x/y) and posture, with minimal hardware
that works from plain Python.

> **On the prices below.** They were rough marketplace figures noted in 2026-07
> and are deliberately vague, because a precise number with no date and no source
> becomes a false statement on its own. The manufacturer's own listing for the
> LD2450 is lower than the range given here. Treat these as "this is a cheap tier
> or an expensive one", check the current price yourself, and do not quote them.

- **Tier R0 — one room, no soldering (tens of euros):** one HLK-LD2450, a
  CP2102/CH340 USB-TTL adapter and four female-female jumpers (5V, GND, TX, RX —
  note the LD2450 runs its UART at 256000 baud). The module speaks **UART/TTL**,
  not USB, which is what the adapter is for. It then plugs into the machine: `WAVR_MMWAVE_PORT=COM3`, `WAVR_MMWAVE_ROOM=living`,
  `pip install -e backend[mmwave]`, restart, and targets appear on the radar. No
  ESP32, no firmware.
- **Tier R1 — a remote room (a few euros more per room):** LD2450 plus a cheap ESP32. The
  TCP/MQTT transport is a new `frames` generator; the class and the parser do
  not change, because that seam already exists.
- **Tier R2 — the CSI experiment:** two ESP32-S3 boards. When the CSI
  frames carry pose/targets, `normalize_ruview` already accepts them. Research,
  not a deliverable.
- **Tier R3 — posture from cameras you already have (no new hardware):** an RTSP
  camera, `pip install -e backend[camera]` (several GB — it pulls torch) and
  `pose=True` at
  camera bring-up gives sitting/standing/lying on the radar — without x/y
  position, since the homography is a follow-up.

**Calibration, stated honestly:** the LD2450's x/y are in the SENSOR's frame
(mounted on a wall, looking into the room). V1 assumes the sensor sits at the
origin corner looking along +y; per-room offset and rotation are a small
follow-up once the hardware is in place.

### Bring-up notes

- **Serial transport for the LD2450 — one issue left, and it needs the real
  device to close.** The frame buffer *is* persisted between reads
  (`sources/mmwave.py` carries `leftover` across iterations), and the shutdown
  path closes the port off the event loop (`asyncio.to_thread(s.close)`) so a
  blocking close cannot freeze the loop. What remains is the race itself: a
  `read` may still be in flight when the close happens, and no amount of reading
  the code settles what the driver does then. Bring-up **must** therefore include
  cutting power mid-stream, not only the happy read path.
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

- **Hardware:** a small board with an embedded GPU — a Jetson Orin Nano class
  device — removes the conflict of the GPU living in a machine you use for other
  things. NVIDIA's product pages no longer list a price, and the last figure
  they published was a 2024 announcement; look it up rather than trusting a
  number in this file. A mini-PC with a GPU works equally well. Either runs
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
