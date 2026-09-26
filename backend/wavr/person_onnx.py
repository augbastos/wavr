"""Local, optional YOLOv8 person detection. No model is fetched at runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


MANIFEST = Path(__file__).parent / "models" / "person-detector.json"


class PersonModelError(RuntimeError):
    """A provisioned model failed integrity or shape validation."""


def model_path() -> Path:
    """Resolve beside WAVR_DB, as other per-Core data does."""
    db = os.getenv("WAVR_DB", "wavr.db")
    data_dir = Path(db).resolve().parent if db != ":memory:" else Path.cwd()
    return Path(os.getenv("WAVR_PERSON_MODEL", str(data_dir / "yolov8n.onnx")))


def verify_model(path: Path, expected_filename: str | None = None) -> str:
    """Check the model against the hash that vouches for it.

    A local export (option B) carries a sidecar written by
    scripts/provision_person_model.py: it is checked against THAT, because a
    local export is not byte-identical to the release's copy. A copy without a
    sidecar (option A, shipped with a release) is checked against the release
    manifest's hash when one is pinned. Applying the manifest hash to every file
    of that name would refuse every legitimate local export once A is pinned."""
    path = Path(path)
    if not path.is_file():
        raise PersonModelError("person model not provisioned: run python scripts/provision_person_model.py --pt <local-yolov8n.pt>")
    sidecar = path.with_suffix(path.suffix + ".json")
    if sidecar.is_file():
        expected = json.loads(sidecar.read_text(encoding="utf-8"))["sha256"]
    else:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        named = (expected_filename or path.name) == manifest["filename"]
        expected = manifest["sha256"] if named else None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and expected != digest:
        raise PersonModelError("person model SHA-256 mismatch; re-provision the local ONNX model")
    return digest


def letterbox(img, size=640, stride=32):
    """Ultralytics auto=True: minimum stride-aligned rectangle, not a square."""
    import cv2
    import numpy as np

    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = round(w * r), round(h * r)
    dw, dh = ((size - nw) % stride) / 2, ((size - nh) % stride) / 2
    if (w, h) != (nw, nh):
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT,
                             value=(114, 114, 114))
    x = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x)


def nms(boxes, scores, iou=0.7):
    import numpy as np

    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0]); yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2]); yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        a = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        b = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (boxes[order[1:], 3] - boxes[order[1:], 1])
        order = order[1:][inter / (a + b - inter + 1e-9) <= iou]
    return keep


class OrtDetector:
    def __init__(self, path):
        verify_model(Path(path))
        # Official ORT builds ship telemetry ON. The variable stops the 1DS
        # uploader off Windows, if set before ORT initializes; the call
        # suppresses non-essential events on every platform (ETW on Windows).
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import onnxruntime as ort

        ort.disable_telemetry_events()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.s = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self.name = self.s.get_inputs()[0].name

    def persons(self, frame, floor=0.01):
        import numpy as np

        out = self.s.run(None, {self.name: letterbox(frame)})[0][0]
        if out.ndim != 2 or out.shape[0] != 84:
            raise PersonModelError("person model output must have 84 channels")
        cls = out[4:]
        best = cls.argmax(0)
        conf = cls.max(0)
        m = (best == 0) & (conf >= floor)
        if not m.any():
            return []
        cx, cy, w, h = out[0, m], out[1, m], out[2, m], out[3, m]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
        sc = conf[m]
        return sorted((float(sc[i]) for i in nms(boxes, sc)), reverse=True)
