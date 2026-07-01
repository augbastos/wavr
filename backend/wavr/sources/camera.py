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
        self._frames = frames or rtsp_frames
        self._detect = detect or yolo_detect
        self._interval = interval

    async def events(self) -> AsyncIterator[SensingEvent]:
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
