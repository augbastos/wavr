"""ESPresense boards as room evidence, and what the adapter refuses to take.

Payloads are ESPresense's own: the docs' device object (firmware v4.0.6) and two
MQTT captures from ESPresense-companion's test data (2023 firmware, which still
carried `idType`/`raw`). No broker and no board: the transport is a list.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from wavr.espresense import (
    DEFAULT_CONFIDENCE, EspresenseConfig, EspresenseConfigError, EspresenseSource,
    EspresenseTracker, descriptor, sensor_id_for,
)
from wavr.fusion import FusionEngine, RESOLUTION_SCOPE, TRUSTED_ABSENCE_MODALITIES
from wavr.mqtt_subscriber import CONNECTED, DISCONNECTED, TopicRefused, check_topic

T0 = datetime(2026, 9, 23, 20, 0, 0, tzinfo=timezone.utc)

# docs: ESPresense.com configuration/rest-api.md (current firmware, one device)
DOCS_DEVICE = {"id": "apple:iphone15-3", "name": "Dan's iPhone", "rssi@1m": -59,
               "rssi": -71, "distance": 2.4, "mac": "aabbccddeeff"}
# ESPresense-companion tests/TestData, real 2023 captures of one watch seen by two boards
CAPTURE_BASEMENT = {"mac": "58b1294494d3", "id": "watch:darrell", "name": "Darrell's Watch",
                    "idType": 250, "rssi@1m": -64, "rssi": -86, "raw": 5.24,
                    "distance": 5.89, "int": 588}
CAPTURE_OFFICE = {"mac": "58b1294494d3", "id": "watch:darrell", "name": "Darrell's Watch",
                  "idType": 250, "rssi@1m": -69, "rssi": -68, "raw": 0.93,
                  "distance": 0.85, "int": 628}


def cfg(**kw):
    base = dict(devices={"watch:darrell": "Darrell", "apple:iphone15-3": ""},
                rooms={"office": "office", "basement": "basement", "kitchen": "kitchen"},
                max_distance_m=5.0, timeout_s=30.0)
    base.update(kw)
    return EspresenseConfig(**base)


def dev(device_id, slug, body):
    return f"espresense/devices/{device_id}/{slug}", json.dumps(body).encode()


def at(s):
    return T0 + timedelta(seconds=s)


# -- What is subscribed ----------------------------------------------------------

def test_only_enrolled_devices_and_mapped_boards_are_subscribed():
    """A board publishes every BLE device in range, bystanders included. The
    adapter asks the broker for the enrolled ids only, so a neighbour's phone is
    not filtered out after it arrives -- it never arrives."""
    topics = cfg().topics()
    assert "espresense/devices/watch:darrell/+" in topics
    assert all(t.startswith(("espresense/devices/watch:darrell/",
                             "espresense/devices/apple:iphone15-3/",
                             "espresense/rooms/")) for t in topics)
    assert not any("settings" in t or "known_irks" in t or "#" in t for t in topics)
    for t in topics:
        check_topic(t)


def test_an_id_that_would_widen_the_subscription_is_refused():
    for bad in ("+", "watch/#", "a/b", ""):
        with pytest.raises(EspresenseConfigError):
            EspresenseConfig(devices={bad: ""}, rooms={"office": "office"})


def test_wildcards_are_refused_at_the_subscriber():
    for bad in ("espresense/#", "frigate/+/person/snapshot", "a/+/b", "a//b"):
        with pytest.raises(TopicRefused):
            check_topic(bad)
    assert check_topic("espresense/devices/irk:0123/+")


# -- The evidence ----------------------------------------------------------------

def test_a_device_near_a_mapped_board_puts_presence_in_that_room():
    t = EspresenseTracker(cfg())
    [ev] = t.on_message(*dev("apple:iphone15-3", "kitchen", DOCS_DEVICE), at(0))
    assert (ev.room, ev.modality, ev.presence) == ("kitchen", "ble", True)
    assert ev.confidence == DEFAULT_CONFIDENCE
    assert ev.count is None and ev.targets == () and ev.identities == ()


def test_the_nearest_board_wins_and_a_move_releases_the_old_room():
    t = EspresenseTracker(cfg(max_distance_m=10.0))
    [first] = t.on_message(*dev("watch:darrell", "basement", CAPTURE_BASEMENT), at(0))
    assert first.room == "basement"
    evs = t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(1))
    assert [(e.room, e.presence) for e in evs] == [("basement", False), ("office", True)]
    assert evs[0].confidence == 0.0, "a release is a zero-mass reading, not a claim"


def test_a_device_farther_than_the_limit_is_in_no_room():
    """5.89 m from the basement board is not "in the basement"."""
    t = EspresenseTracker(cfg(max_distance_m=5.0))
    assert t.on_message(*dev("watch:darrell", "basement", CAPTURE_BASEMENT), at(0)) == []


def test_a_stale_reading_is_released_at_the_next_tick():
    """ESPresense says nothing when a device leaves; the adapter times it out."""
    t = EspresenseTracker(cfg())
    t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(0))
    assert [(e.room, e.presence) for e in t.tick(at(20))] == [("office", True)]
    assert [(e.room, e.presence) for e in t.tick(at(31))] == [("office", False)]
    assert t.tick(at(40)) == []


def test_a_board_going_offline_releases_what_it_vouched_for():
    t = EspresenseTracker(cfg())
    t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(0))
    evs = t.on_message("espresense/rooms/office/status", b"offline", at(1))
    assert [(e.room, e.presence) for e in evs] == [("office", False)]


def test_unknown_device_unknown_room_and_malformed_payloads_produce_nothing():
    t = EspresenseTracker(cfg())
    assert t.on_message(*dev("md:0059:22", "office", {"id": "md:0059:22", "distance": 1}), at(0)) == []
    assert t.on_message(*dev("watch:darrell", "garage", CAPTURE_OFFICE), at(0)) == []
    for bad in (b"not json", b"{}", b'{"distance": "near"}', b'{"distance": -1}',
                b'{"distance": NaN}', b"[1,2]"):
        assert t.on_message("espresense/devices/watch:darrell/office", bad, at(0)) == []
    assert t.dropped == {"unknown_device": 1, "unknown_room": 1, "malformed": 6}


def test_the_mac_and_irk_in_a_payload_are_never_kept():
    t = EspresenseTracker(cfg(devices={"irk:00112233445566778899aabbccddeeff": ""}))
    body = dict(CAPTURE_OFFICE, id="irk:00112233445566778899aabbccddeeff",
                irk="00112233445566778899aabbccddeeff")
    [ev] = t.on_message(*dev("irk:00112233445566778899aabbccddeeff", "office", body), at(0))
    # The operator's enrolment necessarily holds the id (it names the topic);
    # the MAC is kept nowhere, and what leaves the adapter -- the event that is
    # fused, published and persisted -- carries neither the MAC nor the key.
    assert "58b1294494d3" not in repr(t.__dict__) + repr(ev)
    assert "00112233445566778899" not in repr(ev)
    assert ev.sensor_id == sensor_id_for("irk:00112233445566778899aabbccddeeff")
    assert ev.sensor_id.startswith("espresense:") and len(ev.sensor_id) == 23


def test_duplicate_updates_are_just_refreshes():
    t = EspresenseTracker(cfg())
    a = t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(0))
    b = t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(1))
    assert [(e.room, e.presence) for e in a + b] == [("office", True), ("office", True)]


def test_motion_is_a_pir_and_its_off_cannot_clear_a_room():
    t = EspresenseTracker(cfg())
    [on] = t.on_message("espresense/rooms/kitchen/motion", b"ON", at(0))
    [off] = t.on_message("espresense/rooms/kitchen/motion", b"OFF", at(5))
    assert (on.modality, on.presence, off.presence, off.confidence) == ("pir", True, False, 0.0)
    assert "pir" not in TRUSTED_ABSENCE_MODALITIES
    assert [e.presence for e in t.tick(at(30))] == [False], "held state is re-asserted"


def test_a_lost_broker_is_silence_not_absence():
    t = EspresenseTracker(cfg())
    t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), at(0))
    t.on_message("espresense/rooms/kitchen/motion", b"ON", at(0))
    assert t.on_message(DISCONNECTED, b"", at(1)) == []
    assert t.tick(at(2)) == [], "nothing is re-asserted from a broker Wavr cannot hear"
    assert t.on_message(CONNECTED, b"", at(3)) == []


def test_ble_is_room_precision_and_cannot_count():
    assert RESOLUTION_SCOPE["ble"] == "room"
    assert descriptor().precision_ceiling == "room"
    assert descriptor().reach == "lan"


def test_fused_end_to_end_the_room_follows_the_device():
    t = EspresenseTracker(cfg(max_distance_m=10.0))
    clock = [at(0)]
    fe = FusionEngine(now_fn=lambda: clock[0])
    for ev in t.on_message(*dev("watch:darrell", "basement", CAPTURE_BASEMENT), clock[0]):
        fe.update(ev)
    assert fe.state("basement").confidence > 0
    clock[0] = at(1)
    for ev in t.on_message(*dev("watch:darrell", "office", CAPTURE_OFFICE), clock[0]):
        fe.update(ev)
    assert fe.state("office").confidence > 0
    assert fe.state("basement").confidence == 0.0


# -- The source, over a transport ---------------------------------------------------

async def test_the_source_turns_a_message_stream_into_events_and_reasserts():
    seen_topics = []

    async def transport(topics):
        seen_topics.extend(topics)
        yield (CONNECTED, b"", False)
        yield (*dev("watch:darrell", "office", CAPTURE_OFFICE), False)
        await asyncio.sleep(3600)     # a broker with nothing more to say

    clock = [T0]
    src = EspresenseSource(cfg(), transport=transport, reassert_s=1.0,
                           now_fn=lambda: clock[0])
    gen = src.events()
    first = await asyncio.wait_for(gen.__anext__(), 2)
    clock[0] = at(5)
    again = await asyncio.wait_for(gen.__anext__(), 3)   # the periodic re-assertion
    await gen.aclose()
    assert (first.room, first.presence, again.room, again.presence) == \
        ("office", True, "office", True)
    assert again.ts == at(5).isoformat(), "re-asserted evidence is stamped now"
    assert seen_topics == cfg().topics()


async def test_a_transport_failure_reaches_the_supervisor():
    """A missing paho or an unreachable broker must be a visible fault, not a
    source that looks healthy and says nothing."""
    async def transport(topics):
        raise ModuleNotFoundError("No module named 'paho'")
        yield  # pragma: no cover

    src = EspresenseSource(cfg(), transport=transport)
    with pytest.raises(ModuleNotFoundError):
        await asyncio.wait_for(src.events().__anext__(), 2)


# -- Configuration and wiring --------------------------------------------------------

def test_nothing_is_registered_until_devices_and_rooms_are_mapped(monkeypatch):
    for var in ("WAVR_ESPRESENSE_DEVICES", "WAVR_ESPRESENSE_ROOMS", "WAVR_FRIGATE_CAMERAS",
                "WAVR_BLE_KNOWN", "WAVR_RUVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    from wavr.app import _default_sources
    from wavr.config import load_config
    assert "espresense" not in {n for n, _f, _e in _default_sources(load_config())}
    monkeypatch.setenv("WAVR_ESPRESENSE_DEVICES", "watch:darrell=Darrell")
    assert "espresense" not in {n for n, _f, _e in _default_sources(load_config())}, \
        "devices with no board mapped to a room would subscribe to evidence with nowhere to go"
    monkeypatch.setenv("WAVR_ESPRESENSE_ROOMS", "office=office,attic=")
    cfg_ = load_config()
    assert cfg_.espresense_rooms == {"office": "office"}, "a board with no room is dropped"
    assert "espresense" in {n for n, _f, _e in _default_sources(cfg_)}


def test_a_mapping_that_would_widen_the_subscription_starts_nothing(monkeypatch, caplog):
    monkeypatch.setenv("WAVR_ESPRESENSE_DEVICES", "watch:darrell,#")
    monkeypatch.setenv("WAVR_ESPRESENSE_ROOMS", "office=office")
    from wavr.app import _default_sources
    from wavr.config import load_config
    assert "espresense" not in {n for n, _f, _e in _default_sources(load_config())}
    assert "not started" in caplog.text


def test_through_the_real_app_the_room_becomes_occupied(tmp_path, monkeypatch):
    """The whole path: supervised source -> _ingest -> fusion -> /api/state."""
    import time
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)

    async def transport(topics):
        yield (*dev("watch:darrell", "office", CAPTURE_OFFICE), False)
        await asyncio.sleep(3600)

    app = create_app(
        sources=[("espresense", lambda: EspresenseSource(cfg(), transport=transport), True)],
        storage=Storage(":memory:"), hub=Hub(), camera_store=CameraStore(":memory:"))
    with TestClient(app) as c:
        deadline = time.time() + 5
        rooms = {}
        while time.time() < deadline:
            rooms = c.get("/api/state").json()        # {room: RoomState}
            if "office" in rooms:
                break
            time.sleep(0.05)
    office = rooms["office"]
    assert office["occupied"] is True
    assert [s["modality"] for s in office["sources"]] == ["ble"]
