# Wavr — Camera hardening (pre-hardware) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the four camera-path items the Sub-plan C final review deferred to hardware bring-up, so `CameraSource` is safe/robust to run against a live camera: keep-alive on transient errors (I2), honest ON/OFF status (I3), thread-safe YOLO load, and VRAM released when the last camera stops.

**Architecture:** Two small, independent changes — `camera.py` (resilience + GPU resource lifecycle) and `sourcemanager.py` (self-pop of completed tasks). Both keep the existing injected-seam design; all tests use fakes (no cv2/torch/camera).

**Tech Stack:** Python 3.11+, asyncio, stdlib `threading`/`contextlib`. `torch` referenced only via a lazy, suppressed import (never required in tests).

## Global Constraints

- Platform Windows 11; interpreter `C:\IA\wavr\.venv\Scripts\python.exe`; run from `C:\IA\wavr`.
- Canonical `SensingEvent` shape unchanged; `CameraSource` still emits `modality="camera"`, motion 0.0, no vitals. `fusion.py` untouched.
- SAFETY unchanged: cameras boot OFF; disabling still runs the frame generator's `finally` → RTSP release. These fixes must NOT weaken that — CancelledError/GeneratorExit must still propagate (re-raise CancelledError; never catch BaseException).
- VRAM: only the YOLO model uses GPU. Releasing it must be safe with `torch` absent (suppressed lazy import) and must only fire when the LAST active camera stops (not when one of several stops).
- TDD; files < 500 lines; DRY, YAGNI.
- Deps stay optional (`[camera]` extra, not installed). Tests never import cv2/ultralytics/torch.

**Branch:** `camera-hardening` off `master` (Sub-plans A+B+C merged).

**Existing interfaces:** `CameraSource(room, rtsp_url="", frames=None, detect=None, interval=0.5, confidence=0.0)` with `events() -> AsyncIterator[SensingEvent]`, wrapping frame iteration in `contextlib.aclosing(self._frames(self._url))` and running `detect` via `asyncio.to_thread`. Module fns `_model()` (lazy YOLO singleton in global `_YOLO_MODEL`), `yolo_detect`, `rtsp_frames`. `SourceManager._run(name)` iterates the source's `events()` and calls `agen.aclose()` in `finally`; `status()` reports `active = name in self._tasks`; `_kill` pops the task before awaiting it.

---

### Task 1: CameraSource keep-alive + thread-safe model load + VRAM release on last stop

**Files:**
- Modify: `backend/wavr/sources/camera.py`
- Modify: `backend/tests/test_camera_source.py`

**Interfaces:**
- Consumes: existing `CameraSource`, `Detection`, `_model`, `_YOLO_MODEL`.
- Produces: `CameraSource.__init__` gains `reconnect_delay: float = 3.0`. `events()` wraps its connect/read/detect loop in a `while True` with `except asyncio.CancelledError: raise` / `except Exception: logging.warning(...)` + reconnect sleep (mirrors RuViewSource), and maintains a module active-camera count so the YOLO model is released when the last camera's `events()` exits. New module fn `release_model()`. `_model()` becomes thread-safe (double-checked `threading.Lock`).

- [ ] **Step 1: Write the failing tests** — append to `backend/tests/test_camera_source.py`

```python
import wavr.sources.camera as _cam

def _reset_active():
    _cam._ACTIVE = 0
    _cam._YOLO_MODEL = None

async def test_camera_survives_transient_detect_error():
    _reset_active()
    calls = {"n": 0}
    def detect(f):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad frame")   # transient
        return Detection(count=1, confidence=0.7)
    async def frames(url):
        yield "f"                              # one frame per connection
    src = CameraSource("quarto", frames=frames, detect=detect, reconnect_delay=0)
    [ev] = await _first_n(src, 1)              # 1st frame errors -> reconnect -> 2nd ok
    assert ev.presence is True and calls["n"] == 2

async def test_last_camera_stop_releases_model(monkeypatch):
    _reset_active()
    calls = {"n": 0}
    monkeypatch.setattr(_cam, "release_model", lambda: calls.__setitem__("n", calls["n"] + 1))
    async def frames(url):
        while True:
            yield "f"
    src = CameraSource("quarto", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    agen = src.events()
    await agen.__anext__()
    await agen.aclose()
    assert calls["n"] == 1                      # only/last camera stopped -> released

async def test_model_not_released_while_another_camera_active(monkeypatch):
    _reset_active()
    calls = {"n": 0}
    monkeypatch.setattr(_cam, "release_model", lambda: calls.__setitem__("n", calls["n"] + 1))
    async def frames(url):
        while True:
            yield "f"
    a = CameraSource("quarto", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    b = CameraSource("quintal", frames=frames, detect=lambda f: Detection(1, 0.5), interval=0)
    ag_a, ag_b = a.events(), b.events()
    await ag_a.__anext__(); await ag_b.__anext__()   # _ACTIVE == 2
    await ag_a.aclose()
    assert calls["n"] == 0                            # one still active -> not released
    await ag_b.aclose()
    assert calls["n"] == 1                            # last stopped -> released

def test_release_model_nulls_global_and_is_torch_safe():
    _cam._YOLO_MODEL = "sentinel"
    _cam.release_model()                              # torch absent -> suppressed, no raise
    assert _cam._YOLO_MODEL is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: FAIL — `AttributeError: module 'wavr.sources.camera' has no attribute '_ACTIVE'` / `release_model`, and the keep-alive test errors out (no reconnect yet).

- [ ] **Step 3: Implement** — modify `backend/wavr/sources/camera.py`

Add to the top imports (alongside the existing ones): `import logging`, `import threading`.

Add module state near `_YOLO_MODEL = None`:

```python
_ACTIVE = 0                       # count of running CameraSource.events() loops
_MODEL_LOCK = threading.Lock()    # guards the lazy YOLO load (called from to_thread workers)
```

Make `_model()` thread-safe (double-checked locking):

```python
def _model():
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        with _MODEL_LOCK:
            if _YOLO_MODEL is None:
                from ultralytics import YOLO
                _YOLO_MODEL = YOLO("yolov8n.pt")
    return _YOLO_MODEL
