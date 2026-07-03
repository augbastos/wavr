"""Wavr Net service -- makes the defensive LAN inventory a LIVE, running thing.

Wraps wavr.netinventory.scan_inventory in a periodic asyncio task: it re-scans
the LAN on a config-driven interval, keeps the latest resolved `list[Device]` in
memory, and raises an edge-triggered rogue-device alert the FIRST time a MAC not
on the known allowlist appears (a rescan never re-alerts the same MAC).

DEFENSIVE ONLY (ADR-0004): the scan reads the ARP cache of the LAN this host is
already on -- nothing else. Risky-port awareness stays OFF by default; it only
runs when WAVR_NET_PORTSCAN is explicitly enabled (wavr.netutils gate), and even
then it is connect-only, report-only.

Everything is in-memory (bounded alert ring) and the scan transport is injectable
(same seam as wavr.sources.network), so the whole service is mock-tested with
zero real network / zero hardware.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from wavr.netinventory import Device, scan_inventory
from wavr.netutils import annotate_risks, port_scan_enabled

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RogueAlert:
    """One rogue-device sighting: an unknown MAC that appeared on the LAN. Kept
    in-memory only. `ts` is ISO-8601 UTC of first sighting."""
    ts: str
    mac: str
    vendor: str
    ip: str | None = None
    device_type: str = "unknown"
    hostname: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "mac": self.mac,
            "vendor": self.vendor,
            "ip": self.ip,
            "device_type": self.device_type,
            "hostname": self.hostname,
        }


def _norm_macs(macs) -> set[str]:
    return {m.strip().replace("-", ":").lower() for m in (macs or ()) if m.strip()}


class NetworkInventoryService:
    """Periodically scans the LAN, holds the latest inventory, and logs
    edge-triggered rogue-device alerts.

    `scan` is the injectable ARP-text transport handed straight to
    scan_inventory (default: the real local ARP scan) -- inject a coroutine
    returning canned `arp -a` text to run without a network. `port_scan`
    overrides the WAVR_NET_PORTSCAN env gate (tests); leave None for the
    default (OFF) behaviour.
    """

    def __init__(self, known_macs=None,
                 scan: Callable[[], Awaitable[str]] | None = None,
                 interval: float = 30.0, max_alerts: int = 100,
                 port_scan: bool | None = None):
        self._known = _norm_macs(known_macs)
        self._scan = scan
        self._interval = interval
        self._max_alerts = max_alerts
        self._port_scan = port_scan
        self._inventory: list[Device] = []
        self._alerts: list[RogueAlert] = []
        self._alerted: set[str] = set()   # MACs already alerted (edge-triggered)
        self._task: asyncio.Task | None = None

    def _port_scan_on(self) -> bool:
        """OFF unless explicitly overridden (tests) or WAVR_NET_PORTSCAN is set."""
        return port_scan_enabled() if self._port_scan is None else self._port_scan

    async def scan_once(self) -> list[Device]:
        """Run a single scan: refresh the inventory and fold any new unknown MACs
        into the rogue-alert log. Called by the background loop; also directly
        callable (deterministic) for tests."""
        devices = await scan_inventory(known_macs=self._known, scan=self._scan)
        if self._port_scan_on():
            devices = await annotate_risks(devices)   # opt-in risk notes only
        self._inventory = devices
        self._record_rogues(devices)
        return devices

    def _record_rogues(self, devices) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        for d in devices:
            if d.known or d.mac in self._alerted:   # allowlisted or already seen
                continue
            self._alerted.add(d.mac)
            self._alerts.append(RogueAlert(
                ts=ts, mac=d.mac, vendor=d.vendor,
                ip=d.ip, device_type=d.device_type, hostname=d.hostname,
            ))
        if len(self._alerts) > self._max_alerts:    # bounded ring
            self._alerts = self._alerts[-self._max_alerts:]
        # Bound the edge-trigger dedup set: once it grows large (MAC randomization +
        # transient visitors accumulate forever), forget MACs no longer on the LAN.
        # A departed device re-alerts if it returns, which is fine.
        if len(self._alerted) > 4 * self._max_alerts:
            self._alerted &= {d.mac for d in devices}

    def latest_inventory(self) -> list[Device]:
        """The devices from the most recent scan (empty before the first)."""
        return list(self._inventory)

    def recent_alerts(self, limit: int = 50) -> list[RogueAlert]:
        """The most recent rogue-device alerts, newest last."""
        return self._alerts[-limit:]

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("network inventory scan failed")
            if self._interval:
                await asyncio.sleep(self._interval)

    async def start(self) -> None:
        """Spawn the periodic scan task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the scan task, cancel-safe (mirrors SourceManager teardown)."""
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
