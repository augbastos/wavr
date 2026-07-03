from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent, normalize_ruview


async def _default_connect(url: str) -> AsyncIterator[dict]:
    """Real WS client: connect and yield decoded JSON frames for the life of the
    connection. `websockets` is a WS client library (present via uvicorn[standard],
    declared explicitly in pyproject)."""
    import websockets  # local import so the module loads even if unused in tests

    async with websockets.connect(url) as ws:
        async for raw in ws:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue


class RuViewSource:
    """WiFi CSI presence + vitals from a RuView sensing WebSocket. Reconnects
    forever on drop so a missing/rebooting container never crashes the manager.
    Maps each 'sensing_update' frame via the shared normalize_ruview()."""

    def __init__(self, url: str, room: str = "sala",
                 connect: Callable[[str], AsyncIterator[dict]] | None = None,
                 reconnect_delay: float = 3.0):
        self._url = url
        self._room = room
        self._connect = connect or _default_connect
        self._delay = reconnect_delay

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            try:
                async with contextlib.aclosing(self._connect(self._url)) as stream:
                    async for frame in stream:
                        if not (isinstance(frame, dict) and frame.get("type") == "sensing_update"):
                            continue
                        try:
                            ev = normalize_ruview(frame, self._room)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # Per-frame error (e.g. a bad timestamp): skip just this
                            # frame and keep the connection alive — only a
                            # connection-level error below should trigger a reconnect.
                            logging.warning("RuViewSource bad frame; skipping", exc_info=True)
                            continue
                        yield ev
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.warning("RuViewSource connection error; reconnecting", exc_info=True)
            if self._delay:
                await asyncio.sleep(self._delay)
