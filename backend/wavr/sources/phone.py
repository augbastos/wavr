"""Paired-phone telemetry -> whole-home presence (mobile unification, blueprint step 4).

The fusion consumer for `POST /api/telemetry`. It drains the shared `TelemetryHub`
(the queue the authenticated telemetry handler `offer()`s readings onto) and folds
those readings into ONE coarse, house-level vote:

    SensingEvent(room="casa", modality="phone", presence=<any device seen recently>)

PRIVACY / CONTRACT invariants this source upholds (do not weaken):

  * The SensingEvent is house-level ONLY. `room` is HARDCODED "casa" -- a phone can
    never localize a person, and the wire payload never controls the room. `targets`
    is ALWAYS () for the same reason.

  * The event carries NO per-person identity. There is no `identity`/`person`/`rssi`
    field on SensingEvent and this source adds none -- the operator label and the raw
    rssi stay in the telemetry store keyed to `device_id`, never on the fused event
    that flows to RoomState -> the hub -> MCP. `whos_home()` is the ONLY place a label
    is resolved, and only from the DeviceStore (`get_label`) by device_id, for the UI's
    who's-home view -- it is derived on demand, not pushed onto the event.

  * Staleness is the SAME window fusion uses: entries older than `freshness_s`
    (default = fusion's `WAVR_SOURCE_FRESHNESS_S`) are pruned, so `presence` reflects
    "a registered device POSTed within the fusion freshness window". No bespoke timer.

Because `phone` weight (0.5) x `present_confidence` (0.8) = 0.4 sits BELOW the default
fusion threshold (0.5), a lone phone can corroborate who's-home but can never, on its
own, fabricate occupancy or a room -- see test_phone_source.py::T4.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent
# Reuse the SAME freshness constant fusion decays against -- never a second timer.
from wavr.fusion import _DEFAULT_FRESHNESS_S
from wavr.telemetry import TelemetryHub


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PhoneSensorSource:
    """Queue-fed, house-level presence from paired-phone telemetry. Modeled on
    NetworkSource (emits room='casa') but consumes a `TelemetryHub` instead of
    scanning: `presence=True` means >=1 registered device streamed a telemetry POST
    within `freshness_s`. Emits on every reading and on every `tick` of silence, so a
    phone that stops POSTing decays to absent even with no new readings."""

    def __init__(self, hub: TelemetryHub,
                 get_label: Callable[[str], str | None] | None = None,
                 room: str = "casa", tick: float = 15.0,
                 freshness_s: float | None = None,
                 present_confidence: float = 0.8,
                 now_fn: Callable[[], datetime] = _utcnow):
        self._hub = hub
        self._get_label = get_label
        self._room = room
        self._tick = tick
        self._freshness_s = _DEFAULT_FRESHNESS_S if freshness_s is None else freshness_s
        self._present_conf = present_confidence
        self._now = now_fn
        # device_id -> datetime it was last seen POSTing. The ONLY per-device state;
        # the operator label is never stored here, only resolved on demand.
        self._last_seen: dict[str, datetime] = {}

    def _fresh(self, seen: datetime, now: datetime) -> bool:
        return (now - seen).total_seconds() <= self._freshness_s

    def _prune(self, now: datetime) -> None:
        self._last_seen = {
            device_id: seen for device_id, seen in self._last_seen.items()
            if self._fresh(seen, now)
        }

    def whos_home(self) -> list[str]:
        """Sorted operator labels for devices seen within `freshness_s`, resolved from
        the DeviceStore (`get_label`) by device_id, falling back to the id when unnamed.
        Read-only (no state mutation): the who's-home view for the UI. The label is
        derived HERE and never rides the SensingEvent -- that is the privacy boundary."""
        now = self._now()
        labels: set[str] = set()
        for device_id, seen in self._last_seen.items():
            if not self._fresh(seen, now):
                continue
            label = self._get_label(device_id) if self._get_label else None
            labels.add(label or device_id)
        return sorted(labels)

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            try:
                reading = await asyncio.wait_for(self._hub.get(), timeout=self._tick)
            except asyncio.TimeoutError:
                # No new reading this tick -> re-evaluate presence against the clock so
                # a phone that went silent ages out even with an empty queue.
                reading = None
            now = self._now()
            if reading is not None:
                self._last_seen[reading.device_id] = now
            self._prune(now)
            present = bool(self._last_seen)
            yield SensingEvent(
                room=self._room, modality="phone", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._present_conf if present else 0.0,
                ts=now.isoformat(), targets=(),
            )
