# Person detection without torch (`camera-lite`)

The camera source counts people in each frame to decide whether a room is occupied. It
has two detectors:

- **`camera` extra (default):** Ultralytics YOLOv8n on torch. It downloads its weights the
  first time it runs, because they are not vendored in this repository.
- **`camera-lite` extra:** ONNX Runtime running a local `yolov8n.onnx`. It never
  downloads a model.

Install one of the two extras, not both: they bring different OpenCV packages
(`opencv-python` and `opencv-python-headless`).

## Getting the model

Put `yolov8n.onnx` next to the Wavr database, or point `WAVR_PERSON_MODEL` at it.

- **From a release:** releases from 0.5.0 attach the exact `yolov8n.onnx` whose SHA-256 is
  pinned in `backend/wavr/models/person-detector.json`.
- **Exported locally:** `python scripts/provision_person_model.py --pt <local-yolov8n.pt>`
  converts a `yolov8n.pt` you already have. It needs `ultralytics` installed once, blocks
  network access while it runs, and writes a SHA-256 sidecar (`yolov8n.onnx.json`) next to
  the model.

## Verification

- A model with a sidecar is checked against the sidecar.
- A model without a sidecar is checked against the hash pinned in the manifest.
- A model that fails the check is never loaded. Detection falls back to the torch path
  and the reason is logged once; the same file is not re-hashed on every frame. With
  `camera-lite` alone there is no torch path, so nothing is detected until a valid model is
  in place.

## Privacy

Frames stay in memory, as with the torch path. Official ONNX Runtime builds ship with
telemetry on by default (Windows ETW on Windows; the 1DS SDK over HTTPS on Linux, macOS,
Android and iOS). Wavr sets `ORT_DISABLE_TELEMETRY=1` before ONNX Runtime is imported, which
disables the uploader outside Windows, and calls `onnxruntime.disable_telemetry_events()`
before creating a session. ONNX Runtime documents that a minimal initialization event may be
emitted before that call. Network egress of this path has been measured on Windows only.

## Measured

On one machine: start-up and memory from an interleaved A/B
(`benchmarks/results/2026-09-24-onnx-ab.json`); installed stack sizes from
`benchmarks/results/2026-09-24-onnx-spike.json`:

| | torch path | ONNX path |
|---|---|---|
| Cold start (median) | 5.2 s | 0.7 s |
| Resident memory after warm-up (median) | ~400 MB | ~125 MB |
| Installed dependency stack | ~1057 MB | ~213 MB |

On 41 test images, both paths reported the same person counts at thresholds 0.25, 0.4 and
0.6 (`benchmarks/results/2026-09-24-onnx-spike.json`). The images are synthetic variants of
two scenes plus three negatives; this is a parity check, not an accuracy evaluation.

## Model provenance and license

Ultralytics YOLOv8n, exported with `format=onnx`, `imgsz=640`, `simplify=True`,
`dynamic=True`; 13,309,063 bytes. The weights are licensed AGPL-3.0, like Wavr.

## Not covered

Posture (pose estimation) still uses the torch path.
