from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
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
                 interval: float = 0.5, confidence: float = 0.0,
                 reconnect_delay: float = 3.0):
        self.room = room
        self._url = rtsp_url
        self._frames = frames or rtsp_frames
        self._confidence = confidence
        # An injected `detect` is responsible for its own thresholding — the
        # confidence param is only applied on the real (yolo_detect) path below.
        self._detect = detect or (lambda f: yolo_detect(f, self._confidence))
        self._interval = interval
        self._reconnect = reconnect_delay

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


# ---- Real adapters (lazy imports; only exercised on the real hardware path) ----

_YOLO_MODEL = None
_ACTIVE = 0                       # count of running CameraSource.events() loops
_MODEL_LOCK = threading.Lock()    # guards the lazy YOLO load (called from to_thread workers)


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
    ultralytics pulls torch/CUDA, which we never want at import/test time.
    Thread-safe (double-checked locking): _detect runs via asyncio.to_thread,
    so concurrent cameras could race on the first load without this lock."""
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        with _MODEL_LOCK:
            if _YOLO_MODEL is None:
                from ultralytics import YOLO
                _YOLO_MODEL = YOLO("yolov8n.pt")
    return _YOLO_MODEL


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


def yolo_detect(frame, conf_threshold: float = 0.0) -> Detection:
    results = _model()(frame)
    persons = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        for cls, conf in zip(list(boxes.cls), list(boxes.conf)):
            if int(cls) == 0 and float(conf) >= conf_threshold:  # COCO class 0 = person
                persons.append(float(conf))
    return Detection(count=len(persons), confidence=max(persons) if persons else 0.0)
