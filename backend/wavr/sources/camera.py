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
