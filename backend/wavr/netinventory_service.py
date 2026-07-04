"""Wavr Net service -- makes the defensive LAN inventory a LIVE, running thing.

Wraps wavr.netinventory.scan_inventory in a periodic asyncio task: it re-scans
the LAN on a config-driven interval, keeps the latest resolved `list[Device]` in
memory, and raises an edge-triggered rogue-device alert the FIRST time a MAC not
on the known allowlist appears (a rescan never re-alerts the same MAC).

DEFENSIVE ONLY (ADR-0004): the scan reads the ARP cache of the LAN this host is
already on -- nothing else. Risky-port awareness stays OFF by default; it only
runs when WAVR_NET_PORTSCAN is explicitly enabled (wavr.netutils gate), and even
then it is connect-only, report-only. OPERATOR WARNING: when on, it connect-scans
EVERY host the ARP inventory discovers on the /24, which on a shared/guest
subnet may include hosts the operator doesn't own -- see wavr.netutils
port_scan_enabled()'s docstring. WAVR_NET_PORTSCAN_SCOPE=known narrows that pass
to the known-MAC allowlist only (port_scan_known_only_enabled()).

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

from wavr.netinventory import Device, apply_recognition, scan_inventory
from wavr.netutils import annotate_ports, port_scan_enabled, port_scan_known_only_enabled

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RogueAlert:
    """One rogue-device sighting: an unknown MAC that appeared on the LAN. Kept
    in-memory only. `ts` is ISO-8601 UTC of first sighting. `device_type` /
    `type_confidence` carry the recog fusion verdict (taxonomy value +
    high/medium/low) so alert rows can render the same identity as inventory."""
    ts: str
    mac: str
    vendor: str
    ip: str | None = None
    device_type: str = "unknown"
    hostname: str | None = None
    type_confidence: str = "low"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "mac": self.mac,
            "vendor": self.vendor,
            "ip": self.ip,
            "device_type": self.device_type,
            "hostname": self.hostname,
            "type_confidence": self.type_confidence,
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
    default (OFF) behaviour. `port_scan_known_only` overrides
    WAVR_NET_PORTSCAN_SCOPE=known (tests); leave None for the default (OFF --
    the port pass covers every discovered host, unchanged behaviour); when on,
    only devices already on the known-MAC allowlist are connect-scanned (L3
    audit fix: bounds the pass's footprint on a shared/guest subnet).
    `on_rogue` is an OPTIONAL injectable callback
    `(RogueAlert) -> None` fired at the same edge-triggered moment a new
    rogue MAC is recorded (once per MAC, same rule as the alert log) -- used
    by the opt-in ntfy notifier. Exceptions from it are caught and logged,
    never propagated (a broken callback must not break scanning).

    `device_meta` is an OPTIONAL wavr.device_meta.DeviceMeta -- when given,
    every device in a scan calls `device_meta.seen(mac)` (Feature A: persisted
    first-seen/last-seen). A persistence failure (e.g. disk issue) is caught
    and logged, same tolerance as `on_rogue` -- it must never break scanning.
    """

    def __init__(self, known_macs=None,
                 scan: Callable[[], Awaitable[str]] | None = None,
                 interval: float = 30.0, max_alerts: int = 100,
                 port_scan: bool | None = None,
                 port_scan_known_only: bool | None = None,
                 on_rogue: Callable[[RogueAlert], None] | None = None,
                 device_meta=None, port_probe=None):
        self._known = _norm_macs(known_macs)
        self._scan = scan
        self._interval = interval
        self._max_alerts = max_alerts
        self._port_scan = port_scan
        # L3 audit fix: optionally scope the opt-in port pass to the known-MAC
        # allowlist so a shared/guest-subnet neighbor is never connect-scanned.
        # None (default) -> read WAVR_NET_PORTSCAN_SCOPE (off unless "known").
        self._port_scan_known_only = port_scan_known_only
        self._port_probe = port_probe   # injectable TCP-connect probe (tests)
        self._on_rogue = on_rogue
        self._device_meta = device_meta
        self._inventory: list[Device] = []
        self._alerts: list[RogueAlert] = []
        self._alerted: set[str] = set()   # MACs already alerted (edge-triggered)
        self._task: asyncio.Task | None = None

    def _port_scan_on(self) -> bool:
        """OFF unless explicitly overridden (tests) or WAVR_NET_PORTSCAN is set."""
        return port_scan_enabled() if self._port_scan is None else self._port_scan

    def _port_scan_known_only_on(self) -> bool:
        """OFF unless explicitly overridden (tests) or WAVR_NET_PORTSCAN_SCOPE=known."""
        return (port_scan_known_only_enabled() if self._port_scan_known_only is None
                else self._port_scan_known_only)

    async def scan_once(self) -> list[Device]:
        """Run a single scan: refresh the inventory and fold any new unknown MACs
        into the rogue-alert log. Called by the background loop; also directly
        callable (deterministic) for tests."""
        pins = self._type_pins()
        devices = await scan_inventory(known_macs=self._known, scan=self._scan,
                                       pins=pins)
        if self._port_scan_on():
            # Opt-in connect-only pass: risk notes + open_ports, then re-fuse
            # identity so port-derived type hints fold into device_type.
            if self._port_scan_known_only_on():
                # Scoped mode (L3 audit fix): only connect-scan devices already on
                # the known-MAC allowlist -- an unknown/rogue host on a shared
                # subnet is left untouched by the port pass (still inventoried and
                # still alerted on, just never connect-scanned).
                known_devs = [d for d in devices if d.known]
                scanned = {d.mac: d for d in await annotate_ports(known_devs, probe=self._port_probe)}
                devices = [scanned.get(d.mac, d) for d in devices]
            else:
                devices = await annotate_ports(devices, probe=self._port_probe)
            devices = [apply_recognition(d, pin=pins.get(d.mac)) for d in devices]
        self._inventory = devices
        self._record_rogues(devices)
        self._record_seen(devices)
        return devices

    def _type_pins(self) -> dict:
        """User device-type pins (mac -> taxonomy value) from device_meta --
        the highest-precedence recog signal. Tolerant, same rule as `seen`:
        a broken/legacy store must never break scanning."""
        if not self._device_meta:
            return {}
        try:
            return self._device_meta.type_pins()
        except Exception:
            _LOG.warning("device_meta.type_pins failed", exc_info=True)
            return {}

    def _record_seen(self, devices) -> None:
        # Feature A: persist first-seen/last-seen for every observed MAC, not
        # just rogue ones. Tolerant, same rule as the on_rogue callback -- a
        # persistence failure must never break scanning.
        if not self._device_meta:
            return
        for d in devices:
            try:
                self._device_meta.seen(d.mac)
            except Exception:
                _LOG.warning("device_meta.seen failed for %s", d.mac, exc_info=True)

    def _record_rogues(self, devices) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        for d in devices:
            if d.known or d.mac in self._alerted:   # allowlisted or already seen
                continue
            self._alerted.add(d.mac)
            alert = RogueAlert(
                ts=ts, mac=d.mac, vendor=d.vendor,
                ip=d.ip, device_type=d.device_type, hostname=d.hostname,
                type_confidence=d.type_confidence,
            )
            self._alerts.append(alert)
            if self._on_rogue:
                try:
                    self._on_rogue(alert)
                except Exception:
                    _LOG.warning("on_rogue callback failed", exc_info=True)
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
