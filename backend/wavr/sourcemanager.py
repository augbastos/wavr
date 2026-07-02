from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable


class SourceManager:
    """Runs one async task per ENABLED source, each feeding on_event. Global on/off
    (start/stop) + per-source on/off at runtime. Heavy sources (camera CV) only
    consume resources while enabled — disabling cancels the task."""

    def __init__(self, on_event: Callable[[object], Awaitable]):
        self._on_event = on_event
        self._factories: dict[str, Callable[[], object]] = {}
        self._enabled: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def register(self, name: str, factory: Callable[[], object], enabled: bool = True) -> None:
        self._factories[name] = factory
        self._enabled[name] = enabled
        # If the manager is already running, a newly registered enabled source must
        # start immediately — otherwise a runtime register() silently no-ops until
        # the next full start()/set_enabled() cycle.
        if enabled and self._running:
            self._spawn(name)

    async def start(self) -> None:
        self._running = True
        for name, en in self._enabled.items():
            if en:
                self._spawn(name)

    async def stop(self) -> None:
        self._running = False
        for name in list(self._tasks):
            await self._kill(name)

    async def set_running(self, running: bool) -> None:
        await (self.start() if running else self.stop())

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._factories:
            raise KeyError(name)
        self._enabled[name] = enabled
        if enabled and self._running:
            self._spawn(name)
        elif not enabled:
            await self._kill(name)

    async def unregister(self, name: str) -> None:
        """Kill the source's task if running and remove it from the roster. Used by
        the in-app camera CRUD to drop a camera at runtime."""
        if name not in self._factories:
            raise KeyError(name)
        await self._kill(name)
        self._factories.pop(name, None)
        self._enabled.pop(name, None)

    def status(self) -> dict:
        return {
            "running": self._running,
            "sources": [
                {"name": n, "enabled": self._enabled[n], "active": n in self._tasks}
                for n in self._factories
            ],
        }

    def _spawn(self, name: str) -> None:
        if name not in self._tasks:
            self._tasks[name] = asyncio.create_task(self._run(name))

    async def _kill(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
            # wait_for guards against a source whose teardown blocks (e.g. a stalled
            # camera read) so a disable/stop can't hang the control plane.
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)

    async def _run(self, name: str) -> None:
        agen = self._factories[name]().events()
        try:
            async for ev in agen:
                await self._on_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("source %s crashed", name)
        finally:
            # Deterministic teardown: runs the source generator's cleanup (e.g. a
            # CameraSource releasing its RTSP stream) the moment the task is cancelled.
            with contextlib.suppress(Exception):
                await agen.aclose()
            # Self-terminated source (generator ended on its own): drop it from
            # the active set so status() reports active=False. Only pop if the
            # registered task is still THIS one (a re-enable may have replaced it;
            # _kill pops before awaiting, so this is a no-op on the cancel path).
            if self._tasks.get(name) is asyncio.current_task():
                self._tasks.pop(name, None)
