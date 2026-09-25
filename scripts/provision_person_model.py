"""Export a locally supplied yolov8n.pt into Wavr's optional ONNX model path.

Usage: python scripts/provision_person_model.py --pt path/to/yolov8n.pt
The input must already exist. This command never downloads weights or packages.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from wavr.person_onnx import OrtDetector, model_path, verify_model  # noqa: E402
from wavr.sources.camera import import_yolo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pt", required=True, type=Path, help="existing local yolov8n.pt")
    args = parser.parse_args()
    pt = args.pt.resolve()
    if not pt.is_file():
        parser.error(f"local weights not found: {pt}; obtain them separately before exporting")
    if pt.name != "yolov8n.pt":
        parser.error("expected a local file named yolov8n.pt")

    # Fail closed even if a future ultralytics version ignores its offline flags.
    def no_network(*_args, **_kwargs):
        raise RuntimeError("model export attempted network access; install prerequisites offline")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.socket.sendto = no_network
    socket.create_connection = no_network
    socket.getaddrinfo = no_network
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    import numpy as np
    dest = model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    model = import_yolo()(str(pt))
    exported = Path(model.export(format="onnx", imgsz=640, simplify=True, dynamic=True))
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".onnx", delete=False) as tmp:
        staged = Path(tmp.name)
    try:
        shutil.copyfile(exported, staged)
        detector = OrtDetector(staged)
        output = detector.s.run(None, {detector.name: np.zeros((1, 3, 640, 640), dtype=np.float32)})[0]
        if output.shape != (1, 84, 8400):
            raise RuntimeError(f"unexpected YOLOv8n output shape: {output.shape}")
        # Validate against a pinned release hash before replacing any installed
        # model; the staging file has a random basename.
        digest = verify_model(staged, expected_filename=dest.name)
        staged.replace(dest)
        sidecar = {
            "sha256": digest,
            "source": pt.name,
            "license": "AGPL-3.0 (Ultralytics)",
            "export": {"format": "onnx", "imgsz": 640, "simplify": True, "dynamic": True},
            "size_bytes": dest.stat().st_size,
        }
        dest.with_suffix(dest.suffix + ".json").write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    finally:
        staged.unlink(missing_ok=True)
    print(f"Verified local person model: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
