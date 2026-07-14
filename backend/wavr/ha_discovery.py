from __future__ import annotations

import json
from typing import Callable, Iterable

from wavr.mqtt_topics import (
    house_status_topic, intrusion_topic, room_state_topic, routine_anomaly_topic,
    slug_room, status_topic,
)

# Shared HA device block: every Wavr entity carries it so Home Assistant groups
# them under a single "Wavr" device instead of scattering loose entities.
_DEVICE = {
    "identifiers": ["wavr"],
    "name": "Wavr",
    "manufacturer": "Wavr",
}

# PRIVACY INVARIANT (hard): discovery + the state topics it points at expose ONLY
# occupancy + confidence, and (Build C4) the three DERIVED trigger signals below --
# unrecognized-person counts, an occupancy-anomaly bool, and house-status
# captions/severity. Positions (x/y), targets, pose and vitals are NEVER
# referenced in any topic, template, or payload built here. Keep it that way.


def publish_ha_discovery(
    publish: Callable[[str, str, bool], None],
    rooms: Iterable[str],
    prefix: str = "wavr",
    discovery_prefix: str = "homeassistant",
) -> None:
    """Publish RETAINED Home Assistant MQTT Discovery config messages so HA
    auto-creates Wavr's entities.

    `publish(topic, payload, retain)` is the same callable shape the existing
    rules/away publishers use (injectable in tests; the real one is
    `make_publisher(...)`).

    Per room it emits:
      - a `binary_sensor` (device_class ``occupancy``) reading the retained room
        state topic, extracting the ``occupied`` boolean into ON/OFF;
      - a `sensor` for ``confidence`` (%), reading the same state topic;
      - Build C4: a `binary_sensor` (device_class ``safety``, ON = unrecognized
        person present) reading Watch's A2 per-room intrusion topic;
      - Build C4: a `binary_sensor` (device_class ``problem``, ON = unusual for
        this hour) reading A4's per-room routine-anomaly topic.
    Plus house-level entities: a `binary_sensor` (device_class ``presence``)
    reading the retained home/away topic; Build C4's room-AGNOSTIC counterpart
    to the per-room intrusion sensor (device_class ``safety``); and Build C4's
    `sensor` for A10's composed house-status verdict (state = ok/notice/alert,
    with `score` + `reasons` as attributes -- captions/severity only, see
    `wavr.house_status`). All entities share the `_DEVICE` block.

    Every payload also declares an ``availability_topic`` (``{prefix}/status``):
    the publisher's retained Last Will flips it to ``offline`` when Wavr drops off,
    so HA renders these entities *unavailable* instead of showing stale presence.
    The room segment of every state topic is slugged (via `mqtt_topics`) IDENTICALLY
    to what the rules engine publishes -- so a room name with an MQTT wildcard
    (`+`/`#`) or `/` still yields a legal, matching topic."""
    # Availability is the same for every entity -- one retained status topic, HA's
    # documented default online/offline payloads.
    availability = {
        "availability_topic": status_topic(prefix),
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    for room in rooms:
        oid = slug_room(room)
        state_topic = room_state_topic(prefix, room)

        # Occupancy: pull the `occupied` boolean out of the retained JSON and map
        # it to ON/OFF (HA's binary_sensor payload contract).
        publish(
            f"{discovery_prefix}/binary_sensor/wavr_{oid}/config",
            json.dumps({
                "name": f"{room} occupancy",
                "unique_id": f"wavr_{oid}_occupancy",
                "device_class": "occupancy",
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if value_json.occupied else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": _DEVICE,
                **availability,
            }),
            True,   # retained: HA picks up the config even if it starts after Wavr
        )

        # Confidence: same retained state topic, exposed as a 0-100 % reading.
        publish(
            f"{discovery_prefix}/sensor/wavr_{oid}_confidence/config",
            json.dumps({
                "name": f"{room} confidence",
                "unique_id": f"wavr_{oid}_confidence",
                "state_topic": state_topic,
                "value_template": "{{ (value_json.confidence * 100) | round(0) }}",
                "unit_of_measurement": "%",
                "device": _DEVICE,
                **availability,
            }),
            True,
        )

        # Build C4: Watch's A2 unrecognized-person signal, per room. device_class
        # 'safety' -- HA's convention is ON = unsafe, which matches "an
        # unrecognized person is present" (count-only upstream in wavr.watch;
        # this discovery config only points at the topic, never the count).
        publish(
            f"{discovery_prefix}/binary_sensor/wavr_{oid}_intrusion/config",
            json.dumps({
                "name": f"{room} unrecognized person",
                "unique_id": f"wavr_{oid}_intrusion",
                "device_class": "safety",
                "state_topic": intrusion_topic(prefix, room),
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": _DEVICE,
                **availability,
            }),
            True,
        )

        # Build C4: A4's "occupancy unusual for this hour" signal, per room.
        # device_class 'problem' -- ON = an abnormal condition was detected.
        publish(
            f"{discovery_prefix}/binary_sensor/wavr_{oid}_routine_anomaly/config",
            json.dumps({
                "name": f"{room} routine anomaly",
                "unique_id": f"wavr_{oid}_routine_anomaly",
                "device_class": "problem",
                "state_topic": routine_anomaly_topic(prefix, room),
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": _DEVICE,
                **availability,
            }),
            True,
        )

    # House-level presence: reads the retained plain "home"/"away" string.
    publish(
        f"{discovery_prefix}/binary_sensor/wavr_house/config",
        json.dumps({
            "name": "House presence",
            "unique_id": "wavr_house_presence",
            "device_class": "presence",
            "state_topic": f"{prefix}/house/state",
            "payload_on": "home",
            "payload_off": "away",
            "device": _DEVICE,
            **availability,
        }),
        True,
    )

    # Build C4: Watch's A2 house-level aggregate -- ROOM-AGNOSTIC (someone
    # unaccounted-for is in the house, spread across rooms so no single room's
    # count betrays them; see wavr.watch.house_unrecognized). Never names a room.
    publish(
        f"{discovery_prefix}/binary_sensor/wavr_house_intrusion/config",
        json.dumps({
            "name": "House unrecognized person",
            "unique_id": "wavr_house_intrusion",
            "device_class": "safety",
            "state_topic": intrusion_topic(prefix, None),
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": _DEVICE,
            **availability,
        }),
        True,
    )

    # Build C4: A10's composed "is everything OK in the house?" verdict.
    # State is the plain ok/notice/alert enum; `score` + `reasons` (each already
    # only {layer, kind, what, severity, ts} captions -- wavr.house_status never
    # adds a new raw field) ride along as HA attributes for the dashboard/
    # automation template to read without a second subscription.
    house_status_state_topic = house_status_topic(prefix)
    publish(
        f"{discovery_prefix}/sensor/wavr_house_status/config",
        json.dumps({
            "name": "House status",
            "unique_id": "wavr_house_status",
            "device_class": "enum",
            "options": ["ok", "notice", "alert"],
            "state_topic": house_status_state_topic,
            "value_template": "{{ value_json.status }}",
            "json_attributes_topic": house_status_state_topic,
            "json_attributes_template":
                "{{ {'score': value_json.score, 'reasons': value_json.reasons} | tojson }}",
            "device": _DEVICE,
            **availability,
        }),
        True,
    )