```

Add the release fn:

```python
def release_model() -> None:
    """Drop the cached YOLO model and hand cached VRAM back to the driver. Safe
    with torch absent (suppressed). Called when the last camera stops so the GPU
    isn't held while no camera is running (e.g. so games get the VRAM back)."""
    global _YOLO_MODEL
    with _MODEL_LOCK:
        _YOLO_MODEL = None
    with contextlib.suppress(Exception):
        import torch
        torch.cuda.empty_cache()
```

Change `__init__` to add `reconnect_delay: float = 3.0` (store `self._reconnect = reconnect_delay`).

Rewrite `events()` with the keep-alive loop + active-count/release:

```python
    async def events(self) -> AsyncIterator[SensingEvent]:
        global _ACTIVE
        _ACTIVE += 1
        try:
            while True:
                try:
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
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.warning("CameraSource(%s) error; reconnecting", self.room, exc_info=True)
                if self._reconnect:
                    await asyncio.sleep(self._reconnect)
        finally:
            _ACTIVE -= 1
            if _ACTIVE == 0:
                release_model()
```

> Note: `_ACTIVE` inc/dec and the `== 0` check run in the single event loop with no `await` between the decrement and the check, so no lock is needed for the counter. `release_model`'s `_MODEL_LOCK` guards only the model global against the `to_thread` load path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_source.py -q`
Expected: PASS (prior camera tests + 4 new). If a prior test that drives `events()` now loops on reconnect, confirm it uses `_first_n` (breaks after N) or `aclose()` — the existing tests do.

- [ ] **Step 5: Full suite**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (59 + new). No cv2/torch imported.

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/sources/camera.py backend/tests/test_camera_source.py
git commit -m "fix: CameraSource keep-alive + thread-safe YOLO load + release VRAM on last camera stop"
```

---

### Task 2: SourceManager self-pops completed tasks (honest ON/OFF status)

**Files:**
- Modify: `backend/wavr/sourcemanager.py`
- Modify: `backend/tests/test_sourcemanager.py`

**Interfaces:**
- Consumes: existing `SourceManager`.
- Produces: `_run(name)`'s `finally` removes its own task from `self._tasks` on natural completion (guarded so it never removes a replacement task), so a source whose `events()` ends on its own reports `active=False` in `status()`.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_sourcemanager.py`

```python
async def test_self_terminated_source_reports_inactive():
    got = []
    async def on_event(ev):
        got.append(ev)

    class _Finite:
        async def events(self):
            yield "x"          # emit once, then the generator ends naturally

    mgr = SourceManager(on_event)
    mgr.register("finite", lambda: _Finite(), True)
    await mgr.start()
    await asyncio.sleep(0.02)   # let it emit and complete
    status = {s["name"]: s["active"] for s in mgr.status()["sources"]}
    assert status["finite"] is False   # completed task must not report active
    assert "x" in got
    await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sourcemanager.py -q`
Expected: FAIL — `assert True is False`; the completed task is still in `self._tasks`, so `status()` reports `active=True`.

- [ ] **Step 3: Implement** — modify `backend/wavr/sourcemanager.py`

In `_run`, change the `finally` block so the completed task removes itself (guarded against the `_kill` race and against removing a re-spawned replacement):

```python
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()
            # Self-terminated source (generator ended on its own): drop it from
            # the active set so status() reports active=False. Only pop if the
            # registered task is still THIS one (a re-enable may have replaced it;
            # _kill pops before awaiting, so this is a no-op on the cancel path).
            if self._tasks.get(name) is asyncio.current_task():
                self._tasks.pop(name, None)
```

(Keep the existing `except asyncio.CancelledError: raise` and `except Exception: logging.exception(...)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sourcemanager.py -q`
Expected: PASS (all existing sourcemanager tests + the new one). Existing tests use infinite sources (never complete), so their behavior is unchanged.

- [ ] **Step 5: Full suite**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/sourcemanager.py backend/tests/test_sourcemanager.py
git commit -m "fix: SourceManager self-pops completed tasks so status() ON/OFF is honest"
```

---

## Definition of Done
- [ ] A transient `detect`/read error no longer kills a camera — it logs and reconnects; CancelledError still propagates (kill-switch intact).
- [ ] `_model()` is thread-safe (no double YOLO load under concurrent first-detection).
- [ ] The YOLO model + cached VRAM is released when the LAST active camera stops; safe with torch absent; not released while another camera runs.
- [ ] A self-terminated source reports `active=False` in `status()` (honest dashboard ON/OFF); the kill-switch and existing sources are unaffected.
- [ ] `fusion.py` untouched; canonical shape unchanged; deps still optional; full suite green.

## Next
Fase 1 of the deploy plan (Dockerize + GPU). Live camera bring-up: `pip install -e backend[camera]`, set RTSP env, toggle on.
