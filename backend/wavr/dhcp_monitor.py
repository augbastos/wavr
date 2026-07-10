"""Rogue / multiple-DHCP-server detector (defensive-inventory #7) -- opt-in, LOCAL only.

Same shape/tolerance rules as `internet_monitor.InternetMonitor` and
`netinventory_service.NetworkInventoryService`: OFF by default, no background
task and no packets sent/received unless a caller injects a monitor or the
integration-layer config flag (`WAVR_NET_DHCP_MONITOR`) is set; a raising
`collect`/`on_rogue` is caught and logged, never propagated into the run loop.

DEBOUNCE / FALSE-POSITIVE DESIGN (the whole point of this module -- a cheap
detector is useless if noisy):
  * The alert is edge-triggered on a *server identity* (DHCP option-54 Server
    Identifier, or its source IP as a fallback), not on "the observed set
    changed" -- so a router simply REBOOTING (one server blinking off then
    back on) never crosses into "extra server" territory: it's the SAME
    known id disappearing and reappearing, not a new one showing up.
  * An extra (not-yet-known) server id must accumulate `alert_threshold`
    "present" cycles within a sliding window before it fires (audit fix #5 --
    a leaky N-of-M window, NOT a strict consecutive-streak reset): a single
    absent cycle no longer wipes all prior progress back to zero, so an
    INTERMITTENT rogue (e.g. offering every other 30s window) still
    accumulates toward the threshold instead of never firing at all. Once an
    id HAS fired, recovery is immediate -- edge-triggered like
    `NetworkInventoryService`'s rogue-MAC alerts: it will not fire again
    while it stays "extra" every cycle (no alert storm), but IS forgotten
    (and can re-alert fresh) the very next cycle it goes quiet, matching the
    "a departed device re-alerts if it returns" rule. An id that never
    crossed the threshold and has fallen completely out of the window
    (absent for the window's whole span) is forgotten too, bounding memory
    for a one-off blip.
  * Anti-flood (audit fix #5): distinct tracked extra ids are capped at
    `max_tracked_extras` per window -- a burst of OFFERs bearing many
    spoofed option-54 server-ids can't grow the tracking dict unboundedly or
    crowd out an already-accumulating genuine rogue's progress (brand-new
    ids beyond the cap are simply not tracked that cycle; already-tracked
    ids are unaffected). And when multiple ids cross the threshold in the
    SAME cycle, `on_rogue` fires exactly ONCE (coalesced) rather than once
    per id -- the alert LOG below still records one row per id for triage;
    only the push/ntfy fan-out is capped.
  * A legitimate second DHCP server (failover pair, a second router the
    operator actually runs) is handled by seeding `known_servers` with both
    IPs up front -- same allowlist idea as `NetworkInventoryService.known_macs`.
    Left unset, the baseline auto-adopts whatever is observed in the FIRST
    cycle (mirrors InternetMonitor/AwayMonitor's "first-ever determination
    settles without alerting" rule) -- this assumes the LAN is clean at first
    boot; an operator who wants a stronger guarantee should set
    `known_servers` explicitly (e.g. the LAN gateway's own IP).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from wavr.alert_severity import SEVERITY_ALERT

_LOG = logging.getLogger(__name__)

CollectFn = Callable[[], Awaitable[dict]]


@dataclass(frozen=True)
class DhcpRogueAlert:
    """One rogue/extra-DHCP-server sighting. Kept in-memory only, same
    bounded-ring convention as `netinventory_service.RogueAlert`. `severity`
    rides wavr.alert_severity's ONE alert ladder (the same ladder RogueAlert
    and GatewayAlert use, so /api/alerts never forks into three gradients) at
    `alert`: a second DHCP server is a real security-relevant LAN event but,
    unlike a confirmed-and-persisting gateway-identity spoof, is not by itself
    confirmed malicious (could be an un-allowlisted legitimate box), so it
    never claims `critical`."""
    ts: str
    extra_server: str
    known_servers: tuple[str, ...]
    observed_servers: tuple[str, ...]
    severity: str = SEVERITY_ALERT

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": "rogue_dhcp",
            "severity": self.severity,
            "extra_server": self.extra_server,
            "known_servers": list(self.known_servers),
            "observed_servers": list(self.observed_servers),
        }


def make_collector(collect_duration: float = 3.0, probe: bool = False) -> CollectFn:
    """Build the default real collect() -- lazily imports
    `wavr.sources.dhcp.DHCPCollector` so the UDP/68 socket (and the one-shot
    DISCOVER broadcast when `probe` is on) is only ever opened once this
    monitor is actually enabled/used (tests inject their own `collect`
    instead)."""
    async def collect() -> dict:
        from wavr.sources.dhcp import DHCPCollector
        return await DHCPCollector(probe=probe).collect(duration=collect_duration)
    return collect


class RogueDhcpMonitor:
    """Periodically collects the DHCP servers currently offering on the LAN
    (via `collect`, injectable -- default: `wavr.sources.dhcp.DHCPCollector`)
    and fires `on_rogue` on a debounced "new server id showed up" edge -- the
    SAME `on_rogue`-callback shape `NetworkInventoryService` uses, so the
    integration layer wires it identically:
    `on_rogue=lambda a: _notify(f"...: {a.extra_server}")`, and `/api/alerts`
    can merge `recent_alerts()` from both services into one list."""

    def __init__(self, collect: CollectFn | None = None,
                 known_servers=None, interval: float = 30.0,
                 alert_threshold: int = 2, max_alerts: int = 50,
                 max_tracked_extras: int = 50,
                 on_rogue: Callable[[DhcpRogueAlert], None] | None = None):
        self._collect = collect or make_collector()
        # `is not None` (not truthiness) -- an explicitly-passed EMPTY set means
        # "nothing is known-good yet" (strict: the very first observed server
        # is already "extra"), distinct from the default None which means
        # "auto-adopt whatever the first cycle observes" (see docstring).
        self._known: set[str] | None = set(known_servers) if known_servers is not None else None
        self._interval = interval
        self._alert_threshold = max(1, alert_threshold)
        self._max_alerts = max_alerts
        # Anti-flood cap (audit fix #5): bounds how many distinct extra ids
        # are tracked at once -- see module docstring.
        self._max_tracked = max(1, max_tracked_extras)
        self._on_rogue = on_rogue
        # Leaky N-of-M window per extra server id: a bounded deque of recent
        # present/absent bools (audit fix #5 -- see module docstring). Window
        # size is 2x the threshold (min 2) so a 50%-duty-cycle intermittent
        # rogue still accumulates `alert_threshold` "present" cycles within it.
        self._window_size = max(2 * self._alert_threshold, 2)
        self._windows: dict[str, deque] = {}
        self._alerted: set[str] = set()     # edge-triggered dedup (mirrors RogueAlert)
        self._alerts: list[DhcpRogueAlert] = []
        self._last_observed: set[str] = set()
        self._task: asyncio.Task | None = None
        # Tri-state honest-availability signal (panel-review finding #9/#17) --
        # same contract as sources.dhcp_fp.DHCPFingerprintCollector.available:
        # None = never attempted a cycle yet; True = the underlying collect()
        # ran without a permission/OS error (this cycle may still have seen
        # zero servers -- a normal quiet LAN); False = the raw UDP/68 bind
        # failed (e.g. non-root proot/container lacking CAP_NET_BIND_SERVICE).
        self.available: bool | None = None
        self.unavailable_reason: str | None = None

    def status(self) -> dict:
        return {
            "known_servers": sorted(self._known) if self._known else [],
            "observed_servers": sorted(self._last_observed),
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }

    def recent_alerts(self, limit: int = 50) -> list[DhcpRogueAlert]:
        return self._alerts[-limit:]

    async def check_once(self) -> set[str]:
        """Run a single collection cycle and fold it into the debounced
        state. Called by the background loop; also directly callable
        (deterministic) for tests. Never raises -- an exception from
        `collect` counts as "nothing observed this cycle", same tolerance
        rule as InternetMonitor.check_once."""
        try:
            raw = await self._collect()
        except (PermissionError, OSError) as exc:
            # The raw UDP/68 bind (the first thing DHCPCollector's transport
            # does) failed -- environment can't grant this, not a transient
            # collect failure. Recorded distinctly so callers can show an
            # honest "unavailable on this device" instead of a silent
            # zero-servers-observed cycle.
            _LOG.warning("dhcp monitor unavailable in this environment", exc_info=True)
            self.available = False
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"
            raw = {}
        except Exception:
            _LOG.warning("dhcp monitor collect failed", exc_info=True)
            raw = {}
        else:
            self.available = True
            self.unavailable_reason = None
        observed = set(raw.keys())
        self._last_observed = observed
        self._record(observed)
        return observed

    def _record(self, observed: set[str]) -> None:
        if self._known is None:
            # First-ever determination settles the baseline without alerting
            # -- there is no prior state to have "changed" from (mirrors
            # InternetMonitor/AwayMonitor's `first` guard).
            self._known = set(observed)
            return
        extras = observed - self._known

        # Anti-flood cap (audit fix #5): a burst of spoofed option-54 ids
        # must not grow the window dict unboundedly or crowd out an
        # already-accumulating genuine rogue's progress -- brand-new ids
        # beyond the cap are simply not tracked this cycle (already-tracked
        # ids are unaffected, so a slow-building genuine rogue keeps going).
        brand_new = sorted(extras - set(self._windows))
        room = max(0, self._max_tracked - len(self._windows))
        tracked_this_cycle = (extras & set(self._windows)) | set(brand_new[:room])

        newly_fired: list[str] = []
        for extra in set(self._windows) | tracked_this_cycle:
            present = extra in tracked_this_cycle
            window = self._windows.setdefault(extra, deque(maxlen=self._window_size))
            window.append(present)

            if extra in self._alerted:
                # Already-confirmed rogue: recovery is immediate -- ANY
                # single clean cycle forgets it (same debounce contract as
                # before), so a later reappearance is a fresh sighting.
                if not present:
                    del self._windows[extra]
                    self._alerted.discard(extra)
                continue

            if not present and sum(window) == 0:
                # Never crossed the threshold and has now fallen completely
                # out of the window -- forget it (bounds memory for a
                # one-off blip that never became a real rogue).
                del self._windows[extra]
                continue

            # Leaky N-of-M window (audit fix #5): a "present" cycle credits
            # the window; an "absent" cycle does NOT wipe it back to zero
            # (unlike the old strict consecutive-streak reset) -- so an
            # intermittent rogue (present every other cycle) still
            # accumulates toward the threshold instead of resetting on its
            # very first quiet cycle.
            if sum(window) >= self._alert_threshold:
                newly_fired.append(extra)

        if newly_fired:
            self._fire(sorted(newly_fired), observed)

    def _fire(self, extras: list[str], observed: set[str]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        known_tuple = tuple(sorted(self._known or ()))
        observed_tuple = tuple(sorted(observed))
        alerts = []
        for extra in extras:
            self._alerted.add(extra)
            alerts.append(DhcpRogueAlert(
                ts=ts, extra_server=extra,
                known_servers=known_tuple, observed_servers=observed_tuple,
            ))
        self._alerts.extend(alerts)
        if len(self._alerts) > self._max_alerts:    # bounded ring
            self._alerts = self._alerts[-self._max_alerts:]
        if self._on_rogue:
            # Coalesce into ONE callback per cycle even if multiple
            # server-ids crossed the threshold simultaneously (audit fix #5
            # anti-flood mitigation) -- the alert log above still records
            # one row per id for triage; only the push/ntfy fan-out is
            # capped at one call per cycle.
            combined = alerts[0] if len(alerts) == 1 else DhcpRogueAlert(
                ts=ts, extra_server=", ".join(extras),
                known_servers=known_tuple, observed_servers=observed_tuple,
            )
            try:
                self._on_rogue(combined)
            except Exception:
                _LOG.warning("dhcp monitor on_rogue callback failed", exc_info=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("dhcp monitor loop error")
            if self._interval:
                await asyncio.sleep(self._interval)

    async def start(self) -> None:
        """Spawn the periodic check task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the check task, cancel-safe (mirrors InternetMonitor
        teardown)."""
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
