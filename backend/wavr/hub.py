from __future__ import annotations

import asyncio


class Hub:
    """Fan-out broadcaster. Extension seam: Camada 2/3 just subscribe() and react."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, item: dict) -> None:
        for q in list(self._subscribers):
            await q.put(item)
