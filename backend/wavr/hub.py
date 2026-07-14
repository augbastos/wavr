from __future__ import annotations

import asyncio
from contextlib import suppress

# Bound per-subscriber buffering. A live view wants freshness over completeness, so a
# slow-but-open consumer drops its oldest frames instead of growing memory unbounded.
_MAX_QUEUE = 256


class Hub:
    """Fan-out broadcaster. Extension seam: Camada 2/3 just subscribe() and react."""

    def __init__(self, maxsize: int = _MAX_QUEUE):
        self._subscribers: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        """Current live-view subscriber count -- a bare int, never the queues
        themselves. Passive/zero-cost read used by GET /api/companion/health's
        `ws_clients` self-report (no egress, no new state)."""
        return len(self._subscribers)

    async def publish(self, item: dict) -> None:
        # Non-blocking: never await a slow consumer. On a full queue, drop the oldest
        # frame to make room for the newest (bounded memory, backpressure-free).
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with suppress(asyncio.QueueFull):
                    q.put_nowait(item)
