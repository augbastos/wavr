"""No-motion ("stillness") detection for the elder-care guardian routine.

The honest complement of fall detection: FallDetector fires when someone is lying
outside a bed zone; StillnessDetector reports how long a room has been continuously
STILL despite being occupied -- so a routine can say "if nobody in the living room has
moved for 3 hours, notify me".

HONESTY IS THE WHOLE FEATURE (ADR-0003: Wavr is NOT a medical/safety device). We only
ever count "still" time when the room is CONFIDENTLY occupied by a MOTION-CAPABLE source
(mmWave/camera give per-target velocity; a network/BLE-only occupancy has NO motion
signal). If we cannot judge motion, `room_motionless` returns None -- the detector never
manufactures "hasn't moved", and NEVER produces false reassurance from a blind room. The
routine that consumes this must present it as best-effort awareness, not a safety promise.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.fall_detect import _as_utc

# A target whose speed is below this reads as effectively still (small posture shifts in a
# chair stay under it; standing up and walking goes well above it).
STILL_VELOCITY_MS = 0.15
# Continuous above-threshold movement longer than this ends a stillness episode (a real
# "they got up"); a briefer blip is tolerated so one noisy frame doesn't reset an hours-long
# clock. Longer than fall's flicker grace on purpose -- stillness is measured over hours.
STILL_MOVE_GRACE_S = 20.0


def room_motionless(occupied: bool, targets) -> bool | None:
    """None  -> cannot judge (not occupied, or no motion-capable source present): NEVER
                counted as still, NEVER as reassurance.
       True  -> occupied by a motion-capable source and every present target is still.
       False -> occupied and at least one target is moving."""
    if not occupied:
        return None
    vels = []
    for t in (targets or []):
        v = t.get("velocity") if isinstance(t, dict) else getattr(t, "velocity", None)
        if v is not None:
            vels.append(abs(float(v)))
    if not vels:
        return None   # occupancy without any velocity signal (e.g. network/BLE) -> unknowable
    return all(v < STILL_VELOCITY_MS for v in vels)


class StillnessDetector:
    """Per-room continuous-still timer. `update(room, still, ts)` returns the seconds the
    room has been continuously still (0.0 when not still / episode reset). Mirrors
    FallDetector's since/last-seen/flicker-grace shape, but reports elapsed instead of a
    one-shot latched alert -- the routines engine decides per-routine whether the elapsed
    has crossed that routine's `minutes` threshold, and an elapsed of 0.0 tells it the
    episode ended (so it can re-arm)."""

    def __init__(self, now_fn=None):
        self._now_fn = now_fn
        self._since: dict[str, datetime] = {}       # room -> ts the CURRENT still episode began
        self._last_still: dict[str, datetime] = {}  # room -> ts of the most recent still frame

    def _now_iso(self) -> str:
        return self._now_fn() if self._now_fn is not None else datetime.now(timezone.utc).isoformat()

    def update(self, room: str, still: bool | None, ts: str | None = None) -> float:
        ts = ts or self._now_iso()
        try:
            now = _as_utc(ts)
        except (TypeError, ValueError):
            return 0.0   # a malformed ts must never crash the fusion tick for this room
        if still is True:
            self._last_still[room] = now
            since = self._since.get(room)
            if since is None:
                self._since[room] = now
                return 0.0
            return (now - since).total_seconds()
        # still is False (moving) OR None (unknowable). A brief gap is tolerated so one noisy
        # frame doesn't reset an hours-long clock; a sustained gap ends the episode -- which,
        # for None, is the honesty guard: once we've lost the ability to judge, we stop
        # asserting stillness rather than freezing a stale "hasn't moved".
        last = self._last_still.get(room)
        if last is not None and (now - last).total_seconds() > STILL_MOVE_GRACE_S:
            self._since.pop(room, None)
            self._last_still.pop(room, None)
            return 0.0
        since = self._since.get(room)
        if since is None or last is None:
            return 0.0
        return (last - since).total_seconds()   # frozen at the last still frame during the grace

    def reset(self) -> None:
        self._since.clear()
        self._last_still.clear()
