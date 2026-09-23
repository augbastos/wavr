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


# Longest wait between reconnect attempts while the service stays unreachable.
_MAX_RECONNECT_DELAY_S = 60.0


class RuViewSource:
    """WiFi CSI presence + vitals from a RuView sensing WebSocket. Reconnects
    forever on drop so a missing/rebooting container never crashes the manager.
    Maps each 'sensing_update' frame via the shared normalize_ruview().

    Reconnects BACK OFF: the delay doubles from `reconnect_delay` up to a minute
    while the service stays down, and resets the moment a frame arrives. It used
    to retry every few seconds with a full traceback each time -- and because it
    was registered on every install, whether or not anybody ran RuView, every
    default Core logged a stack trace every seven seconds for its whole life.
    It is now registered only when WAVR_RUVIEW_URL is set (see `_default_sources`),
    and an unreachable service is logged once per outage, not once per attempt.
    """

    def __init__(self, url: str, room: str = "sala",
                 connect: Callable[[str], AsyncIterator[dict]] | None = None,
                 reconnect_delay: float = 3.0):
        self._url = url
        self._room = room
        self._connect = connect or _default_connect
        self._delay = reconnect_delay

    async def events(self) -> AsyncIterator[SensingEvent]:
        delay = self._delay
        failing = False
        while True:
            try:
                async with contextlib.aclosing(self._connect(self._url)) as stream:
                    async for frame in stream:
                        if failing:
                            logging.info("RuViewSource: %s is answering again", self._url)
                            failing = False
                        delay = self._delay
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
            except Exception as exc:
                # One line per outage at WARNING; the traceback, and every retry
                # after the first, at DEBUG. An absent service is a state, not a
                # new event every few seconds.
                if not failing:
                    logging.warning("RuViewSource: cannot reach %s (%s); retrying "
                                    "with backoff up to %.0fs", self._url,
                                    type(exc).__name__, _MAX_RECONNECT_DELAY_S)
                    failing = True
                logging.debug("RuViewSource connection error", exc_info=True)
            if delay:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_S)
