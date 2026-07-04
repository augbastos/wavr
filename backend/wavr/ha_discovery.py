from __future__ import annotations

import json
from typing import Callable, Iterable

from wavr.mqtt_topics import room_state_topic, slug_room, status_topic

# Shared HA device block: every Wavr entity carries it so Home Assistant groups
# them under a single "Wavr" device instead of scattering loose entities.
_DEVICE = {
    "identifiers": ["wavr"],
    "name": "Wavr",
    "manufacturer": "Wavr",
}

# PRIVACY INVARIANT (hard): discovery + the state topics it points at expose ONLY
# occupancy + confidence. Positions (x/y), targets, pose and vitals are NEVER
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
      - a `sensor` for ``confidence`` (%), reading the same state topic.
    Plus one house-level `binary_sensor` (device_class ``presence``) reading the
    retained home/away topic. All entities share the `_DEVICE` block.

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
