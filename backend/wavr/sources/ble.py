from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from wavr.events import SensingEvent


def _norm(addr: str) -> str:
    """Normalize a BLE address to lowercase colon form (separator-agnostic:
    Windows/Linux use ':', some tools use '-'). macOS UUIDs pass through as-is
    apart from lowercasing."""
    return addr.strip().replace("-", ":").lower()


async def bleak_scan(duration: float = 5.0) -> dict[str, int]:
    """Default real scan: discover BLE advertisements via the host Bluetooth
    adapter for `duration` seconds and return {address: rssi_dbm} for every
    device seen. bleak is a lazy optional dep ([ble] extra) imported here — never
    at module top — so the module (and its tests) load with no bleak installed."""
    from bleak import BleakScanner  # lazy: optional [ble] extra

    # return_adv=True -> {address: (BLEDevice, AdvertisementData)}; adv.rssi is the
    # per-advertisement signal strength (BLEDevice.rssi is deprecated in bleak 0.22+).
    found = await BleakScanner.discover(timeout=duration, return_adv=True)
    return {_norm(addr): int(adv.rssi) for addr, (_dev, adv) in found.items()}


class BLESource:
    """House-level (or room-level) presence from BLE advertisements seen by the
    host Bluetooth adapter — no new hardware. Emits modality='ble'. A known
    address (config allowlist, person-labelled) counts as present only when its
    RSSI is at or above `rssi_min`; debounced by `grace` consecutive misses so a
    phone whose advertising briefly drops out doesn't flap the state.

    The `scan` seam is injectable (returns {address: rssi_dbm}) so tests run with
    no `bleak` installed; the default binds `bleak_scan` with `scan_window`."""

    def __init__(self, known: dict[str, str],
                 scan: Callable[[], Awaitable[dict[str, int]]] | None = None,
                 room: str = "casa", rssi_min: int = -80,
                 interval: float = 15.0, scan_window: float = 5.0,
                 grace: int = 2, present_confidence: float = 0.7):
        self._known = {_norm(a): p for a, p in known.items()}
        self._rssi_min = rssi_min
        self._scan_window = scan_window
        self._scan = scan or (lambda: bleak_scan(self._scan_window))
        self._room = room
        self._interval = interval
        self._grace = grace
        self._conf = present_confidence
        self._missed = grace + 1  # start "absent" until first sighting

    def _present_addrs(self, seen: dict[str, int]) -> set[str]:
        """Known addresses seen at or above the RSSI floor. Unknown addresses are
        ignored; a known address below the floor counts as not seen (too far)."""
        return {a for a, rssi in seen.items()
                if a in self._known and rssi >= self._rssi_min}

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            if self._known:
                try:
                    seen = await self._scan()
                except Exception:
                    logging.warning("BLESource scan failed", exc_info=True)
                    seen = {}
            else:
                seen = {}
            if self._present_addrs(seen):
                self._missed = 0
            else:
                self._missed += 1
            present = self._missed <= self._grace
            yield SensingEvent(
                room=self._room, modality="ble", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._conf if present else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
            )
            if self._interval:
                await asyncio.sleep(self._interval)
