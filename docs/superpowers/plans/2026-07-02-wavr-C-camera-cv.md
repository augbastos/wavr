# Wavr Sub-plan C — Camera + local CV (RTSP + YOLO) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `CameraSource` (RTSP frame pull + local YOLO person detection) as the highest-precision `SensorSource`, wired for the two Tapo cameras (C210→quarto, TC40→quintal), booting **OFF** by default with a hard server-side kill-switch, and releasing the RTSP stream deterministically on disable — all fully unit-tested with fakes (no camera, no GPU, no torch/opencv installed to build or test).

**Architecture:** `CameraSource` implements the existing `SensorSource` Protocol. Both heavy dependencies (OpenCV RTSP capture, Ultralytics YOLO) sit behind **injectable seams** (`frames=` async-iterator of raw frames, `detect=` sync frame→detection callable) whose real defaults **lazy-import** cv2/ultralytics only on the real path. Tests inject fakes → zero heavy deps exercised. The blocking RTSP read runs in a thread (`asyncio.to_thread`) so it never blocks the event loop, and the frame generator's `finally` releases the capture, so `SourceManager`'s `agen.aclose()` on disable performs a real kill. Cameras register `enabled=False` (boot-OFF safety). Only derived `RoomState`/events persist — never a frame.

**Tech Stack:** Python 3.11+, asyncio, `opencv-python` + `ultralytics` (OPTIONAL extra, NOT installed by default — lazy-imported on the real path only), stdlib threading via `asyncio.to_thread`.

## Global Constraints

- **Platform:** Windows 11, PowerShell. Venv at `C:\IA\wavr\.venv`. Python interpreter: `C:\IA\wavr\.venv\Scripts\python.exe`. Run all commands from `C:\IA\wavr`.
- **Python:** 3.11+.
- **Canonical event shape — EXACT (8 fields, no extras):** `{"room": str, "modality": str, "presence": bool, "motion": float, "breathing_bpm": float|None, "heart_bpm": float|None, "confidence": float, "ts": str}`; `ts` = `datetime.now(timezone.utc).isoformat()` (ends `+00:00`). `modality` ∈ `{"wifi_csi","network","camera","sim"}`. CameraSource emits `modality="camera"`, `motion=0.0`, `breathing_bpm=None`, `heart_bpm=None`. Person count / bounding boxes are NOT canonical fields — they stay internal; the event carries only `presence` + `confidence`. Do NOT add fields (it breaks the exact-shape contract).
- **`confidence` is the modality's OWN 0..1** (the detector's person-detection confidence), NOT the fusion weight. `fusion.py` already weights `camera` at 1.0 — **do not touch `fusion.py`**.
- **SAFETY — boot OFF (fail-safe):** cameras register with `enabled=False`. There is no persisted ON state; a camera is only ever enabled by a conscious runtime toggle (`POST /api/sources/{name}/toggle`, guarded by `X-Wavr-Local`). Never register a camera `enabled=True`.
- **SAFETY — hard kill on disable:** OFF means the `CameraSource` closes the RTSP capture and reads/processes NO frames. The frame generator MUST `release()` the capture in a `finally`, reached via `agen.aclose()` (which `SourceManager._kill` calls on cancel). No frame enters memory while OFF.
- **SAFETY — a stalled read must not outlive a disable:** the blocking `cap.read()` runs via `asyncio.to_thread` so cancellation returns control to the loop immediately; `SourceManager._kill` already guards with a 5s `wait_for`. Document that a truly wedged native read is abandoned to its thread while `release()` runs, rather than hanging the control plane.
- **PRIVACY — frames never leave the LAN, never persist:** only derived events/`RoomState` are stored (`storage.py` already enforces derived-only). Never write a frame to disk, DB, logs, or the public Plano B. Nothing in this sub-plan touches the frontend or Plano B.
- **Deps are an OPTIONAL extra:** add `opencv-python`/`ultralytics` under `[project.optional-dependencies] camera = [...]`, NOT to the default `dependencies`. Do NOT `pip install` them in this plan. Real imports are lazy (inside the default adapter functions) so the module loads and all tests run without them.
- **TDD discipline:** failing test → run, watch it fail for the right reason → minimal impl → run, watch pass → commit. Files < 500 lines. DRY, YAGNI.

