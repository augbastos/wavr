from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from wavr.events import Identity, SensingEvent


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
                 grace: int = 2, present_confidence: float = 0.7,
                 emit_identity: bool = False,
                 known_provider: Callable[[], dict[str, str]] | None = None):
        self._known = {_norm(a): p for a, p in known.items()}
        # LIVE consent registry seam: when a provider is injected, the known map is
        # re-read at the START of every scan cycle instead of frozen at construction,
        # so a device REGISTERED (or opted-out) via the identity registry becomes (or
        # stops being) a signal on the very next cycle -- no server restart. Default
        # None -> byte-identical to before (the frozen env allowlist).
        self._known_provider = known_provider
        self._rssi_min = rssi_min
        self._scan_window = scan_window
        self._scan = scan or (lambda: bleak_scan(self._scan_window))
        self._room = room
        self._interval = interval
        self._grace = grace
        self._conf = present_confidence
        # Non-biometric "who is home": attach the operator-configured person label
        # only when explicitly enabled. Default OFF -> byte-identical to before
        # (no Identity is ever created), so no PII enters the event/state/DB path.
        self._emit_identity = emit_identity
        self._missed = grace + 1  # start "absent" until first sighting

    def _current_known(self) -> dict[str, str]:
        """The known {address: person} map for THIS cycle: the live provider result
        (normalized) when one is injected, else the frozen construction map."""
        if self._known_provider is not None:
            return {_norm(a): p for a, p in self._known_provider().items()}
        return self._known

    def _present_addrs(self, seen: dict[str, int], known: dict[str, str]) -> set[str]:
        """Known addresses seen at or above the RSSI floor. Unknown addresses are
        ignored; a known address below the floor counts as not seen (too far)."""
        return {a for a, rssi in seen.items()
                if a in known and rssi >= self._rssi_min}

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            known = self._current_known()
            if known:
                try:
                    seen = await self._scan()
                except Exception:
                    logging.warning("BLESource scan failed", exc_info=True)
                    seen = {}
            else:
                seen = {}
            present_addrs = self._present_addrs(seen, known)
            if present_addrs:
                self._missed = 0
            else:
                self._missed += 1
            present = self._missed <= self._grace
            # House-level "who is home": name the currently-seen known devices only
            # when the gate is on and they carry a non-empty person label. During a
            # grace-held miss (present True, nothing seen this cycle) the list is
            # empty — honest: we hold presence but can't name who right now.
            identities: tuple = ()
            if present and self._emit_identity:
                identities = tuple(
                    Identity(person=known[a], source="ble", rssi=seen[a])
                    for a in sorted(present_addrs) if known.get(a)
                )
            yield SensingEvent(
                room=self._room, modality="ble", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._conf if present else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
                identities=identities,
            )
            if self._interval:
                await asyncio.sleep(self._interval)
