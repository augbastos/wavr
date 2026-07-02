from __future__ import annotations

from typing import Callable


class AwayMonitor:
    """House-level presence: home if ANY room is occupied, else away (debounced).
    Publishes retained house state + arrived/left edge events for home automation.
    Only house-level home/away is published — never room detail/frames/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None],
                 prefix: str = "wavr", away_grace: int = 3):
        self._publish = publish
        self._prefix = prefix
        self._grace = away_grace
        self._rooms: dict[str, bool] = {}
        self._house: bool | None = None   # True=home, False=away, None=undetermined
        self._vacant_streak = 0

    def handle(self, rs: dict) -> None:
        self._rooms[rs["room"]] = bool(rs["occupied"])
        if any(self._rooms.values()):
            self._vacant_streak = 0
            self._set_house(True)
        else:
            self._vacant_streak += 1
            if self._vacant_streak >= self._grace:
                self._set_house(False)

    def _set_house(self, home: bool) -> None:
        if self._house == home:
            return
        first = self._house is None
        self._house = home
        self._publish(f"{self._prefix}/house/state", "home" if home else "away", True)
        if not first:
            self._publish(f"{self._prefix}/house/event", "arrived" if home else "left", False)

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
