"""Frigate's person counts as evidence, for a household that already runs it.

Frigate (github.com/blakeblackshear/frigate, MIT) is an NVR that runs object
detection on its own cameras. When somebody already has it, running Wavr's own
detector on the same streams would pay twice for one answer, so Wavr can read
Frigate's conclusion instead: how many people it currently tracks per camera or
zone, published on MQTT.

    frigate/available              online | stopped | offline   (LWT)
    frigate/<camera_or_zone>/person        integer count, published on change
    frigate/<camera>/detect/state          ON | OFF

(Topic shapes from Frigate 0.18 and its MQTT docs; see docs/EXTERNAL-PROVIDERS.md.)

## What is refused, and why

**No image ever enters Wavr.** Frigate publishes a RETAINED JPEG on
`frigate/<camera>/person/snapshot`, speech transcriptions, face names and plate
numbers on other topics. Wavr subscribes to the exact count, detect-state and
availability topics of the names an operator mapped -- never a wildcard, which
`mqtt_subscriber` refuses anyway -- so none of that is ever received.

**The room comes from Wavr's mapping.** A Frigate camera or zone name an operator
did not map is not subscribed to, and cannot conjure a room.

**A count nobody has published is unknown, not zero.** Count topics are not
retained and are published only when they change. A Wavr that connects while
somebody sits in the kitchen hears nothing until that changes, and says nothing
until then -- rather than "empty".

**Zero is trusted only where Wavr can tell detection was running.** `camera` is
a modality whose "nobody here" LOWERS a room's confidence (fusion's trusted
absence), because a working camera that sees an empty room is real evidence. A
Frigate camera with detection switched off still reports its last count, so a
zero is passed on as absence only for a camera whose `detect/state` is known to
be ON while Frigate is `online`. A zone's detection state is not published, so a
zone only ever contributes presence.

**Held counts are re-asserted, stamped now, while Frigate is online.** A count of
two published ten minutes ago, with Frigate still online and nothing changed, is
Frigate's answer NOW. Stamping it with the publish time would let fusion age a
room with two people in it down to nothing. The moment Frigate reports itself
`offline` or `stopped`, re-assertion stops and the evidence decays.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent
from wavr.mqtt_subscriber import CONNECTED, DISCONNECTED, tracked_events
from wavr.providers import CONF_NONE, KIND_DERIVED, REACH_LAN, describe

_LOG = logging.getLogger(__name__)

DEFAULT_PREFIX = "frigate"
LABEL = "person"
# Declared, not measured. A count passed Frigate's own threshold (0.7 by
# default), so it is a confident detection -- but Wavr cannot see the frame,
# the mask or the zone geometry, so it sits below Wavr's own camera.
DEFAULT_CONFIDENCE = 0.75
REASSERT_S = 10.0
# A count cannot be negative and a household camera does not see thousands.
_MAX_COUNT = 1000

_FORBIDDEN = ("/", "+", "#")


class FrigateConfigError(ValueError):
    """A configuration that would subscribe to more than the operator named."""


@dataclass(frozen=True)
class FrigateConfig:
    """Frigate camera-or-zone name -> Wavr room, as an operator mapped it."""

    names: dict = field(default_factory=dict)
    prefix: str = DEFAULT_PREFIX

    def __post_init__(self):
        for key in [self.prefix, *self.names]:
            if not key or any(c in key for c in _FORBIDDEN) or len(key) > 200:
                raise FrigateConfigError(
                    f"{key!r} cannot be a Frigate topic prefix or camera/zone name")
        for bad in ("available", "events", "reviews", "stats", "camera_activity",
                    "tracked_object_update", "onConnect"):
            if bad in self.names:
                raise FrigateConfigError(f"{bad!r} is a Frigate topic, not a camera")

    @property
    def configured(self) -> bool:
        return bool(self.names)

    def topics(self) -> list[str]:
        p = self.prefix
        out = [f"{p}/available"]
        for name in sorted(self.names):
            out += [f"{p}/{name}/{LABEL}", f"{p}/{name}/detect/state"]
        return out


def descriptor():
    return describe(
        "frigate", "Frigate person counts", KIND_DERIVED, REACH_LAN,
        modality="camera",
        observes=("presence", "count"),
        confidence_semantics=CONF_NONE,
        requires=("MQTT broker address", "a Wavr room for each Frigate camera or zone"),
        notes=("How many people an existing Frigate install sees per camera or "
               "zone. Counts only: no image, clip, face or plate ever reaches "
               "Wavr."))


class FrigateTracker:
    """Messages -> evidence, with no I/O (see `EspresenseTracker`)."""

    def __init__(self, cfg: FrigateConfig):
        self.cfg = cfg
        self._available: str | None = None
        self._counts: dict[str, int] = {}          # name -> last published count
        self._detect: dict[str, bool] = {}         # camera name -> detect ON?
        self.dropped = {"unknown_name": 0, "malformed": 0}

    def on_message(self, topic: str, payload: bytes, now: datetime) -> list[SensingEvent]:
        if topic == DISCONNECTED:
            # The broker is gone, so nothing Wavr holds is current any more.
            self._available = None
            self._counts.clear()
            self._detect.clear()
            return []
        if topic == CONNECTED:
            return []
        p = self.cfg.prefix
        text = payload.decode("utf-8", "replace").strip()
        if topic == f"{p}/available":
            self._available = text.lower()
            if self._available != "online":
                # Stopped or gone: the last counts describe a Frigate that is
                # no longer looking. Forget them; emit nothing.
                self._counts.clear()
                self._detect.clear()
            return []
        levels = topic.split("/")
        if len(levels) < 3 or levels[0] != p:
            return []
        name = levels[1]
        if name not in self.cfg.names:
            self.dropped["unknown_name"] += 1
            return []
        if levels[2:] == ["detect", "state"]:
            if text.upper() in ("ON", "OFF"):
                self._detect[name] = text.upper() == "ON"
            else:
                self.dropped["malformed"] += 1
            return []
        if levels[2:] != [LABEL]:
            return []
        try:
            count = int(text)
        except ValueError:
            self.dropped["malformed"] += 1
            return []
        if not 0 <= count <= _MAX_COUNT:
            self.dropped["malformed"] += 1
            return []
        self._counts[name] = count
        ev = self._event(name, now)
        return [ev] if ev is not None else []

    def tick(self, now: datetime) -> list[SensingEvent]:
        if self._available != "online":
            return []
        out = []
        for name in self._counts:
            ev = self._event(name, now)
            if ev is not None:
                out.append(ev)
        return out

    def _event(self, name: str, now: datetime) -> SensingEvent | None:
        count = self._counts[name]
        if count == 0 and not (self._available == "online"
                               and self._detect.get(name) is True):
            # Unknown is not empty: see the module docstring.
            return None
        present = count > 0
        return SensingEvent(
            room=self.cfg.names[name], modality="camera", presence=present,
            motion=0.0, breathing_bpm=None, heart_bpm=None,
            # 0.0 on absence is the wire convention; fusion gives a trusted
            # camera's "empty" its own weight (see fusion's dissent rule).
            confidence=DEFAULT_CONFIDENCE if present else 0.0,
            ts=now.isoformat(), count=count, sensor_id=f"frigate:{name}")


Transport = Callable[[list], AsyncIterator[tuple[str, bytes, bool]]]


class FrigateSource:
    """A SourceManager source reading Frigate's broker (see `EspresenseSource`)."""

    def __init__(self, cfg: FrigateConfig, *, transport: Transport | None = None,
                 host: str = "localhost", port: int = 1883, username: str = "",
                 password: str = "", reassert_s: float = REASSERT_S, now_fn=None):
        self._tracker = FrigateTracker(cfg)
        self._cfg = cfg
        self._reassert_s = max(1.0, float(reassert_s))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        if transport is None:
            from wavr.mqtt_subscriber import mqtt_messages

            def transport(topics):
                return mqtt_messages(host, port, topics, username=username,
                                     password=password, client_id="wavr-frigate")
        self._transport = transport

    @property
    def dropped(self) -> dict:
        return dict(self._tracker.dropped)

    async def events(self) -> AsyncIterator[SensingEvent]:
        async for ev in tracked_events(self._transport(self._cfg.topics()),
                                       self._tracker, self._reassert_s, self._now):
            yield ev

