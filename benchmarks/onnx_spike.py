"""Phase F spike: can person detection drop torch/ultralytics for onnxruntime?

    python onnx_spike.py <workdir> <yolov8n.pt>      (the onnx-spike venv)

Steps, each printed as JSON at the end:
  1. export yolov8n.pt -> yolov8n.onnx with ultralytics (offline, analytics off)
  2. a test set: ultralytics' bundled images + deterministic variants + crops
  3. torch path = Wavr's yolo_detect semantics (class 0, conf floor 0.01,
     operator threshold) vs ORT path = own letterbox + decode + NMS in numpy
  4. parity per image at thresholds 0.25 / 0.4 (Wavr default) / 0.6
  5. latency per frame (median), cold start (fresh process: import + load +
     first inference), peak RSS, installed footprint of each stack
  6. egress: fresh process per path under a Python audit hook (socket.*) plus
     psutil sampling of the process's own connections (native code included,
     sampled every 20 ms -- a lower bound)
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

os.environ.update({"YOLO_OFFLINE": "1", "YOLO_AUTOINSTALL": "False", "ORT_DISABLE_TELEMETRY": "1"})

WORK = Path(sys.argv[1])
PT = Path(sys.argv[2])
THRESHOLDS = (0.25, 0.4, 0.6)


# -- the ORT path, as Wavr would ship it (numpy + onnxruntime + cv2 only) ------
ORT_PATH_SRC = r'''
import numpy as np, cv2, onnxruntime as ort

def letterbox(img, size=640, stride=32):
    # ultralytics LetterBox(auto=True): the minimum rectangle, padded to the
    # stride -- what its predictor uses for a .pt or a dynamic ONNX model.
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
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.s = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.name = self.s.get_inputs()[0].name

    def persons(self, frame, floor=0.01):
        out = self.s.run(None, {self.name: letterbox(frame)})[0][0]   # (84, 8400)
        cls = out[4:]
        best = cls.argmax(0)
        conf = cls.max(0)
        # ultralytics NMS is class-aware over all classes; a person box competes
        # only with other person boxes, so keep class-0 candidates, then NMS.
        m = (best == 0) & (conf >= floor)
        if not m.any():
            return []
        cx, cy, w, h = out[0, m], out[1, m], out[2, m], out[3, m]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
        sc = conf[m]
        return sorted((float(sc[i]) for i in nms(boxes, sc)), reverse=True)
'''


def images():
    import cv2
    import numpy as np
    import ultralytics
    assets = Path(ultralytics.__file__).parent / "assets"
    base = {p.stem: cv2.imread(str(p)) for p in sorted(assets.glob("*.jpg"))}
    out = {}
    for name, img in base.items():
        h, w = img.shape[:2]
        out[name] = img
        out[name + "-flip"] = cv2.flip(img, 1)
        out[name + "-half"] = cv2.resize(img, (w // 2, h // 2))
        out[name + "-dark"] = (img * 0.45).astype(np.uint8)
        out[name + "-bright"] = np.clip(img.astype(np.int16) + 60, 0, 255).astype(np.uint8)
        out[name + "-noise"] = np.clip(img.astype(np.int16) + np.random.default_rng(1).integers(-25, 26, img.shape), 0, 255).astype(np.uint8)
        for s in (0.35, 0.75, 1.25):
            out[f"{name}-scale{s}"] = cv2.resize(img, (round(w * s), round(h * s)))
        out[name + "-blur"] = cv2.GaussianBlur(img, (7, 7), 0)
        out[name + "-jpeg30"] = cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])[1], 1)
        out[name + "-grey"] = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        for ang in (-8, 8):
            m = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
            out[f"{name}-rot{ang}"] = cv2.warpAffine(img, m, (w, h), borderValue=(114, 114, 114))
        for i, (y0, y1, x0, x1) in enumerate([(0, h // 2, 0, w // 2), (0, h // 2, w // 2, w),
                                             (h // 2, h, 0, w // 2), (h // 2, h, w // 2, w),
                                             (h // 4, 3 * h // 4, w // 4, 3 * w // 4)]):
            out[f"{name}-crop{i}"] = np.ascontiguousarray(img[y0:y1, x0:x1])
    rng = np.random.default_rng(7)
    out["neg-noise"] = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    out["neg-grey"] = np.full((480, 640, 3), 128, np.uint8)
    out["neg-gradient"] = np.tile(np.linspace(0, 255, 640, dtype=np.uint8)[None, :, None], (480, 1, 3))
    return out


def torch_persons(model, frame, floor=0.01):
    r = model(frame, verbose=False, conf=floor)
    ps = []
    for res in r:
        for c, cf in zip(list(res.boxes.cls), list(res.boxes.conf)):
            if int(c) == 0:
                ps.append(float(cf))
    return sorted(ps, reverse=True)


def footprint(dists):
    from importlib import metadata
    total = 0
    seen = set()
    todo = list(dists)
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for f in d.files or []:
            try:
                total += (d.locate_file(f)).stat().st_size
            except OSError:
                pass
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            n = req.split(";")[0].split("[")[0]
            for sep in "<>=!~ (":
                n = n.split(sep)[0]
            if n.strip():
                todo.append(n.strip())
    return round(total / 2**20, 1), sorted(seen)


CHILD = r'''
import sys, os, time, json, threading
os.environ.update({"YOLO_OFFLINE": "1", "YOLO_AUTOINSTALL": "False", "ORT_DISABLE_TELEMETRY": "1"})
events = []
def hook(ev, args):
    if ev.startswith(("socket.", "urllib.", "http.")) or ev in ("socket.getaddrinfo",):
        events.append([ev, repr(args)[:160]])
sys.addaudithook(hook)
t0 = time.perf_counter()
mode, model = sys.argv[1], sys.argv[2]
import cv2, numpy as np
img = cv2.imread(sys.argv[3])
if mode == "torch":
    from ultralytics import YOLO
    from ultralytics.utils import events as e; e.events.enabled = False
    m = YOLO(model)
    run = lambda: m(img, verbose=False, conf=0.01)
else:
    exec(open(sys.argv[4]).read())
    import onnxruntime as ort
    try: ort.disable_telemetry_events()
    except Exception: pass
    d = OrtDetector(model)
    run = lambda: d.persons(img)
run()
cold = time.perf_counter() - t0
lat = []
for _ in range(40):
    t = time.perf_counter(); run(); lat.append(time.perf_counter() - t)
import psutil
p = psutil.Process()
print(json.dumps({"cold_s": round(cold, 3), "p50_ms": round(sorted(lat)[20] * 1000, 1),
                  "rss_mb": round(p.memory_info().rss / 2**20, 1),
                  "threads": p.num_threads(), "audit_events": events}))
'''


def child(mode, model, img_path, ort_src):
    import psutil
    script = WORK / "child.py"
    script.write_text(CHILD)
    p = psutil.Popen([sys.executable, str(script), mode, str(model), str(img_path), str(ort_src)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    conns = set()
    while p.poll() is None:
        # The whole tree: a venv's python.exe on Windows is a launcher stub,
        # and the interpreter doing the work is its child.
        try:
            procs = [p, *p.children(recursive=True)]
        except psutil.Error:
            procs = [p]
        for q in procs:
            try:
                for c in q.net_connections(kind="inet"):
                    conns.add((c.type.name if hasattr(c.type, "name") else str(c.type),
                               f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-", c.status))
            except psutil.Error:
                pass
        time.sleep(0.02)
    out, err = p.communicate()
    res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {"error": err[-800:]}
    res["sampled_connections"] = sorted(conns)
    return res


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    from ultralytics import YOLO
    from ultralytics.utils import events as e
    e.events.enabled = False
    import cv2
    model = YOLO(str(PT))
    onnx_path = Path(model.export(format="onnx", imgsz=640, simplify=True, dynamic=True))
    ort_src = WORK / "ort_path.py"
    ort_src.write_text(ORT_PATH_SRC)
    ns: dict = {}
    exec(ORT_PATH_SRC, ns)
    det = ns["OrtDetector"](str(onnx_path))

    imgs = images()
    # Export fidelity on its own: the SAME preprocessed tensor into both runtimes.
    import torch
    tm = model.model.float().eval()
    raw_max = 0.0
    for img in imgs.values():
        x = ns["letterbox"](img)
        with torch.no_grad():
            y = tm(torch.from_numpy(x))
        y = (y[0] if isinstance(y, (list, tuple)) else y).numpy()
        z = det.s.run(None, {det.name: x})[0]
        raw_max = max(raw_max, float(abs(y - z).max()))
    per = {}
    agree = {t: 0 for t in THRESHOLDS}
    maxdiff = 0.0
    for name, img in imgs.items():
        a = torch_persons(model, img)
        b = det.persons(img)
        row = {}
        for t in THRESHOLDS:
            ca, cb = sum(x >= t for x in a), sum(x >= t for x in b)
            row[str(t)] = [ca, cb]
            agree[t] += ca == cb
        top = [round(a[0], 3) if a else 0.0, round(b[0], 3) if b else 0.0]
        maxdiff = max(maxdiff, abs(top[0] - top[1]))
        row["top_conf"] = top
        per[name] = row

    sample = WORK / "sample.jpg"
    cv2.imwrite(str(sample), imgs[next(iter(imgs))])
    res = {
        "onnx_bytes": onnx_path.stat().st_size,
        "pt_bytes": PT.stat().st_size,
        "images": len(imgs),
        "raw_output_max_abs_diff": raw_max,
        "count_agreement": {str(t): f"{agree[t]}/{len(imgs)}" for t in THRESHOLDS},
        "max_top_conf_abs_diff": round(maxdiff, 4),
        "per_image": per,
        "torch_run": child("torch", PT, sample, ort_src),
        "ort_run": child("ort", onnx_path, sample, ort_src),
        "footprint_mb": {
            "torch_stack": footprint(["ultralytics", "torch", "torchvision", "opencv-python"])[0],
            "ort_stack": footprint(["onnxruntime", "numpy", "opencv-python-headless"])[0],
        },
    }
    import onnxruntime, torch, ultralytics
    res["versions"] = {"onnxruntime": onnxruntime.__version__, "torch": torch.__version__,
                       "ultralytics": ultralytics.__version__}
    (WORK / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "per_image"}, indent=1))


if __name__ == "__main__":
    main()
