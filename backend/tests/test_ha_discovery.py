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
        assert f"homeassistant/binary_sensor/wavr_{room}_intrusion/config" in topics
        assert f"homeassistant/binary_sensor/wavr_{room}_routine_anomaly/config" in topics
    assert "homeassistant/binary_sensor/wavr_house/config" in topics
    assert "homeassistant/binary_sensor/wavr_house_intrusion/config" in topics
    assert "homeassistant/sensor/wavr_house_status/config" in topics
    # 4 per room (occupancy/confidence/intrusion/routine_anomaly) + 3 house-level
    # (presence/intrusion/status) -- Build C4 added the intrusion/anomaly/status trio.
    assert len(msgs) == len(ROOMS) * 4 + 3


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


def test_every_payload_declares_availability():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    assert msgs
    for topic, payload, _ in msgs:
        cfg = json.loads(payload)
        assert cfg["availability_topic"] == "wavr/status", topic
        assert cfg["payload_available"] == "online"
        assert cfg["payload_not_available"] == "offline"


def test_availability_topic_follows_prefix():
    msgs, publish = _record()
    publish_ha_discovery(publish, ["sala"], prefix="casa")
    for _, payload, _ in msgs:
        assert json.loads(payload)["availability_topic"] == "casa/status"


def test_room_with_wildcard_is_slugged_in_topic_and_object_id():
    msgs, publish = _record()
    publish_ha_discovery(publish, ["Kids#1"])
    cfg, _ = _payload(msgs, "homeassistant/binary_sensor/wavr_kids_1/config")
    assert cfg["state_topic"] == "wavr/rooms/kids_1/state"
    assert cfg["name"] == "Kids#1 occupancy"     # human name keeps the raw room label
    for _, payload, _ in msgs:
        assert "#" not in json.loads(payload)["state_topic"]


# ---- Build C4: intrusion / routine-anomaly / house-status discovery configs ----

def test_per_room_intrusion_binary_sensor():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/binary_sensor/wavr_sala_intrusion/config")
    assert retain is True
    assert cfg["device_class"] == "safety"
    assert cfg["state_topic"] == "wavr/watch/rooms/sala/intrusion"
    assert cfg["payload_on"] == "ON" and cfg["payload_off"] == "OFF"
    assert cfg["device"]["identifiers"] == ["wavr"]


def test_per_room_routine_anomaly_binary_sensor():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/binary_sensor/wavr_quarto_routine_anomaly/config")
    assert retain is True
    assert cfg["device_class"] == "problem"
    assert cfg["state_topic"] == "wavr/rooms/quarto/routine_anomaly"
    assert cfg["payload_on"] == "ON" and cfg["payload_off"] == "OFF"


def test_house_level_intrusion_binary_sensor_is_room_agnostic():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/binary_sensor/wavr_house_intrusion/config")
    assert retain is True
    assert cfg["device_class"] == "safety"
    assert cfg["state_topic"] == "wavr/watch/house/intrusion"
    for room in ROOMS:                              # never names a room
        assert room not in cfg["state_topic"] and room not in cfg["name"]


def test_house_status_sensor_shape():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg, retain = _payload(msgs, "homeassistant/sensor/wavr_house_status/config")
    assert retain is True
    assert cfg["state_topic"] == "wavr/house/status"
    assert cfg["value_template"] == "{{ value_json.status }}"
    assert cfg["device_class"] == "enum"
    assert set(cfg["options"]) == {"ok", "notice", "alert"}
    assert cfg["json_attributes_topic"] == "wavr/house/status"
    assert "reasons" in cfg["json_attributes_template"]
    assert "score" in cfg["json_attributes_template"]


def test_c4_entities_use_the_same_prefix():
    msgs, publish = _record()
    publish_ha_discovery(publish, ["sala"], prefix="casa")
    intrusion, _ = _payload(msgs, "homeassistant/binary_sensor/wavr_sala_intrusion/config")
    anomaly, _ = _payload(msgs, "homeassistant/binary_sensor/wavr_sala_routine_anomaly/config")
    house_intr, _ = _payload(msgs, "homeassistant/binary_sensor/wavr_house_intrusion/config")
    status, _ = _payload(msgs, "homeassistant/sensor/wavr_house_status/config")
    assert intrusion["state_topic"] == "casa/watch/rooms/sala/intrusion"
    assert anomaly["state_topic"] == "casa/rooms/sala/routine_anomaly"
    assert house_intr["state_topic"] == "casa/watch/house/intrusion"
    assert status["state_topic"] == "casa/house/status"


def test_publisher_and_discovery_agree_on_slugged_state_topic():
    # The silent-drop fix: discovery must subscribe to EXACTLY the topic the rules
    # engine publishes, even for an MQTT-hostile room name.
    from wavr.rules import RulesEngine

    room = "Sala + Cozinha/Kids#1"
    disc_msgs, disc_pub = _record()
    publish_ha_discovery(disc_pub, [room])
    disc_state = next(
        json.loads(p)["state_topic"]
        for _, p, _ in disc_msgs
        if "/rooms/" in json.loads(p).get("state_topic", "")
    )
    rules_msgs = []
    RulesEngine(lambda t, p, r: rules_msgs.append(t)).handle(
        {"room": room, "occupied": True, "confidence": 0.5, "ts": "t"})
    rules_state = next(t for t in rules_msgs if t.endswith("/state"))
    assert disc_state == rules_state             # lockstep -- no divergence
    assert "+" not in rules_state and "#" not in rules_state
    assert rules_state.count("/") == 3           # no injected topic level
