from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from datetime import datetime, timezone
from typing import AsyncIterator

from wavr.events import SensingEvent, Target

log = logging.getLogger(__name__)

_HEADER = b"\xaa\xff\x03\x00"
_TAIL = b"\x55\xcc"
_WALK_MS = 0.25          # |velocity| above this = "walking"


def _signmag(raw: int) -> int:
    return (raw & 0x7FFF) if raw & 0x8000 else -raw


def parse_ld2450_frame(frame: bytes) -> list[Target]:
    """One 30-byte LD2450 report frame -> decoded targets (meters, m/s)."""
    if len(frame) != 30 or not frame.startswith(_HEADER) or not frame.endswith(_TAIL):
        return []
    out: list[Target] = []
    for i in range(3):
        slot = frame[4 + i * 8: 12 + i * 8]
        if slot == b"\x00" * 8:
            continue
        rx, ry, rs, _res = struct.unpack("<HHHH", slot)
        vel = _signmag(rs) / 100.0                  # cm/s -> m/s
        out.append(Target(
            id=i + 1,
            x=_signmag(rx) / 1000.0,               # mm -> m, FROM THE RADAR
            y=_signmag(ry) / 1000.0,
            velocity=abs(vel),
            posture="walking" if abs(vel) > _WALK_MS else None,
            confidence=0.9,
            # The LD2450 reports relative to ITSELF: origin at the module, its
            # own axes. This used to be emitted as though it were room-local,
            # so the map drew the person wherever they happened to be relative
            # to the radar and called it position-level precision. Declaring the
            # frame is what lets `spatial_frames` either place it properly (when
            # the radar's mount is known) or honestly drop the coordinate.
            frame="sensor",
        ))
    return out


async def _serial_frames(port: str) -> AsyncIterator[bytes]:
    """Default transport: read LD2450 frames from a local serial port.
    pyserial is a lazy optional dep ([mmwave] extra)."""
    import serial                                    # lazy: optional [mmwave] extra

    leftover = b""

    def _read_frame(s) -> bytes:
        nonlocal leftover
        buf = leftover
        while True:
            buf += s.read(64)
            i = buf.find(_HEADER)
            if i >= 0 and len(buf) >= i + 30:
                leftover = buf[i + 30:]
                return buf[i: i + 30]
            if len(buf) > 4096:
                buf = buf[-64:]

    # Opening (and closing) the port is a blocking syscall -- offload both off the
    # event loop, same class of fix as the RTSP capture open in sources/camera.py.
    s = await asyncio.to_thread(serial.Serial, port, 256000, timeout=1)
    try:
        while True:
            yield await asyncio.to_thread(_read_frame, s)
    finally:
        await asyncio.to_thread(s.close)


class MmWaveSource:
    """Room-level position radar from an HLK-LD2450 (serial today; the frames
    seam takes any async byte-frame generator — TCP/MQTT transports later)."""

    def __init__(self, room: str, port: str, frames: AsyncIterator[bytes] | None = None,
                 interval: float = 0.2, reconnect_delay: float = 3.0):
        self._room = room
        self._port = port
        # The wired radar has no name of its own, so it takes the SAME id
        # `sensor_coverage._host_rows` gives it. Two surfaces describing one
        # physical sensor must agree on what it is called, or coverage reports
        # the kitchen radar healthy while reliability has never heard of it.
        self._sensor_id = f"wired-radar-{room or 'unassigned'}"
        self._frames = frames
        self._interval = interval
        self._reconnect_delay = reconnect_delay

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            frames = self._frames if self._frames is not None else _serial_frames(self._port)
            try:
                async with contextlib.aclosing(frames) as stream:
                    async for raw in stream:
                        targets = tuple(parse_ld2450_frame(raw))
                        speed = max((t.velocity or 0.0 for t in targets), default=0.0)
                        yield SensingEvent(
                            sensor_id=self._sensor_id,
                            room=self._room, modality="mmwave",
                            presence=bool(targets), motion=speed,
                            breathing_bpm=None, heart_bpm=None,
                            confidence=0.9 if targets else 0.0,
                            ts=datetime.now(timezone.utc).isoformat(),
                            targets=targets,
                            count=len(targets),   # discrete radar targets = person count
                        )
                        if self._interval:
                            await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("MmWaveSource(%s) error; reconnecting", self._room, exc_info=True)
            if self._reconnect_delay:
                await asyncio.sleep(self._reconnect_delay)
