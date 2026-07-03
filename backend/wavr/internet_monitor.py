"""Internet / gateway outage monitor (Feature B) -- opt-in, LOCAL only.

DEFENSIVE + LOCAL invariant: with zero configuration this checks reachability
of the LAN default gateway (the router), NOT any internet host -- so out of
the box this module makes zero egress off the LAN. An operator MAY point it at
a real internet host via `internet_check_host` to specifically monitor WAN
connectivity, but that is an explicit, informed opt-in -- never the default
(mirrors mqtt_publisher/notifier/ha_client's "self-hosted / configurable, not
a hardcoded cloud endpoint" rule).

OFF by default (`WAVR_INTERNET_MONITOR`); no background task and no pings
happen unless a caller injects a monitor or the flag is set (same opt-in shape
as AwayMonitor / NetworkInventoryService / the ntfy notifier).

On a down->up or up->down transition it fires the injected `notify` (ntfy)
with a short derived-only message, the same way AwayMonitor does on the
arrived/left edge. Debounced: N consecutive failed checks are required before
a "down" transition fires, so one dropped ping never alerts; recovery ("up")
fires as soon as a single check succeeds. Tolerant: a raising `check` (or
`notify`) is caught and logged, never propagated into the run loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from wavr.sources.network import _local_ipv4, _run

_LOG = logging.getLogger(__name__)

CheckFn = Callable[[], Awaitable[bool]]


def guess_gateway(local_ip: str | None = None) -> str | None:
    """Best-effort LAN gateway guess: the '.1' host of the local /24. This is a
    common home-router convention, not a real routing-table read (which would
    need OS-specific parsing) -- good enough for a LOCAL reachability
    heartbeat. Returns None when the local IPv4 address can't be determined."""
    ip = local_ip if local_ip is not None else _local_ipv4()
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3] + ["1"])


async def _ping_once(host: str) -> bool:
    """Default real reachability check: a single OS ping (Windows flags, mirrors
    wavr.sources.network.arp_scan / wavr.netinventory._arp_output). Best-effort
    -- any failure (no route, host down, subprocess error) means False, never
    raises."""
    try:
        out = await _run("ping", "-n", "1", "-w", "1000", host)
    except Exception:
        return False
    return "ttl=" in out.lower()


def make_checker(host: str) -> CheckFn:
    """Build a `check()` coroutine that pings `host` once. Used as the default
    transport; tests inject their own CheckFn instead (no real network)."""
    async def check() -> bool:
        return await _ping_once(host)
    return check


class InternetMonitor:
    """Periodically checks reachability of `host` (default: the LAN gateway,
    via `guess_gateway`) and fires `notify` on a debounced down<->up
    transition. `check` is the injectable transport for tests -- default is a
    real ping at `host` (or 127.0.0.1 as a last resort if the local IP can't be
    determined, which will simply always read "up" and is harmless)."""

    def __init__(self, check: CheckFn | None = None, host: str | None = None,
                 interval: float = 15.0, fail_threshold: int = 3,
                 notify: Callable[[str], None] | None = None):
        self._check = check or make_checker(host or guess_gateway() or "127.0.0.1")
        self._interval = interval
        self._fail_threshold = max(1, fail_threshold)
        self._notify = notify
        self._ok: bool | None = None      # None = no determination yet
        self._since: str | None = None
        self._consec_ok = 0
        self._consec_fail = 0
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        return {"ok": self._ok, "since": self._since}

    async def check_once(self) -> bool:
        """Run a single check and fold it into the debounced state. Called by
        the background loop; also directly callable (deterministic) for
        tests. Never raises -- an exception from `check` counts as a failed
        check."""
        try:
            reachable = await self._check()
        except Exception:
            _LOG.warning("internet_monitor check failed", exc_info=True)
            reachable = False
        self._record(reachable)
        return reachable

    def _record(self, reachable: bool) -> None:
        if reachable:
            self._consec_fail = 0
            self._consec_ok += 1
        else:
            self._consec_ok = 0
            self._consec_fail += 1

        if self._ok is None:
            # First-ever determination settles as soon as debounce is met, but
            # never fires a transition notification (mirrors AwayMonitor's
            # `first` guard -- there's no prior state to have "changed" from).
            if reachable:
                self._set(True, notify=False)
            elif self._consec_fail >= self._fail_threshold:
                self._set(False, notify=False)
            return
        if self._ok and self._consec_fail >= self._fail_threshold:
            self._set(False, notify=True)
        elif not self._ok and reachable:
            self._set(True, notify=True)

    def _set(self, ok: bool, notify: bool) -> None:
        self._ok = ok
        self._since = datetime.now(timezone.utc).isoformat()
        if notify and self._notify:
            try:
                self._notify("Wavr: internet voltou" if ok else "Wavr: internet caiu")
            except Exception:
                _LOG.warning("internet_monitor notify failed", exc_info=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("internet monitor loop error")
            if self._interval:
                await asyncio.sleep(self._interval)

    async def start(self) -> None:
        """Spawn the periodic check task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the check task, cancel-safe (mirrors NetworkInventoryService
        teardown)."""
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
