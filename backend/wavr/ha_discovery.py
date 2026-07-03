from __future__ import annotations

import json
from typing import Callable, Iterable

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


def _slug(name: str) -> str:
    """object_id / unique_id fragment: keep [a-z0-9_-], collapse anything else to
    '_' so a room name is always a valid MQTT discovery node. The state_topic still
    uses the raw room name (to match what the rules engine publishes)."""
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
    return s.strip("_").lower() or "room"


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
    """
    for room in rooms:
        oid = _slug(room)
        state_topic = f"{prefix}/rooms/{room}/state"

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
        }),
        True,
    )
