"""ESPresense rooms as evidence: which room a person's own BLE device is in.

ESPresense (github.com/ESPresense/ESPresense, AGPL-3.0) is ESP32 firmware that a
household flashes onto cheap boards, one per room. Each board measures its
distance to nearby BLE devices and publishes it over MQTT. Wavr reads those
messages; it copies no ESPresense code and runs nothing on the boards.

    espresense/devices/<device-id>/<room-slug>   {"id": .., "distance": 2.4, ..}
    espresense/rooms/<room-slug>/status          online | offline   (retained, LWT)
    espresense/rooms/<room-slug>/motion          ON | OFF           (retained, on change)

(Topic shapes and fields from ESPresense firmware v4.0.6; see
docs/EXTERNAL-PROVIDERS.md for the sources.)

## What is refused, and why

**Unknown devices are never seen.** A board publishes fingerprints of every BLE
device in range -- the neighbours' phones, a passer-by's watch (`md:`, `sd:`,
`apple:`, `name:` ids). Wavr subscribes ONLY to the device ids an operator
enrolled, one topic each, so a bystander's traffic is not filtered out after
arrival: it never arrives. Nothing here can learn an id it was not given.

**The room comes from Wavr's mapping, never from the topic.** The last topic
level is the board's own room slug, which anyone with the board's web UI can
rename. An unmapped slug is dropped, not turned into a room.

**Secrets are never read.** `espresense/settings/+/config` and
`rooms/+/known_irks` carry the IRKs that de-anonymise a phone; nothing here
subscribes to them (`mqtt_subscriber` refuses the wildcards that would), and the
`mac` and `irk` fields of a payload are never kept. The device id itself becomes
a short hash before it names a sensor, because an `irk:` id contains the key.

**A distance is not a position.** Wavr takes the NEAREST fresh board within
`max_distance_m` as the device's room, and nothing finer: `ble` is capped at room
precision by fusion, and one RSSI-derived distance per board cannot honestly
place a person on a floor plan.

**Absence has to be timed out here.** ESPresense publishes nothing when a device
leaves -- it stops publishing. A reading older than `timeout_s` stops counting,
and the room the device was attributed to is released explicitly (a zero-mass
reading, which is what "this sensor no longer sees it here" is worth). A board
that goes `offline` releases everything it was vouching for. A broker that goes
away is silence, and silence decays in fusion the ordinary way.

**Presence only, no names.** A room event carries no identity: per-room identity
is exactly what Wavr refuses (`RoomState.identities` is house-level only). The
label an operator gives a device is for the operator's screen.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent
from wavr.mqtt_subscriber import CONNECTED, DISCONNECTED, tracked_events
from wavr.providers import CONF_NONE, KIND_SENSOR, REACH_LAN, describe

_LOG = logging.getLogger(__name__)

PREFIX = "espresense"      # hard-coded in the firmware (defaults.h CHANNEL)

# Declared, not measured, like every provider that hands over no calibrated
# confidence (see ha_presence.DEFAULT_CONFIDENCE). Below Wavr's own BLE source
# (0.7): Wavr did not place these boards and cannot see how they are aimed.
# Reliability measures the real figure per sensor and scales it down.
DEFAULT_CONFIDENCE = 0.6
MOTION_CONFIDENCE = 0.65
DEFAULT_MAX_DISTANCE_M = 5.0
# ESPresense-companion's own default for "no longer current" (Config.cs).
DEFAULT_TIMEOUT_S = 30.0
# How often held state is re-asserted: the motion topics are published on
# change only, and fusion ages evidence that is not refreshed.
REASSERT_S = 10.0

_FORBIDDEN_IN_ID = ("/", "+", "#")


class EspresenseConfigError(ValueError):
    """A configuration that would subscribe to more than the operator named."""


@dataclass(frozen=True)
class EspresenseConfig:
    """What an operator enrolled: device ids -> label, board slug -> Wavr room."""

    devices: dict = field(default_factory=dict)
    rooms: dict = field(default_factory=dict)
    max_distance_m: float = DEFAULT_MAX_DISTANCE_M
    timeout_s: float = DEFAULT_TIMEOUT_S

    def __post_init__(self):
        for key in list(self.devices) + list(self.rooms):
            if not key or any(c in key for c in _FORBIDDEN_IN_ID) or len(key) > 200:
                raise EspresenseConfigError(
                    f"{key!r} cannot be an ESPresense id or room slug: it would "
                    f"change which topics are subscribed")
        if not (self.max_distance_m > 0 and math.isfinite(self.max_distance_m)):
            raise EspresenseConfigError("max_distance_m must be a positive number")
        if not (self.timeout_s > 0 and math.isfinite(self.timeout_s)):
            raise EspresenseConfigError("timeout_s must be a positive number")

    @property
    def configured(self) -> bool:
        return bool(self.devices) and bool(self.rooms)

    def topics(self) -> list[str]:
        out = [f"{PREFIX}/devices/{d}/+" for d in sorted(self.devices)]
        for slug in sorted(self.rooms):
            out += [f"{PREFIX}/rooms/{slug}/status", f"{PREFIX}/rooms/{slug}/motion"]
        return out


def sensor_id_for(device_id: str) -> str:
    """A stable, non-reversible name for one enrolled device.

    An `irk:` id carries the phone's Identity Resolving Key in clear; stored
    as a sensor id it would sit in every persisted room state.
    """
    return "espresense:" + hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]


def descriptor():
    """This provider, declaring itself (see `providers.describe`)."""
    return describe(
        "espresense", "ESPresense BLE rooms", KIND_SENSOR, REACH_LAN,
        modality="ble",
        observes=("presence",),
        confidence_semantics=CONF_NONE,
        requires=("MQTT broker address", "enrolled ESPresense device ids",
                  "a Wavr room for each ESPresense board"),
        notes=("Which room an enrolled phone or tag is in, from ESPresense "
               "boards on your own MQTT broker. Devices you did not enrol are "
               "never received."))


def _iso(t: datetime) -> str:
    return t.isoformat()


class EspresenseTracker:
    """The whole mapping from messages to evidence, with no I/O.

    `on_message` and `tick` return the events to emit. Every call takes `now`
    so the logic is deterministic under test.
    """

    def __init__(self, cfg: EspresenseConfig):
        self.cfg = cfg
        # device id -> board slug -> (distance m, received)
        self._readings: dict[str, dict[str, tuple[float, datetime]]] = {}
        self._current: dict[str, str | None] = {}      # device id -> Wavr room
        self._motion: dict[str, bool] = {}             # board slug -> ON?
        self._online: dict[str, bool] = {}             # board slug -> online?
        self.dropped = {"unknown_device": 0, "unknown_room": 0, "malformed": 0}

    # -- messages ------------------------------------------------------------

    def on_message(self, topic: str, payload: bytes, now: datetime) -> list[SensingEvent]:
        if topic == DISCONNECTED:
            # The broker is gone: Wavr no longer knows. Forget, and emit nothing
            # -- silence decays in fusion; a claim of absence would be a lie.
            self._readings.clear()
            self._current.clear()
            self._motion.clear()
            return []
        if topic == CONNECTED:
            return []
        levels = topic.split("/")
        if len(levels) == 4 and levels[0] == PREFIX and levels[1] == "devices":
            return self._device(levels[2], levels[3], payload, now)
        if len(levels) == 4 and levels[0] == PREFIX and levels[1] == "rooms":
            slug, kind = levels[2], levels[3]
            if slug not in self.cfg.rooms:
                self.dropped["unknown_room"] += 1
                return []
            text = payload.decode("utf-8", "replace").strip()
            if kind == "status":
                return self._status(slug, text.lower(), now)
            if kind == "motion":
                return self._motion_msg(slug, text.upper(), now)
        return []

    def _device(self, device_id: str, slug: str, payload: bytes,
                now: datetime) -> list[SensingEvent]:
        if device_id not in self.cfg.devices:
            self.dropped["unknown_device"] += 1
            return []
        if slug not in self.cfg.rooms:
            self.dropped["unknown_room"] += 1
            return []
        try:
            body = json.loads(payload)
            distance = float(body["distance"])
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            self.dropped["malformed"] += 1
            return []
        if not (math.isfinite(distance) and distance >= 0):
            self.dropped["malformed"] += 1
            return []
        # Only the distance is kept. `mac`, `irk`, `name` and the rest of the
        # payload go out of scope here and are never stored.
        self._online[slug] = True
        self._readings.setdefault(device_id, {})[slug] = (distance, now)
        return self._reconcile(device_id, now)

    def _status(self, slug: str, value: str, now: datetime) -> list[SensingEvent]:
        if value == "offline":
            self._online[slug] = False
            self._motion.pop(slug, None)
            out = []
            for device_id in list(self._readings):
                self._readings[device_id].pop(slug, None)
                out += self._reconcile(device_id, now)
            return out
        if value == "online":
            self._online[slug] = True
        return []

    def _motion_msg(self, slug: str, value: str, now: datetime) -> list[SensingEvent]:
        if value not in ("ON", "OFF"):
            self.dropped["malformed"] += 1
            return []
        self._motion[slug] = value == "ON"
        return [self._motion_event(slug, now)]

    # -- time ------------------------------------------------------------------

    def tick(self, now: datetime) -> list[SensingEvent]:
        """Re-assert held state and release what has timed out."""
        out: list[SensingEvent] = []
        for device_id in list(self._current):
            out += self._reconcile(device_id, now)
        for slug in self._motion:
            if self._online.get(slug, True):
                out.append(self._motion_event(slug, now))
        return out

    # -- evidence --------------------------------------------------------------

    def _reconcile(self, device_id: str, now: datetime) -> list[SensingEvent]:
        fresh = {
            slug: dist
            for slug, (dist, at) in self._readings.get(device_id, {}).items()
            if (now - at).total_seconds() <= self.cfg.timeout_s
            and dist <= self.cfg.max_distance_m
            and self._online.get(slug, True)
        }
        room = self.cfg.rooms[min(fresh, key=fresh.get)] if fresh else None
        previous = self._current.get(device_id)
        out = []
        if previous and previous != room:
            out.append(self._event(previous, device_id, False, now))
        if room:
            out.append(self._event(room, device_id, True, now))
            self._current[device_id] = room
        else:
            self._current.pop(device_id, None)
        return out

    def _event(self, room: str, device_id: str, present: bool,
               now: datetime) -> SensingEvent:
        return SensingEvent(
            room=room, modality="ble", presence=present, motion=0.0,
            breathing_bpm=None, heart_bpm=None,
            confidence=DEFAULT_CONFIDENCE if present else 0.0,
            ts=_iso(now), sensor_id=sensor_id_for(device_id))

    def _motion_event(self, slug: str, now: datetime) -> SensingEvent:
        on = self._motion[slug]
        # `pir`: outside TRUSTED_ABSENCE_MODALITIES, so OFF can never clear a
        # room -- a person reading a book stops moving long before they leave.
        return SensingEvent(
            room=self.cfg.rooms[slug], modality="pir", presence=on,
            motion=1.0 if on else 0.0, breathing_bpm=None, heart_bpm=None,
            confidence=MOTION_CONFIDENCE if on else 0.0,
            ts=_iso(now), sensor_id=f"espresense:{slug}:motion")


Transport = Callable[[list], AsyncIterator[tuple[str, bytes, bool]]]


class EspresenseSource:
    """A SourceManager source over one MQTT broker.

    The transport is injectable (`(topics) -> async iterator of (topic, payload,
    retained)`), so the whole adapter is exercised in tests with recorded
    payloads and no broker.
    """

    def __init__(self, cfg: EspresenseConfig, *, transport: Transport | None = None,
                 host: str = "localhost", port: int = 1883, username: str = "",
                 password: str = "", reassert_s: float = REASSERT_S,
                 now_fn=None):
        self._tracker = EspresenseTracker(cfg)
        self._cfg = cfg
        self._reassert_s = max(1.0, float(reassert_s))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        if transport is None:
            from wavr.mqtt_subscriber import mqtt_messages

            def transport(topics):
                return mqtt_messages(host, port, topics, username=username,
                                     password=password, client_id="wavr-espresense")
        self._transport = transport

    @property
    def dropped(self) -> dict:
        return dict(self._tracker.dropped)

    async def events(self) -> AsyncIterator[SensingEvent]:
        async for ev in tracked_events(self._transport(self._cfg.topics()),
                                       self._tracker, self._reassert_s, self._now):
            yield ev
