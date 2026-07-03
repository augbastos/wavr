from __future__ import annotations

from typing import Callable


class AwayMonitor:
    """House-level presence: home if ANY room is occupied, else away (debounced).
    Publishes retained house state + arrived/left edge events for home automation,
    and (opt-in) fires a short ntfy notification on the SAME arrived/left edge.
    Only house-level home/away is ever emitted — never room detail/frames/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None] | None = None,
                 prefix: str = "wavr", away_grace: int = 3,
                 notify: Callable[[str], None] | None = None):
        # `publish` defaults to a no-op so an ntfy-only caller (no MQTT) can still
        # drive this monitor for its edge detection without a real publisher.
        self._publish = publish or (lambda *a: None)
        self._prefix = prefix
        self._grace = away_grace
        self._notify = notify
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
            if self._notify:
                self._notify("Wavr: alguém chegou em casa" if home else "Wavr: casa vazia")

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
