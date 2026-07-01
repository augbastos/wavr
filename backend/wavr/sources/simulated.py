from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import AsyncIterator

from wavr.events import SensingEvent

# Fixed fictional apartment: which modality "watches" which room.
# "casa" = house-level presence (network); rooms get wifi_csi and/or camera.
SENSORS = [
    ("casa", "network"),
    ("sala", "wifi_csi"),
    ("quarto", "wifi_csi"),
    ("quarto", "camera"),
    ("quintal", "camera"),
]


class SimulatedSource:
    """Emits a plausible multi-modal fictional stream. No real data, no RNG."""

    def __init__(self, interval: float = 1.0):
        self._interval = interval
        self._tick = 0

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            for idx, (room, modality) in enumerate(SENSORS):
                yield self._make(room, modality, idx)
            self._tick += 1
            await asyncio.sleep(self._interval if self._interval else 0)

    def _make(self, room: str, modality: str, idx: int) -> SensingEvent:
        phase = self._tick + idx
        present = (phase % 7) < 4
        gives_vitals = modality == "wifi_csi"
        # camera is high-confidence, network low, wifi mid
        conf = {"camera": 0.95, "wifi_csi": 0.9, "network": 0.6, "sim": 0.6}.get(modality, 0.5)
        return SensingEvent(
            room=room,
            modality=modality,
            presence=present,
            motion=round(abs(math.sin(phase / 3.0)) * 10, 3) if present else 0.0,
            breathing_bpm=round(12 + 3 * math.sin(phase / 5.0), 2) if (present and gives_vitals) else None,
            heart_bpm=round(60 + 10 * math.sin(phase / 4.0), 2) if (present and gives_vitals) else None,
            confidence=conf if present else round(conf * 0.3, 3),
            ts=datetime.now(timezone.utc).isoformat(),
        )