**Repo root:** `C:\IA\wavr\` (git; Sub-plans A+B merged to `master`). Work on a new branch `sub-plan-c-camera-cv` off `master`.

**Existing interfaces this plan consumes (do not redefine):**
- `wavr.events.SensingEvent` — frozen dataclass, canonical shape above.
- `wavr.sources.base.SensorSource` — `Protocol`, `events(self) -> AsyncIterator[SensingEvent]`.
- `wavr.sourcemanager.SourceManager` — `register(name, factory, enabled=True)`; `_run` calls `agen.aclose()` on cancel; `set_enabled(name, bool)` spawns/kills at runtime; `status()` reports `{running, sources:[{name,enabled,active}]}`.
- `wavr.app._default_sources(cfg)` — returns `[(name, factory, enabled), ...]`; currently `network`(True), `ruview`(True), `sim`(False).
- `wavr.config.load_config() -> Config` — dataclass; add camera fields here.

---

### Task 1: `CameraSource` — injectable frames + detect, canonical event, deterministic release

**Files:**
- Create: `backend/wavr/sources/camera.py` (CameraSource + a `Detection` result type; NO real cv2/YOLO yet)
- Create: `backend/tests/test_camera_source.py`
- Modify: `backend/wavr/config.py` (camera config fields)
- Modify: `backend/tests/test_config.py` (assert new defaults)

**Interfaces:**
- Consumes: `SensingEvent`.
- Produces: `Detection` (a small dataclass: `count: int`, `confidence: float`). `CameraSource(room: str, rtsp_url: str = "", frames: Callable[[str], AsyncIterator[object]] | None = None, detect: Callable[[object], Detection] | None = None, interval: float = 0.5)` implementing `events() -> AsyncIterator[SensingEvent]`. For each frame from `frames(rtsp_url)`, runs `detect(frame)` **off the loop** via `asyncio.to_thread`, emits `modality="camera"`, `room=self.room`, `presence = det.count > 0`, `confidence = det.confidence if present else 0.0`, `motion=0.0`, vitals `None`. The `frames` generator's `finally` (release) is reached deterministically on `aclose()` (wrap iteration in `contextlib.aclosing`). Defaults for `frames`/`detect` are the real adapters from Task 2 — in THIS task they may be `None` and the class must require injection (raise a clear error if a default is needed before Task 2 wires it). To keep Task 1 self-contained, set the defaults to `None` and, in `events()`, assert both are provided (they always are, via Task 2 defaults or test injection).

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_camera_source.py`

```python
import pytest
from wavr.sources.camera import CameraSource, Detection

async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out

def _frames_factory(items, released):
    async def frames(url):
        try:
            for f in items:
                yield f
        finally:
            released["v"] = True  # release() proxy — proves deterministic teardown
    return frames

async def test_camera_present_when_person_detected():
    released = {"v": False}
    src = CameraSource("quarto", rtsp_url="rtsp://x",
                       frames=_frames_factory(["frameA"], released),
                       detect=lambda f: Detection(count=1, confidence=0.92))
    [ev] = await _first_n(src, 1)
    assert ev.room == "quarto"
    assert ev.modality == "camera"
    assert ev.presence is True
    assert ev.confidence == 0.92
    assert ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.ts.endswith("+00:00")

async def test_camera_absent_when_no_person():
    released = {"v": False}
    src = CameraSource("quintal", rtsp_url="rtsp://x",
                       frames=_frames_factory(["f"], released),
                       detect=lambda f: Detection(count=0, confidence=0.0))
    [ev] = await _first_n(src, 1)
    assert ev.presence is False
    assert ev.confidence == 0.0

async def test_camera_releases_capture_on_aclose():
    released = {"v": False}
    src = CameraSource("quarto", rtsp_url="rtsp://x",
                       frames=_frames_factory(["f1", "f2", "f3"], released),
                       detect=lambda f: Detection(count=1, confidence=0.5))
    agen = src.events()
    await agen.__anext__()          # pull one event, mid-stream
    await agen.aclose()             # disable → must run frames() finally
    assert released["v"] is True    # RTSP released deterministically, not GC-deferred
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.sources.camera'`.

- [ ] **Step 3: Write minimal implementation** — create `backend/wavr/sources/camera.py`

