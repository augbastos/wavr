"""Bounded, deterministic provider streams using only operator-selected inputs."""

import json
import sys
import types
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wavr.espresense import EspresenseConfig, EspresenseTracker
from wavr.frigate import FrigateConfig, FrigateTracker
from wavr.ha_presence import HAPresenceStore, HomeAssistantSource, to_events
from wavr.mqtt_subscriber import CONNECTED, DISCONNECTED, mqtt_messages
from wavr.fusion import FusionEngine
from wavr.timebase import TimeBase


T0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
DATA = Path(__file__).parent / "data" / "providers"


def rows(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def shape(events):
    return [(e.room, e.presence) for e in events]


def test_espresense_recorded_protocol_and_reconnect():
    cfg = EspresenseConfig(devices={"watch:enrolled": "Watch"},
                           rooms={"kitchen": "kitchen"})
    tracker = EspresenseTracker(cfg)
    assert cfg.topics() == ["espresense/devices/watch:enrolled/+",
                            "espresense/rooms/kitchen/status",
                            "espresense/rooms/kitchen/motion"]
    for i, row in enumerate(rows("espresense.json")):
        events = tracker.on_message(row["topic"], row["payload"].encode(),
                                    T0 + timedelta(seconds=i))
        expected = [] if row["room"] is None else [(row["room"], row["present"])]
        assert shape(events) == expected
        assert "aa:bb:cc:dd:ee:ff" not in repr(events) + repr(tracker._readings)
        assert "private-irk" not in repr(events) + repr(tracker._readings)
    # A lost broker is silence, not absence (fusion decays what was held).
    assert tracker.on_message(DISCONNECTED, b"", T0 + timedelta(seconds=10)) == []
    assert tracker.tick(T0 + timedelta(seconds=11)) == []
    assert tracker.on_message(CONNECTED, b"", T0 + timedelta(seconds=12)) == []
    assert shape(tracker.on_message("espresense/devices/watch:enrolled/kitchen",
                                    b'{"distance":1}', T0 + timedelta(seconds=13))) == [
        ("kitchen", True)]


def test_frigate_recorded_protocol_and_reconnect():
    cfg = FrigateConfig(names={"front_door": "hall"})
    tracker = FrigateTracker(cfg)
    assert cfg.topics() == ["frigate/available", "frigate/front_door/person",
                            "frigate/front_door/detect/state"]
    for i, row in enumerate(rows("frigate.json")):
        events = tracker.on_message(row["topic"], row["payload"].encode(),
                                    T0 + timedelta(seconds=i))
        expected = [] if row["room"] is None else [(row["room"], row["present"])]
        assert shape(events) == expected
    tracker.on_message("frigate/available", b"online", T0 + timedelta(seconds=8))
    assert shape(tracker.on_message("frigate/front_door/person", b"1", T0)) == [
        ("hall", True)]
    assert tracker.on_message(DISCONNECTED, b"", T0 + timedelta(seconds=9)) == []
    assert tracker._counts == {} and tracker._detect == {}
    assert tracker.tick(T0 + timedelta(seconds=10)) == []
    tracker.on_message(CONNECTED, b"", T0 + timedelta(seconds=11))
    tracker.on_message("frigate/available", b"online", T0 + timedelta(seconds=12))
    assert tracker.tick(T0 + timedelta(seconds=13)) == []
    assert shape(tracker.on_message("frigate/front_door/person", b"1", T0)) == [
        ("hall", True)]


def test_bermuda_recorded_states_have_only_mapped_rooms_and_no_identity():
    store = HAPresenceStore(":memory:")
    mapping = store.map_location("sensor.phone_area",
                                 {"Kitchen": "kitchen", "Office": "office"})
    for row in rows("bermuda.json"):
        state = {"state": row["state"], "attributes": row.get("attributes", {})}
        events = to_events(state, mapping, at=T0.isoformat())
        assert {e.room: e.presence for e in events} == row["expected"]
        assert all(e.modality == "ble" and e.count is None for e in events)
        assert "aa:bb:cc:dd:ee:ff" not in repr(events)
    store.close()


def test_espresense_rejects_wrong_types_and_hostile_bytes():
    tracker = EspresenseTracker(EspresenseConfig(
        devices={"watch:enrolled": ""}, rooms={"kitchen": "kitchen"}))
    topic = "espresense/devices/watch:enrolled/kitchen"
    bad = [b"not json", b"[]", b'{"distance":true}',
           b'{"distance":"1.0"}', b'{"distance":-1}',
           b'{"distance":NaN}', b'{"distance":Infinity}',
           b'\xff', b'{' + b'"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
           b'{"distance":1,"pad":"' + b'x' * 4096 + b'"}']
    for payload in bad:
        assert tracker.on_message(topic, payload, T0) == []
    assert tracker.dropped["malformed"] == len(bad)
    assert tracker._readings == {}


def test_frigate_rejects_hostile_payloads_without_losing_held_count():
    tracker = FrigateTracker(FrigateConfig(names={"front_door": "hall"}))
    tracker.on_message("frigate/available", b"online", T0)
    topic = "frigate/front_door/person"
    assert shape(tracker.on_message(topic, b"1", T0)) == [("hall", True)]
    bad = [b"not json", b"1.0", b"-1", b"NaN", b"Infinity", b"\xff",
           b"[" * 2000 + b"]" * 2000,
           b"1" + b" " * 4096]
    for payload in bad:
        assert tracker.on_message(topic, payload, T0) == []
    assert tracker.dropped["malformed"] == len(bad)
    assert tracker._counts == {"front_door": 1}
    assert shape(tracker.tick(T0 + timedelta(seconds=5))) == [("hall", True)]


def test_bermuda_ignores_malformed_states_instead_of_releasing_a_room():
    store = HAPresenceStore(":memory:")
    mapping = store.map_location("sensor.phone_area", {"Kitchen": "kitchen"})
    for bad in (42, True, b"\xff", ["Kitchen"], {"area": "Kitchen"},
                [[0]] * 2000, "x" * 4097):
        assert to_events({"state": bad}, mapping, at=T0.isoformat()) == []
    store.close()


def test_espresense_100k_messages_leave_no_stale_readings():
    devices = {f"watch:{i}": "" for i in range(256)}
    tracker = EspresenseTracker(EspresenseConfig(
        devices=devices, rooms={"kitchen": "kitchen"}, timeout_s=5))
    for i in range(100_000):
        tracker.on_message(f"espresense/devices/watch:{i % 256}/kitchen",
                           b'{"distance":1}', T0 + timedelta(seconds=i))
    tracker.tick(T0 + timedelta(seconds=100_010))
    assert tracker._readings == {}
    assert tracker._current == {}
    assert len(tracker._online) <= 1


def test_frigate_100k_messages_stay_bounded_to_configured_names():
    tracker = FrigateTracker(FrigateConfig(names={"front_door": "hall"}))
    tracker.on_message("frigate/available", b"online", T0)
    for i in range(100_000):
        name = "front_door" if i % 2 else f"unknown_{i}"
        tracker.on_message(f"frigate/{name}/person", b"1", T0)
    assert len(tracker._counts) <= 1
    assert len(tracker._detect) <= 1
    assert tracker.dropped["unknown_name"] == 50_000


def test_bermuda_100k_states_keep_only_the_mapped_entity_clock():
    store = HAPresenceStore(":memory:")
    mapping = store.map_location("sensor.phone_area", {"Kitchen": "kitchen"})

    class LocalHA:
        calls = 0

        def get_state(self, entity_id):
            self.calls += 1
            assert entity_id == "sensor.phone_area"
            return {"state": "Kitchen", "last_changed": T0.isoformat()}

    client = LocalHA()
    source = HomeAssistantSource(store, client, timebase=TimeBase(now_fn=lambda: T0))
    for _ in range(100_000):
        assert shape(source._poll_one(mapping)) == [("kitchen", True)]
    assert client.calls == 100_000
    assert len(source._last_changed) == 1
    store.close()


def test_bermuda_unknown_stops_refreshing_evidence_until_it_ages_out():
    store = HAPresenceStore(":memory:")
    mapping = store.map_location("sensor.phone_area", {"Kitchen": "kitchen"})
    clock = [T0]

    class LocalHA:
        state = "Kitchen"

        def get_state(self, entity_id):
            assert entity_id == "sensor.phone_area"
            return {"state": self.state, "last_changed": T0.isoformat()}

    ha = LocalHA()
    source = HomeAssistantSource(store, ha, timebase=TimeBase(now_fn=lambda: clock[0]))
    fusion = FusionEngine(now_fn=lambda: clock[0], freshness_s=1, stale_s=10,
                          vacate_s=0)
    for event in source._poll_one(mapping):
        fusion.update(event)
    assert fusion.state("kitchen").confidence > 0
    ha.state = "unknown"
    clock[0] += timedelta(seconds=11)
    assert source._poll_one(mapping) == []
    assert fusion.state("kitchen").confidence == 0
    ha.state = "Kitchen"
    for event in source._poll_one(mapping):
        fusion.update(event)
    assert fusion.state("kitchen").confidence > 0
    store.close()


def test_frigate_disconnect_retires_evidence_without_false_camera_absence():
    clock = [T0]
    tracker = FrigateTracker(FrigateConfig(names={"front_door": "hall"}))
    fusion = FusionEngine(now_fn=lambda: clock[0], freshness_s=1, stale_s=10,
                          vacate_s=0)
    tracker.on_message("frigate/available", b"online", T0)
    for event in tracker.on_message("frigate/front_door/person", b"1", T0):
        fusion.update(event)
    assert fusion.state("hall").confidence > 0
    assert tracker.on_message(DISCONNECTED, b"", T0) == []
    assert tracker.tick(T0 + timedelta(seconds=1)) == []
    clock[0] += timedelta(seconds=11)
    assert fusion.state("hall").confidence == 0
    assert tracker._counts == {}


def test_mqtt_reader_only_connects_to_operator_broker(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.on_connect = None
            self.on_disconnect = None
            self.on_message = None

        def subscribe(self, topic, qos):
            calls.append(("subscribe", topic, qos))

        def reconnect_delay_set(self, **kwargs):
            calls.append(("backoff", kwargs))

        def connect_async(self, host, port):
            calls.append(("connect", host, port))

        def loop_start(self):
            self.on_connect(self, None, None, types.SimpleNamespace(is_failure=False))

        def disconnect(self):
            calls.append(("disconnect",))

        def loop_stop(self):
            calls.append(("stop",))

    mqtt = types.ModuleType("paho.mqtt.client")
    mqtt.Client = Client
    mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", mqtt)

    async def run():
        stream = mqtt_messages("operator-broker.local", 1883,
                               ["frigate/available", "frigate/front_door/person"])
        try:
            assert await asyncio.wait_for(stream.__anext__(), 1) == (CONNECTED, b"", False)
        finally:
            await stream.aclose()

    asyncio.run(run())
    assert [c for c in calls if c[0] == "connect"] == [
        ("connect", "operator-broker.local", 1883)]
    assert [c[1] for c in calls if c[0] == "subscribe"] == [
        "frigate/available", "frigate/front_door/person"]
