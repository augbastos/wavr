from __future__ import annotations

import json
from typing import Callable


class RulesEngine:
    """Consumes fused RoomState from the Hub and emits MQTT for home automation.
    Publishes each room's current occupancy to a RETAINED state topic (so a broker
    subscriber always sees the latest), and an edge EVENT topic only when occupancy
    flips. Only derived state is published — never frames/CSI/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None], prefix: str = "wavr"):
        self._publish = publish
        self._prefix = prefix
        self._last: dict[str, bool] = {}   # room -> last occupied

    def handle(self, rs: dict) -> None:
        room = rs["room"]
        occupied = bool(rs["occupied"])
        self._publish(
            f"{self._prefix}/rooms/{room}/state",
            json.dumps({"occupied": occupied, "confidence": rs["confidence"], "ts": rs["ts"]}),
            True,   # retained: latest state persists on the broker
        )
        prev = self._last.get(room)
        if prev is not None and prev != occupied:
            self._publish(f"{self._prefix}/rooms/{room}/event",
                          "occupied" if occupied else "vacant", False)
        self._last[room] = occupied

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