```python
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent


@dataclass(frozen=True)
class Detection:
    count: int
    confidence: float


class CameraSource:
    """Highest-precision source: pulls RTSP frames and runs local person detection.
    Both the frame stream and the detector are injected (real defaults lazy-import
    cv2/YOLO in Task 2). The frame generator releases the capture in its finally,
    so SourceManager.aclose() on disable is a hard RTSP kill — no frame is read or
    kept while OFF. Detection runs in a thread so a slow inference never blocks the
    loop. Only derived presence/confidence is emitted; frames never persist."""

    def __init__(self, room: str, rtsp_url: str = "",
                 frames: Callable[[str], AsyncIterator[object]] | None = None,
                 detect: Callable[[object], Detection] | None = None,
                 interval: float = 0.5):
        self.room = room
        self._url = rtsp_url
        self._frames = frames
        self._detect = detect
        self._interval = interval

    async def events(self) -> AsyncIterator[SensingEvent]:
        assert self._frames is not None and self._detect is not None, \
            "CameraSource requires frames + detect (real defaults wired in Task 2)"
        async with contextlib.aclosing(self._frames(self._url)) as stream:
            async for frame in stream:
                det = await asyncio.to_thread(self._detect, frame)
                present = det.count > 0
                yield SensingEvent(
                    room=self.room, modality="camera", presence=present,
                    motion=0.0, breathing_bpm=None, heart_bpm=None,
                    confidence=det.confidence if present else 0.0,
                    ts=datetime.now(timezone.utc).isoformat(),
                )
                if self._interval:
                    await asyncio.sleep(self._interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Add camera config fields** — modify `backend/wavr/config.py`

Add to the `Config` dataclass:

```python
    cam_quarto_url: str
    cam_quintal_url: str
    cam_interval: float
    cam_confidence: float
```

And in `load_config()`'s returned `Config(...)`:

```python
        cam_quarto_url=os.getenv("WAVR_CAM_QUARTO_URL", ""),
        cam_quintal_url=os.getenv("WAVR_CAM_QUINTAL_URL", ""),
        cam_interval=float(os.getenv("WAVR_CAM_INTERVAL", "0.5")),
        cam_confidence=float(os.getenv("WAVR_CAM_CONFIDENCE", "0.4")),
