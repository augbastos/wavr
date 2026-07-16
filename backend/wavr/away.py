from __future__ import annotations

from typing import Callable


class AwayMonitor:
    """House-level presence: home if ANY room is occupied, else away (debounced).
    Publishes retained house state + arrived/left edge events for home automation,
    and (opt-in) fires a short ntfy notification on the SAME arrived/left edge.
    Only house-level home/away is ever emitted — never room detail/frames/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None] | None = None,
                 prefix: str = "wavr", away_grace: int = 3,
                 notify: Callable[[str], None] | None = None,
                 on_edge: Callable[[bool], None] | None = None):
        # `publish` defaults to a no-op so an ntfy-only caller (no MQTT) can still
        # drive this monitor for its edge detection without a real publisher.
        self._publish = publish or (lambda *a: None)
        self._prefix = prefix
        self._grace = away_grace
        self._notify = notify
        # `on_edge(home: bool)` is the in-process seam the routines engine taps: it
        # fires on the SAME debounced arrived/left edge as the MQTT event + ntfy
        # below, so routines inherit the grace/debounce and never re-derive presence.
        # None (the default) -> byte-identical to before this seam existed.
        self._on_edge = on_edge
        self._rooms: dict[str, bool] = {}
        self._house: bool | None = None   # True=home, False=away, None=undetermined
        self._vacant_streak = 0

    @property
    def home(self) -> bool | None:
        """Current debounced house state (True=home, False=away, None=undetermined)
        -- read by the routines tick for the house_away_by_time deadline trigger."""
        return self._house

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
            if self._on_edge:
                # After the existing sinks, and never on the first determination
                # (no spurious edge at boot) -- same guard as the event/ntfy above.
                self._on_edge(home)

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
