from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from wavr.provider_runtime import STATE_STOPPED, SourceSupervisor, supervised


class SourceManager:
    """Runs one supervised async task per ENABLED source, each feeding on_event.
    Global on/off (start/stop) + per-source on/off at runtime. Heavy sources
    (camera CV) only consume resources while enabled — disabling cancels the task.

    **Supervised** is the word that changed. A source used to get exactly one
    life: `_run` caught the exception, logged it and let the task die, so a
    camera whose stream was cut by a router reboot stayed dead until somebody
    restarted Wavr. Now a failure is a restart with backoff, and the health
    behind that restart is published — see `wavr.provider_runtime` for why both
    halves were needed, and why the second one matters more.
    """

    def __init__(self, on_event: Callable[[object], Awaitable],
                 supervisor: SourceSupervisor | None = None):
        self._on_event = on_event
        self._factories: dict[str, Callable[[], object]] = {}
        self._enabled: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self.supervisor = supervisor or SourceSupervisor()

    def register(self, name: str, factory: Callable[[], object],
                 enabled: bool = True, heartbeat_s: float | None = None) -> None:
        """Add a source.

        `heartbeat_s` declares how long this source may go quiet before silence
        is itself evidence of a fault. Most sources leave it None, because for
        them silence means nothing — a PIR is quiet in an empty room, and a
        camera is quiet while its lens is covered, and calling either one a
        fault would be crying wolf. Give it a value only for a source that
        streams continuously by nature, where a gap can only mean the pipe has
        stalled without anybody raising.
        """
        self._factories[name] = factory
        self._enabled[name] = enabled
        self.supervisor.register(name, heartbeat_s=heartbeat_s)
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
        self.supervisor.forget(name)

    async def restart(self, name: str) -> None:
        """Bring a failed source back now instead of at its next scheduled probe.

        The action behind "try again" on a fault. A source in the slow cold-probe
        state can be five minutes from its next attempt, and somebody who has
        just plugged the camera back in should not have to wait that out — nor
        should they have to restart Wavr, which is what they would otherwise do.
        """
        if name not in self._factories:
            raise KeyError(name)
        await self._kill(name)
        if self._enabled.get(name) and self._running:
            self._spawn(name)

    def status(self) -> dict:
        """What the manager is doing, plus what the supervisor actually observed.

        `enabled` and `active` keep their old meanings and their old places —
        the diagnostics panel and `net_doctor` both read them. `state` and
        `health` are the addition: `active` flickers false for a moment during
        any restart, so a caller asking "is this really broken" needs the state
        machine rather than a sample of whether a task object exists right now.
        """
        health = {h.name: h for h in self.supervisor.all()}
        return {
            "running": self._running,
            "sources": [
                {"name": n, "enabled": self._enabled[n], "active": n in self._tasks,
                 "state": (self.supervisor.state_of(n) if self._enabled[n]
                           else STATE_STOPPED),
                 "healthy": bool(self._enabled[n] and self.supervisor.healthy(n))}
                for n in self._factories
            ],
            "health": [health[n].to_dict() for n in sorted(health)
                       if n in self._factories],
        }

    def healthy_sources(self) -> set[str]:
        """The sources genuinely producing right now.

        This is what a coverage or trust surface must ask, and it is NOT the set
        of enabled ones. A camera that is switched on but whose task is in
        backoff is not watching the kitchen, and saying otherwise on the screen
        whose whole job is honesty about what Wavr can see would be the worst
        bug in the product.
        """
        return {n for n in self._factories
                if self._enabled.get(n) and n in self._tasks
                and self.supervisor.healthy(n)}

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
        self.supervisor.on_stop(name)

    def _should_run(self, name: str) -> bool:
        return bool(self._running and self._enabled.get(name))

    async def _run(self, name: str) -> None:
        try:
            await supervised(
                name, self._factories[name], self._on_event, self.supervisor,
                should_run=lambda: self._should_run(name))
        except asyncio.CancelledError:
            raise
        except Exception:
            # `supervised` is the thing that contains source failures, so
            # reaching here means the supervisor itself broke. Logged loudly and
            # distinctly from a source crash, because the two need completely
            # different fixes and conflating them would hide a Wavr bug inside a
            # message that reads like somebody's camera being unplugged.
            logging.exception("supervisor for source %s failed", name)
        finally:
            # Defensive: on the cancel path `_kill` has already popped, and the
            # supervised loop only returns once `should_run` is false. Popping a
            # task that is no longer the current one would drop a live restart.
            if self._tasks.get(name) is asyncio.current_task():
                self._tasks.pop(name, None)
