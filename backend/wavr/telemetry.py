"""Phone telemetry ingest surface (mobile unification, blueprint §4, step 2).

This module owns the *boundary* between the authenticated `POST /api/telemetry`
handler (producer) and the future `PhoneSensorSource` fusion source (consumer,
step 4 -- owned by another agent). It defines four things and nothing else:

  * `TelemetryPayload` / `SensorBlock` -- pydantic v2 models that validate the
    `telemetry.ts` wire shape AT THE BOUNDARY. A malformed body fails pydantic ->
    FastAPI returns 422, never a 500. Array fields are length-bounded so a hostile
    client cannot force unbounded allocation.

  * `TelemetryReading` -- a normalized, server-stamped reading. Its `device_id` is
    set by the handler from the CALLER'S OWN authenticated token, NEVER from the
    body's `device` field -- a phone can never name itself as another device.

  * `TelemetryHub` -- a bounded asyncio.Queue the handler `offer()`s readings onto
    and `PhoneSensorSource` `await get()`s from. QUEUE INTERFACE CONTRACT for step 4:
        hub = app.state.telemetry_hub          # module-level per-app instance
        reading = await hub.get()              # consumer coroutine awaits here
    `offer()` is non-blocking and DROP-OLDEST on a full queue, so a burst of POSTs
    can never block the event loop nor grow memory without bound (freshest presence
    wins -- correct for a "who's home" signal).

  * `PerDeviceRateLimiter` -- a token bucket keyed per device_id (injectable clock).
    One bucket per paired device; keys come only from verified tokens, so the key
    space is bounded by the paired-device count (no key-explosion DoS).

Nothing here feeds fusion yet -- that wiring is step 4. This is the seam so the two
steps can be built independently against a frozen contract.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

# accel/gyro/mag are 3-axis vectors; pressure/light are small batched sample arrays.
# Cap every array so a hostile payload can't force a large allocation per request.
_MAX_AXIS = 8
_MAX_SAMPLES = 64


class SensorBlock(BaseModel):
    """The optional `sensors` sub-object. Every field is optional -- an absent
    sensor (denied permission, no baro/light hardware) is simply omitted, exactly
    as `sensors.ts` emits. Extra keys are rejected so junk can't ride along."""

    # allow_inf_nan=False: reject NaN/+Inf/-Inf on EVERY float here, INCLUDING the
    # list-element floats (accel/gyro/mag/pressure/light). pydantic's default
    # allow_inf_nan=True would accept `{"accel":[Infinity, NaN]}` (Starlette's json.loads
    # parses those literals) and that non-finite garbage would be enqueued onto the fusion
    # hub -- a poison read. Setting it on the model config makes each element a
    # `finite_number`, so a non-finite sensor sample fails validation (422) at the boundary
    # and nothing non-finite ever reaches PhoneSensorSource.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    accel: list[float] | None = Field(default=None, max_length=_MAX_AXIS)
    gyro: list[float] | None = Field(default=None, max_length=_MAX_AXIS)
    mag: list[float] | None = Field(default=None, max_length=_MAX_AXIS)
    pressure: list[float] | None = Field(default=None, max_length=_MAX_SAMPLES)
    light: list[float] | None = Field(default=None, max_length=_MAX_SAMPLES)

    def present(self) -> dict[str, list[float]]:
        """Only the sensors that were actually reported (drop the None fields)."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class TelemetryPayload(BaseModel):
    """The `telemetry.ts` wire contract (blueprint §Ground-truth):
    `{device, sensors:{accel?,gyro?,mag?,pressure?,light?}, battery_pct, charging,
      rssi, ssid, bssid}`.

    `device` is accepted for wire-compatibility but is NEVER used for identity --
    the handler keys the stored reading to the caller's own token-derived device_id.
    Ranges are loose sanity bounds (reject obvious garbage, tolerate real hardware).
    Extra top-level keys are rejected: the contract is frozen for Phase 1."""

    # allow_inf_nan=False: reject NaN/+Inf/-Inf on battery_pct/rssi. Starlette's json.loads
    # accepts the `NaN`/`Infinity` literals and `1e400` parses to +Inf; without this a
    # non-finite battery_pct/rssi would either be accepted or (with ge/le) rejected in a way
    # whose error render still echoes the non-finite value -> 500. Belt with braces: the
    # JSON-safe validation-error handler in app.py also sanitizes the echoed input.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    device: str | None = Field(default=None, max_length=128)   # IGNORED for identity
    sensors: SensorBlock | None = None
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    charging: bool | None = None
    rssi: int | None = Field(default=None, ge=-200, le=100)
    ssid: str | None = Field(default=None, max_length=64)
    bssid: str | None = Field(default=None, max_length=64)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TelemetryReading:
    """A normalized reading, keyed to the CALLER'S own device_id (never the body).
    This is the object PhoneSensorSource (step 4) consumes off the hub and maps to a
    whole-home SensingEvent(room='casa', modality='phone', ...)."""

    device_id: str
    ts: str
    battery_pct: float | None = None
    charging: bool | None = None
    rssi: int | None = None
    ssid: str | None = None
    bssid: str | None = None
    sensors: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: TelemetryPayload, device_id: str,
                     now_fn: Callable[[], str] = _utcnow_iso) -> "TelemetryReading":
        return cls(
            device_id=device_id,                       # <- token identity, authoritative
            ts=now_fn(),
            battery_pct=payload.battery_pct,
            charging=payload.charging,
            rssi=payload.rssi,
            ssid=payload.ssid,
            bssid=payload.bssid,
            sensors=payload.sensors.present() if payload.sensors else {},
        )

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "ts": self.ts,
            "battery_pct": self.battery_pct,
            "charging": self.charging,
            "rssi": self.rssi,
            "ssid": self.ssid,
            "bssid": self.bssid,
            "sensors": dict(self.sensors),
        }


class TelemetryHub:
    """Bounded producer/consumer seam between the telemetry handler and the fusion
    source. `offer()` (producer, sync, non-blocking) drops the OLDEST reading when the
    queue is full so a POST burst never blocks the event loop or grows memory. `get()`
    (consumer, async) awaits the next reading."""

    def __init__(self, maxsize: int = 256):
        self._q: asyncio.Queue[TelemetryReading] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0

    def offer(self, reading: TelemetryReading) -> bool:
        """Enqueue without blocking. Returns True if stored cleanly, False if a stale
        reading had to be dropped to make room (still stores the new one)."""
        try:
            self._q.put_nowait(reading)
            return True
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()                   # drop oldest -- freshest wins
                self._dropped += 1
            except asyncio.QueueEmpty:                 # pragma: no cover - race guard
                pass
            try:
                self._q.put_nowait(reading)
            except asyncio.QueueFull:                  # pragma: no cover - race guard
                pass
            return False

    async def get(self) -> TelemetryReading:
        return await self._q.get()

    def qsize(self) -> int:
        return self._q.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped


class PerDeviceRateLimiter:
    """Token bucket, one bucket per device_id. `capacity` is the burst size; the bucket
    refills at `refill_per_sec` tokens/second. `allow()` consumes one token and returns
    True, or False (-> caller returns 429) when the bucket is empty. The clock is
    injectable so tests are deterministic. Buckets are keyed by device_id only, which
    is derived from a verified token, so the dict is bounded by the paired-device count."""

    def __init__(self, capacity: float = 60.0, refill_per_sec: float = 2.0,
                 clock: Callable[[], float] = time.monotonic):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._cap = float(capacity)
        self._refill = float(refill_per_sec)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_ts)

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._cap, now))
        tokens = min(self._cap, tokens + max(0.0, now - last) * self._refill)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False
