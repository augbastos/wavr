# Tapo camera bring-up (C210, and a warning about the TC40)

> **Check your model against TP-Link's own list first.** TP-Link publishes which
> Tapo cameras expose RTSP/ONVIF. The **C210 is on it. The TC40 is not** — and
> that is not a whole series being absent, because other TC-series models are
> listed. This document previously presented the TC40 as a working continuous
> RTSP source; that could not be supported from any primary source, so it is
> stated as an unknown here instead. If you have one, confirm RTSP on the device
> before planning around it.

Step by step for adding RTSP cameras as Wavr sources. Everything stays LOCAL:
frames are never stored and never leave the box; cameras always start OFF, and toggling one
off is a hard RTSP kill-switch.

## 0. What the camera gives Wavr (honest scope)
- **Now:** room-level **presence** + **posture** (YOLO person / pose on the GPU). It
  confirms "someone is in the room", not *where* in the room — per-person x/y from a
  camera needs homography (future spec A). So on the radar/3D the camera contributes the
  room's occupancy/confidence, and people-markers stay room-centred until homography lands.
- Frames are consumed in RAM and never written to disk or sent anywhere.

## 1. Get an RTSP account on each camera (in the Tapo app)
Tapo cameras don't use your Tapo login for RTSP — you create a separate "camera account":
1. Tapo app → the camera → **Settings** → **Advanced Settings** → **Camera Account** (some
   firmwares: "Device Account" / "Third-Party Compatibility" / RTSP).
2. Set a **username + password** (write them down). This is what goes in the RTSP URL.

## 2. Find each camera's IP (and pin it)
- Router admin → DHCP client list, or Tapo app → camera → device info shows the IP.
- **Set a DHCP reservation** (static IP) for each camera in the router so the URL never
  changes. (Once Wavr's network scan runs, `/api/inventory` also lists them.)

## 3. Build the RTSP URL
Tapo RTSP path:
```
rtsp://<user>:<pass>@<camera_ip>:554/stream1     # HD main stream
rtsp://<user>:<pass>@<camera_ip>:554/stream2     # SD sub-stream (lower res)
```
**Use `stream2` (SD) for detection** — it's plenty for person/pose YOLO and much lighter on
the GPU + network than the HD `stream1`. Shape of the result:
`rtsp://wavr:<password>@192.168.1.60:554/stream2`

## 4. Install the camera extra (on the machine running the Core; this is the GPU path)
```powershell
# from the repository root
.venv\Scripts\pip install -e backend[camera]     # opencv-python + ultralytics (pulls torch; several GB)
```
This is the only heavy install. YOLO occupies the GPU only while a camera is ON.

## 5. Start Wavr (loopback) and open the dashboard
```powershell
# from the repository root
.venv\Scripts\python -m wavr.serve        # http://127.0.0.1:8000  (or scripts\wavr-desktop.ps1)
```

## 6. Add each camera in the dashboard (Cameras section)
For each camera fill: **name**, **room**, **rtsp_url**, **confidence** (default 0.4). They
register **boot-OFF** (safety). For example:

| name        | room    | rtsp_url                                       |
|-------------|---------|------------------------------------------------|
| cam_bedroom | bedroom | `rtsp://<user>:<pass>@<camera_ip>:554/stream2` |
| cam_yard    | yard    | `rtsp://<user>:<pass>@<camera_ip>:554/stream2` |

(The dashboard masks the password when it lists cameras back. `rtsp_url` must start with
`rtsp://` / `rtsps://` — other schemes are rejected, an SSRF guard added in the audit.)

## 7. Toggle ON → verify
- Toggle a camera **ON** in the dashboard: Wavr connects the RTSP stream (off the event
  loop, so a bad camera can't freeze the backend) and runs YOLO. Walk into the room → the
  room card + radar should read **occupied** with the camera as a contributing source
  (check per-source health). Toggle **OFF** = hard RTSP kill + VRAM released once the last
  camera stops. Closing Wavr returns all of the VRAM to the rest of the machine.

## Per-camera notes
- **C210:** appears on TP-Link's published RTSP/ONVIF list. Standard indoor cam,
  continuous stream — the continuous `CameraSource` model fits it directly. A good
  first device; start there.
- **TC40:** **not on that list**, and no TP-Link product page for it was reachable
  while writing this. An earlier version of this file asserted that it is
  mains-powered and therefore holds a continuous RTSP stream like the C210. Neither
  half of that was verified. If RTSP does work on your unit the setup is identical;
  if it does not, no amount of configuration in Wavr will help, because the camera
  never offers the stream.

## Config note
- `WAVR_FUSION_THRESHOLD=0.35` is a defensible setting for a network-only phase.
  The ~0.4 ceiling for network alone is arithmetic, not a measurement — trust
  weight times source confidence, the same 0.5 x 0.8 the fusion tests work
  through in `backend/tests/test_fusion.py`. With a camera contributing real confidence, move it back
  toward the default `0.5` so a single weak signal doesn't over-report occupied.