```

- [ ] **Step 6: Add config test** — modify `backend/tests/test_config.py`

```python
def test_config_has_camera_defaults(monkeypatch):
    for var in ("WAVR_CAM_QUARTO_URL", "WAVR_CAM_QUINTAL_URL",
                "WAVR_CAM_INTERVAL", "WAVR_CAM_CONFIDENCE"):
        monkeypatch.delenv(var, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.cam_quarto_url == "" and cfg.cam_quintal_url == ""
    assert cfg.cam_interval == 0.5
    assert cfg.cam_confidence == 0.4
```

- [ ] **Step 7: Run the affected tests + full suite**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py backend/tests/test_config.py -q`
Expected: PASS.
Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (Sub-plan B left 50; expect 50 + new).

- [ ] **Step 8: Commit**

```powershell
git add backend/wavr/sources/camera.py backend/tests/test_camera_source.py backend/wavr/config.py backend/tests/test_config.py
git commit -m "feat: CameraSource — injectable RTSP frames + detection, deterministic release"
```

---

### Task 2: Real RTSP + YOLO adapters (lazy-imported), wired as CameraSource defaults

**Files:**
- Modify: `backend/wavr/sources/camera.py` (add `rtsp_frames`, `yolo_detect`, monkeypatchable boundary helpers; make them the CameraSource defaults)
- Modify: `backend/tests/test_camera_source.py` (adapter tests via monkeypatched boundary — NO cv2/torch)
- Modify: `backend/pyproject.toml` (add `[project.optional-dependencies] camera`)

**Interfaces:**
- Consumes: `Detection`, `CameraSource` (Task 1).
- Produces: `async def rtsp_frames(url: str) -> AsyncIterator[object]` — opens a capture via `_open_capture(url)`, yields frames via `await asyncio.to_thread(_read, cap)` in a loop, `finally: _release(cap)`. `def yolo_detect(frame) -> Detection` — runs `_model()(frame)` and reduces to person `count` + max `confidence`. Boundary helpers `_open_capture(url)`, `_read(cap)`, `_release(cap)`, `_model()` isolate the lazy `import cv2` / `import ultralytics` so tests monkeypatch them without the deps installed. `CameraSource.__init__` defaults become `frames = frames or rtsp_frames`, `detect = detect or yolo_detect`.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_camera_source.py`

```python
async def test_rtsp_frames_reads_then_releases(monkeypatch):
    from wavr.sources import camera
    opened = {"cap": None, "released": False}
    def fake_open(url):
        opened["cap"] = f"cap:{url}"
        return opened["cap"]
    reads = iter([(True, "frame1"), (True, "frame2"), (False, None)])  # (ok, frame); (False,_) ends
    monkeypatch.setattr(camera, "_open_capture", fake_open)
    monkeypatch.setattr(camera, "_read", lambda cap: next(reads))
    monkeypatch.setattr(camera, "_release", lambda cap: opened.__setitem__("released", True))
    got = []
    agen = camera.rtsp_frames("rtsp://cam")
    async for f in agen:
        got.append(f)
    assert got == ["frame1", "frame2"]
    assert opened["released"] is True   # released after the stream ends

def test_yolo_detect_counts_persons(monkeypatch):
    from wavr.sources import camera
    # Fake YOLO result: two boxes, classes [person=0, chair=56], confs [0.9, 0.7]
    class _Boxes:
        cls = [0, 56]
        conf = [0.9, 0.7]
    class _Result:
        boxes = _Boxes()
    monkeypatch.setattr(camera, "_model", lambda: (lambda frame: [_Result()]))
    det = camera.yolo_detect("frame")
    assert det.count == 1            # only the person box
    assert det.confidence == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: FAIL — `AttributeError: module 'wavr.sources.camera' has no attribute 'rtsp_frames'` (and `_open_capture`, etc.).

- [ ] **Step 3: Write minimal implementation** — append to `backend/wavr/sources/camera.py`

```python
# ---- Real adapters (lazy imports; only exercised on the real hardware path) ----

_YOLO_MODEL = None


def _open_capture(url: str):
    import cv2  # lazy: only needed on the real path
    return cv2.VideoCapture(url)


def _read(cap):
    return cap.read()  # (ok: bool, frame)


def _release(cap) -> None:
    with contextlib.suppress(Exception):
        cap.release()


def _model():
    """Load the YOLO nano model once (GPU if available). Lazy — importing
    ultralytics pulls torch/CUDA, which we never want at import/test time."""
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        from ultralytics import YOLO
        _YOLO_MODEL = YOLO("yolov8n.pt")
    return _YOLO_MODEL


async def rtsp_frames(url: str) -> "AsyncIterator[object]":
    """Pull frames from an RTSP capture. Blocking reads run in a thread so they
    never block the loop; the capture is released in the finally, so aclose()
    on disable is a hard RTSP kill."""
    cap = _open_capture(url)
    try:
        while True:
            ok, frame = await asyncio.to_thread(_read, cap)
            if not ok:
                break
            yield frame
    finally:
        _release(cap)


def yolo_detect(frame) -> Detection:
    results = _model()(frame)
    persons = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        for cls, conf in zip(list(boxes.cls), list(boxes.conf)):
            if int(cls) == 0:  # COCO class 0 = person
                persons.append(float(conf))
    return Detection(count=len(persons), confidence=max(persons) if persons else 0.0)
```

Then change `CameraSource.__init__` defaults:

```python
        self._frames = frames or rtsp_frames
        self._detect = detect or yolo_detect
```

and delete the `assert self._frames is not None ...` line in `events()` (defaults now always present).

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: PASS (all — 3 CameraSource + 2 adapter).

- [ ] **Step 5: Add the optional camera extra** — modify `backend/pyproject.toml`

Under `[project.optional-dependencies]` (next to the existing `dev = [...]`):

```toml
camera = ["opencv-python>=4.9", "ultralytics>=8.2"]
```

Do NOT install it. (Real hardware bring-up runs `pip install -e backend[camera]` later.)

- [ ] **Step 6: Run full suite**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (no cv2/ultralytics imported — all lazy/monkeypatched).

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/sources/camera.py backend/tests/test_camera_source.py backend/pyproject.toml
git commit -m "feat: lazy RTSP+YOLO adapters as CameraSource defaults (deps optional, mock-tested)"
```

---

### Task 3: Wire both cameras boot-OFF + safety tests

**Files:**
- Modify: `backend/wavr/app.py` (`_default_sources`: add `camera_quarto`, `camera_quintal`, both `enabled=False`)
- Create: `backend/tests/test_camera_safety.py`

**Interfaces:**
- Consumes: `CameraSource` (Tasks 1-2), `_default_sources`/`create_app`, `SourceManager`.
- Produces: `_default_sources(cfg)` gains `("camera_quarto", lambda: CameraSource("quarto", cfg.cam_quarto_url, interval=cfg.cam_interval), False)` and `("camera_quintal", lambda: CameraSource("quintal", cfg.cam_quintal_url, interval=cfg.cam_interval), False)`.

- [ ] **Step 1: Write the failing safety test** — create `backend/tests/test_camera_safety.py`

```python
import asyncio
import pytest
from wavr.sourcemanager import SourceManager
from wavr.sources.camera import CameraSource, Detection


def test_default_sources_register_both_cameras_boot_off(monkeypatch):
    for v in ("WAVR_CAM_QUARTO_URL", "WAVR_CAM_QUINTAL_URL"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    from wavr.app import _default_sources
    srcs = {name: enabled for name, factory, enabled in _default_sources(load_config())}
    assert srcs["camera_quarto"] is False   # SAFETY: boot OFF
    assert srcs["camera_quintal"] is False   # SAFETY: boot OFF


async def test_camera_toggle_on_then_off_releases_rtsp():
    released = {"v": False}
    async def frames(url):
        try:
            while True:
                yield "frame"
                await asyncio.sleep(0.001)
        finally:
            released["v"] = True   # RTSP release proxy
    events = []
    async def on_event(ev):
        events.append(ev)
    mgr = SourceManager(on_event)
    mgr.register("camera_quarto",
                 lambda: CameraSource("quarto", "rtsp://x", frames=frames,
                                      detect=lambda f: Detection(1, 0.8), interval=0),
                 enabled=False)                     # boots OFF
    await mgr.start()
    assert all(not s["active"] for s in mgr.status()["sources"])  # nothing reading while OFF
    await mgr.set_enabled("camera_quarto", True)    # conscious enable
    await asyncio.sleep(0.02)
    assert any(s["name"] == "camera_quarto" and s["active"] for s in mgr.status()["sources"])
    assert events and events[0].modality == "camera"
    await mgr.set_enabled("camera_quarto", False)   # kill-switch
    await asyncio.sleep(0.01)
    assert released["v"] is True                    # HARD kill: RTSP released on disable
    await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_safety.py -q`
Expected: FAIL — `KeyError: 'camera_quarto'` (not yet in `_default_sources`).

- [ ] **Step 3: Wire the cameras** — modify `backend/wavr/app.py`

Add the import alongside the other source imports:

```python
from wavr.sources.camera import CameraSource
```

In `_default_sources(cfg)`, append the two camera entries to the returned list (after `sim`):

```python
        ("camera_quarto", lambda: CameraSource("quarto", cfg.cam_quarto_url, interval=cfg.cam_interval), False),
        ("camera_quintal", lambda: CameraSource("quintal", cfg.cam_quintal_url, interval=cfg.cam_interval), False),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_safety.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Confirm `test_app.py` untouched + full suite**

`test_app.py` passes explicit `sources=`, so the new default cameras don't affect it. Run the full suite:
Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (all — Sub-plan B's 50 + Task-1/2/3 new tests).

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/app.py backend/tests/test_camera_safety.py
git commit -m "feat: register C210(quarto)+TC40(quintal) cameras boot-OFF; kill-switch releases RTSP"
```

---

## Definition of Done (Sub-plan C)
- [ ] `CameraSource` pulls RTSP frames and runs person detection behind injectable seams; emits canonical `modality="camera"` events (presence + own confidence, no vitals, motion 0.0); fully unit-tested with fakes.
- [ ] Real RTSP (cv2) + YOLO (ultralytics) adapters are the defaults, lazy-imported, and never loaded at import/test time; deps are an optional `camera` extra, not installed.
- [ ] Both cameras register `enabled=False` (boot-OFF safety) and are proven so by test.
- [ ] Disabling a camera runs the frame generator's `finally` → RTSP `release()` (hard kill), proven by test; a stalled read can't hang the control plane (`asyncio.to_thread` + `SourceManager`'s 5s guard).
- [ ] `fusion.py` untouched; canonical shape unchanged; no frame persists; nothing touches Plano B.
- [ ] Full suite green.

## Next
Camadas 2-4 (rules/away/MQTT, AI narration). Live hardware bring-up: `pip install -e backend[camera]`, set `WAVR_CAM_QUARTO_URL`/`WAVR_CAM_QUINTAL_URL` (RTSP with creds), toggle a camera on and confirm real person detection on the RTX 3060.

## Deferred (carried from Sub-plans A/B — revisit in Camadas or a cleanup pass)
- `modality` as a `Literal`/enum; shared `_normalize_mac` helper; `_run` kill-on-cancel in NetworkSource; SQLite commit-per-event synchronous on the loop.
