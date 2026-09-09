# Tapo camera bring-up (C210 / TC40)

Step by step for adding a pair of RTSP cameras as Wavr sources. Everything stays LOCAL:
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
| cam_bedroom | bedroom | `rtsp://<user>:<pass>@<C210_ip>:554/stream2`   |
| cam_yard    | yard    | `rtsp://<user>:<pass>@<TC40_ip>:554/stream2`   |

(The dashboard masks the password when it lists cameras back. `rtsp_url` must start with
`rtsp://` / `rtsps://` — other schemes are rejected, an SSRF guard added in the audit.)

## 7. Toggle ON → verify
- Toggle a camera **ON** in the dashboard: Wavr connects the RTSP stream (off the event
  loop, so a bad camera can't freeze the backend) and runs YOLO. Walk into the room → the
  room card + radar should read **occupied** with the camera as a contributing source
  (check per-source health). Toggle **OFF** = hard RTSP kill + VRAM released once the last
  camera stops. Closing Wavr returns all of the VRAM to the rest of the machine.

## Per-camera notes
- **C210:** standard indoor cam, continuous RTSP + ONVIF — the continuous `CameraSource`
  model fits it directly. A good first device; start there.
- **TC40:** **mains-powered (plugged into the wall)**, so it holds a continuous RTSP stream
  like the C210, with none of the battery clip-push behaviour. Same setup: enable its RTSP
  camera account and add it. Both are solid continuous sources.

## Config note
- `WAVR_FUSION_THRESHOLD=0.35` is a defensible setting for a network-only phase, since
  network alone caps at ~0.4. With a camera contributing real confidence, move it back
  toward the default `0.5` so a single weak signal doesn't over-report occupied.
