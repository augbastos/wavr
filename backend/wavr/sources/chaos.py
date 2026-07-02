from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent, Target

# Deterministic clock: chaos scenarios must be reproducible for demos and tests,
# so — unlike the ambient SimulatedSource — we never read the wall clock.
_BASE = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

# Camera "flicker" frames are low-quality detections: weak confidence even though
# the camera modality itself carries a heavy trust weight. Kept as a constant so
# the fusion assertions in the tests aren't magic-number brittle.
FLICKER_CONF = 0.6


def _ts(step: int) -> str:
    """Monotonic ISO-8601 UTC timestamp, one second per step."""
    return (_BASE + timedelta(seconds=step)).isoformat()


def _ev(step: int, room: str, modality: str, presence: bool, confidence: float, *,
        motion: float = 0.0, breathing_bpm: float | None = None,
        heart_bpm: float | None = None, targets: tuple[Target, ...] = ()) -> SensingEvent:
    """Build one SensingEvent. Mirrors the real sources: an absent source reports
    zero confidence (network.py / camera.py both zero it out when not present)."""
    return SensingEvent(
        room=room, modality=modality, presence=presence, motion=motion,
        breathing_bpm=breathing_bpm, heart_bpm=heart_bpm,
        confidence=confidence if presence else 0.0,
        ts=_ts(step), targets=tuple(targets),
    )


# ---- Scenario scripts (pure, deterministic; each returns an ordered event list) ----

def wifi_drop(steps: int = 9) -> list[SensingEvent]:
    """The house network-presence source collapses to zero mid-run. The room must
    fall vacant ONLY once no trusted source still reports presence: while the
    camera holds presence the fused state stays occupied, proving a single
    dropout can't blind the room. Three equal phases:
      1. network + camera present   -> occupied
      2. network dropped, camera on  -> STILL occupied (camera is trusted)
      3. both absent                 -> vacant."""
    room = "sala"
    p = max(1, steps // 3)
    out: list[SensingEvent] = []
    for s in range(3 * p):
        net_present = s < p
        cam_present = s < 2 * p
        out.append(_ev(s, room, "network", net_present, 0.8))
        target = (Target(id=1, x=2.0, y=1.5, posture="standing",
                         velocity=0.0, confidence=0.9),) if cam_present else ()
        out.append(_ev(s, room, "camera", cam_present, 0.9, targets=target))
    return out


def camera_flicker(steps: int = 8) -> list[SensingEvent]:
    """A lone camera alternates false-positive / false-negative presence. The
    fusion math (confidence = agreement × strength) must never let this lone,
    weak-confidence source read 100%: a single present source has agreement 1.0,
    so only the `× strength` term (weight × its own confidence) keeps it honest."""
    room = "quarto"
    out: list[SensingEvent] = []
    for s in range(steps):
        present = (s % 2 == 0)   # even ticks = false positive, odd = false negative
        target = (Target(id=1, x=None, y=None, posture="standing",
                         confidence=FLICKER_CONF),) if present else ()
        out.append(_ev(s, room, "camera", present, FLICKER_CONF, targets=target))
    return out


def multi_target(steps: int = 6, n_targets: int = 7) -> list[SensingEvent]:
    """6-8 people cross one room at once (single camera, many targets). Every
    target must flow through fusion into RoomState.targets — and NEVER be
    persisted (asserted in the tests). Targets march left->right across a 4x3 m
    room over the run."""
    if not 6 <= n_targets <= 8:
        raise ValueError("multi-target expects 6-8 simultaneous targets")
    room = "sala"
    width = 4.0
    out: list[SensingEvent] = []
    for s in range(steps):
        frac = s / max(1, steps - 1)
        targets = tuple(
            Target(id=i + 1,
                   x=round(0.2 + frac * (width - 0.4), 2),   # all crossing L->R
                   y=round(0.4 + 0.35 * i, 2),               # lanes, stays < 3 m
                   posture="walking", velocity=0.6, confidence=0.9)
            for i in range(n_targets)
        )
        out.append(_ev(s, room, "camera", True, 0.9, targets=targets))
    return out


def fall(steps: int = 6) -> list[SensingEvent]:
    """One person transitions standing -> lying while micro-motion collapses.
    Two co-located sources: wifi_csi carries body motion + breathing, camera
    carries the posture target. The room must STAY occupied through the fall
    (a fallen person is still present) and RoomState must reflect the lying
    posture and the dropped motion — never read vacant just because motion fell."""
    room = "quarto"
    fall_at = max(1, steps // 2)
    out: list[SensingEvent] = []
    for s in range(steps):
        fallen = s >= fall_at
        posture = "lying" if fallen else "standing"
        velocity = 0.0 if fallen else 0.3
        motion = 0.2 if fallen else 6.0          # body motion collapses post-fall
        # wifi_csi: person still present — micro-motion + breathing keep it alive.
        out.append(_ev(s, room, "wifi_csi", True, 0.9, motion=motion,
                       breathing_bpm=13.0, heart_bpm=64.0))
        # camera: posture target (velocity doubles as the target's motion proxy).
        target = Target(id=1, x=2.0, y=1.5, posture=posture,
                        velocity=velocity, confidence=0.9)
        out.append(_ev(s, room, "camera", True, 0.9, targets=(target,)))
    return out


SCENARIOS: dict[str, Callable[..., list[SensingEvent]]] = {
    "wifi-drop": wifi_drop,
    "camera-flicker": camera_flicker,
    "multi-target": multi_target,
    "fall": fall,
}


def scenario_events(name: str, **params) -> list[SensingEvent]:
    """Build a named scenario's scripted event list. Raises on an unknown name."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown chaos scenario: {name!r} (have {sorted(SCENARIOS)})")
    return SCENARIOS[name](**params)


class ChaosSource:
    """A SensorSource that replays a deterministic 'chaos' scenario to stress the
    FusionEngine. Where SimulatedSource is a plausible ambient stream, each chaos
    scenario is a finite, scripted, adversarial sequence — a network dropout, a
    flickering camera, a crowd crossing a room, or a fall — so the fusion math can
    be demonstrated (and tested) under stress. No RNG: fully reproducible. Finite
    by default (SourceManager tolerates self-terminating sources); pass loop=True
    to stream it continuously for a live demo."""

    def __init__(self, scenario: str, interval: float = 0.0,
                 loop: bool = False, **params):
        self._script = scenario_events(scenario, **params)   # validates name eagerly
        self.scenario = scenario
        self._interval = interval
        self._loop = loop

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            for e in self._script:
                yield e
                if self._interval:
                    await asyncio.sleep(self._interval)
            if not self._loop:
                return
