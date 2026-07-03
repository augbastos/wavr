import json

import pytest

from wavr.ha_discovery import publish_ha_discovery

ROOMS = ["sala", "quarto"]


def _record():
    """A fake publisher matching the (topic, payload, retain) callable shape."""
    msgs = []
    return msgs, lambda t, p, r: msgs.append((t, p, r))


def _payload(msgs, topic):
    for t, p, r in msgs:
        if t == topic:
            return json.loads(p), r
    raise AssertionError(f"no discovery message for {topic}")


def test_per_room_occupancy_binary_sensor():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/binary_sensor/wavr_sala/config")
    assert retain is True
    assert cfg["device_class"] == "occupancy"
    assert cfg["state_topic"] == "wavr/rooms/sala/state"
    assert "value_json.occupied" in cfg["value_template"]
    assert cfg["payload_on"] and cfg["payload_off"]
    assert cfg["device"] == {"identifiers": ["wavr"], "name": "Wavr", "manufacturer": "Wavr"}


def test_per_room_confidence_sensor():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/sensor/wavr_quarto_confidence/config")
    assert retain is True
    assert cfg["state_topic"] == "wavr/rooms/quarto/state"      # same retained state topic
    assert "value_json.confidence" in cfg["value_template"]
    assert cfg["unit_of_measurement"] == "%"
    assert cfg["device"]["identifiers"] == ["wavr"]             # shared device grouping


def test_house_presence_binary_sensor():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/binary_sensor/wavr_house/config")
    assert retain is True
    assert cfg["device_class"] == "presence"
    assert cfg["state_topic"] == "wavr/house/state"
    assert cfg["payload_on"] == "home"
    assert cfg["payload_off"] == "away"
    assert cfg["device"]["identifiers"] == ["wavr"]


def test_all_expected_topics_and_count():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    topics = {t for t, _, _ in msgs}
    for room in ROOMS:
        assert f"homeassistant/binary_sensor/wavr_{room}/config" in topics
        assert f"homeassistant/sensor/wavr_{room}_confidence/config" in topics
    assert "homeassistant/binary_sensor/wavr_house/config" in topics
    assert len(msgs) == len(ROOMS) * 2 + 1                       # 2 per room + house


def test_everything_is_retained():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    assert all(retain is True for _, _, retain in msgs)


def test_all_payloads_are_valid_json():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    for _, payload, _ in msgs:
        json.loads(payload)   # raises on malformed discovery JSON


def test_prefix_and_discovery_prefix_are_configurable():
    msgs, publish = _record()
    publish_ha_discovery(publish, ["sala"], prefix="casa", discovery_prefix="ha")
    room_cfg, _ = _payload(msgs, "ha/binary_sensor/wavr_sala/config")
    assert room_cfg["state_topic"] == "casa/rooms/sala/state"
    house_cfg, _ = _payload(msgs, "ha/binary_sensor/wavr_house/config")
    assert house_cfg["state_topic"] == "casa/house/state"


def _collect_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _collect_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


# Single-letter position keys are checked as exact JSON keys (they collide as
# substrings, e.g. "occupancy" ends in "y"); the multi-char leaks are also scanned
# as substrings across the raw topic + payload text.
_FORBIDDEN_KEYS = {"x", "y", "target", "targets", "pose", "poses",
                   "vitals", "position", "positions", "px", "py"}
_FORBIDDEN_SUBSTRINGS = ("target", "pose", "vital", "position")


def test_privacy_no_positions_targets_or_vitals_anywhere():
    msgs, publish = _record()
    publish_ha_discovery(publish, ["sala", "quarto", "quintal"])
    assert msgs, "discovery published nothing to scan"
    for topic, payload, _ in msgs:
        keys = _collect_keys(json.loads(payload))
        leaked = keys & _FORBIDDEN_KEYS
        assert not leaked, f"privacy leak: forbidden key(s) {leaked} in {topic}"
        blob = f"{topic} {payload}".lower()
        for word in _FORBIDDEN_SUBSTRINGS:
            assert word not in blob, f"privacy leak: '{word}' appears in {topic}"


def test_config_ha_discovery_defaults_off(monkeypatch):
    monkeypatch.delenv("WAVR_HA_DISCOVERY", raising=False)
    from wavr.config import load_config
    assert load_config().ha_discovery is False   # opt-in gate, off by default
