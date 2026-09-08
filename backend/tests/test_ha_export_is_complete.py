"""The Home Assistant export must carry what Wavr already knows -- and must say
"I cannot see" instead of "nobody is here".

Two defects are pinned here.

1. **The export was occupancy + confidence only.** `RoomState` already carries
   `person_count` and the whole precision ladder, and `wavr.sensor_coverage`
   already produces per-room health and precision rows. None of it reached the
   wire, so an HA user could not build an automation on "the kitchen has two
   people" or "trust this room less, its only sensor is a Wi-Fi count".

2. **`occupied: false` was published for rooms nothing was watching.** HA renders
   a plain `off` occupancy binary_sensor as "Clear", so a room whose only camera
   died looked exactly like a room a healthy sensor had confirmed empty. That is
   the collapse the dashboard's map painter already refuses to make internally.
"""
import json

import jinja2
import pytest

from wavr.ha_discovery import publish_ha_discovery
from wavr.rules import RulesEngine
from wavr.sensor_coverage import (
    HEALTH_DISABLED, HEALTH_OFFLINE, HEALTH_OK, SensorCoverage, summarize,
)

ROOMS = ["sala", "quarto"]


def _record():
    msgs = []
    return msgs, lambda t, p, r: msgs.append((t, p, r))


def _rs(room, occupied, confidence=0.8, ts="2026-09-06T10:00:00+00:00", **extra):
    """A fused RoomState dict as the hub actually delivers it."""
    return {"room": room, "occupied": occupied, "confidence": confidence,
            "vitals": {}, "sources": [], "explanation": "", "ts": ts, **extra}


def _last(msgs, topic):
    hits = [(t, p, r) for t, p, r in msgs if t == topic]
    assert hits, f"nothing published to {topic}; got {sorted({t for t, _, _ in msgs})}"
    return hits[-1]


def _body(msgs, topic):
    _t, payload, _r = _last(msgs, topic)
    return json.loads(payload)


def _cam(room, health, calibrated=False):
    return SensorCoverage(sensor_id=f"cam-{room}", kind="camera", modality="camera",
                          room=room, health=health, calibrated=calibrated)


def _summary(coverage, rooms):
    """The real `sensor_coverage.summarize` shape -- not a hand-rolled stand-in,
    so a change to that serializer breaks this test instead of drifting past it."""
    return summarize(rooms, coverage)


def _render(template: str, payload: dict) -> str:
    """Render an HA value_template the way Home Assistant would."""
    env = jinja2.Environment(autoescape=False)
    return env.from_string(template).render(
        value_json=payload, value=json.dumps(payload)).strip()


# ---- 1. the facts RoomState already holds now reach the wire ----

def test_room_state_carries_person_count_and_the_precision_rung():
    msgs, publish = _record()
    RulesEngine(publish).handle(_rs("sala", True, person_count=2,
                                    precision_level="count", precision_pct=75,
                                    precision_next="calibrate_camera_position"))
    body = _body(msgs, "wavr/rooms/sala/state")
    assert body["person_count"] == 2
    assert body["precision_pct"] == 75
    assert body["occupied"] is True and body["confidence"] == 0.8


def test_the_precision_enum_label_never_reaches_the_wire():
    # The rung is exported as the ordinal, never as the label: the top rung is
    # spelled `position`, and the derived-MQTT privacy invariant is that the word
    # never appears on the broker. The ordinal carries the same information.
    msgs, publish = _record()
    RulesEngine(publish).handle(_rs("sala", True, precision_level="position",
                                    precision_pct=100,
                                    precision_next=None))
    body = _body(msgs, "wavr/rooms/sala/state")
    assert body["precision_pct"] == 100
    assert "precision_level" not in body and "precision_next" not in body
    assert "position" not in " ".join(f"{t} {p}" for t, p, _ in msgs).lower()


def test_unknown_person_count_travels_as_null_not_as_zero():
    # `None` means "no counting-capable source vouched for a number". Publishing
    # 0 would fabricate a headcount out of an absence of evidence.
    msgs, publish = _record()
    RulesEngine(publish).handle(_rs("sala", True, person_count=None,
                                    precision_level="room", precision_pct=50))
    body = _body(msgs, "wavr/rooms/sala/state")
    assert "person_count" in body and body["person_count"] is None


def test_minimal_state_dict_still_produces_the_original_three_key_payload():
    # The pre-existing contract: a caller handing this only {room, occupied,
    # confidence, ts} must get byte-identical output to before this feature.
    msgs, publish = _record()
    RulesEngine(publish).handle(
        {"room": "sala", "occupied": True, "confidence": 0.77, "ts": "t1"})
    assert _body(msgs, "wavr/rooms/sala/state") == {
        "occupied": True, "confidence": 0.77, "ts": "t1"}
    assert [t for t, _, _ in msgs] == ["wavr/rooms/sala/state"]


# ---- 2. availability: a blind room must not read as empty ----

def test_room_with_no_healthy_sensor_publishes_unavailable_not_occupied_false():
    """THE defect. `quarto`'s only camera is offline; `sala`'s is fine."""
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK),
                                  _cam("quarto", HEALTH_OFFLINE)], ROOMS))
    eng.handle(_rs("quarto", False))
    eng.handle(_rs("sala", False))

    assert _last(msgs, "wavr/rooms/quarto/availability")[1] == "offline"
    assert _last(msgs, "wavr/rooms/quarto/availability")[2] is True   # retained
    blind = _body(msgs, "wavr/rooms/quarto/state")
    assert blind["occupied"] is None, "a blind room must not assert occupied:false"
    assert blind["available"] is False
    assert blind["health"] == "offline"

    # The healthy room is untouched: it still makes the assertion it has earned.
    assert _last(msgs, "wavr/rooms/sala/availability")[1] == "online"
    seen = _body(msgs, "wavr/rooms/sala/state")
    assert seen["occupied"] is False and seen["available"] is True
    assert seen["health"] == "ok"


def test_room_with_no_sensor_at_all_is_unavailable_and_reads_health_none():
    # `uncovered` is a purchase, not a repair -- and it is still not "empty".
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK)], ["sala", "porao"]))
    eng.handle(_rs("porao", False))
    assert _last(msgs, "wavr/rooms/porao/availability")[1] == "offline"
    body = _body(msgs, "wavr/rooms/porao/state")
    assert body["occupied"] is None
    assert body["health"] == "none" and body["sensors"] == 0


def test_switched_off_camera_is_disabled_not_offline():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_DISABLED)], ["sala"]))
    eng.handle(_rs("sala", False))
    assert _body(msgs, "wavr/rooms/sala/state")["health"] == "disabled"
    assert _last(msgs, "wavr/rooms/sala/availability")[1] == "offline"


def test_a_live_detection_keeps_the_room_available():
    # A source the coverage table cannot match reads `unknown`, never green. If
    # that alone forced the room unavailable, HA would HIDE a real detection --
    # the same failure pointed the other way.
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OFFLINE)], ["sala"]))
    eng.handle(_rs("sala", True))
    assert _last(msgs, "wavr/rooms/sala/availability")[1] == "online"
    assert _body(msgs, "wavr/rooms/sala/state")["occupied"] is True


def test_going_blind_fires_no_vacant_edge_and_no_routine_trigger():
    edges = []
    msgs, publish = _record()
    eng = RulesEngine(publish, on_edge=lambda r, o: edges.append((r, o)))
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK)], ["sala"]))
    eng.handle(_rs("sala", True))                       # someone is here
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OFFLINE)], ["sala"]))
    eng.handle(_rs("sala", False))                      # camera died mid-presence
    assert [p for t, p, _ in msgs if t == "wavr/rooms/sala/event"] == []
    assert edges == []
    # ...and a genuine departure still lands once sight comes back.
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK)], ["sala"]))
    eng.handle(_rs("sala", False))
    assert [p for t, p, _ in msgs if t == "wavr/rooms/sala/event"] == ["vacant"]
    assert edges == [("sala", False)]


def test_availability_is_change_detected():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK)], ["sala"]))
    for _ in range(3):
        eng.handle(_rs("sala", False))
    assert len([t for t, _, _ in msgs if t == "wavr/rooms/sala/availability"]) == 1


def test_without_coverage_nothing_changes():
    # No coverage wired -> no availability topic, no `available` key, and the
    # pre-existing occupied:false assertion is left exactly as it was.
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle(_rs("sala", False))
    body = _body(msgs, "wavr/rooms/sala/state")
    assert body["occupied"] is False
    assert "available" not in body and "health" not in body
    assert not [t for t, _, _ in msgs if t.endswith("/availability")]


# ---- 3. sensor health + coverage on their own topics ----

def test_per_room_coverage_topic_carries_health_and_precision():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary(
        [_cam("sala", HEALTH_OK, calibrated=True), _cam("sala", HEALTH_OFFLINE)],
        ["sala"]))
    body = _body(msgs, "wavr/rooms/sala/coverage")
    assert body["sensors"] == 2 and body["observing"] == 1
    assert body["health"] == "ok"          # one is alive: the room IS watched
    assert body["precision_pct"] == 100    # the calibrated camera's top rung
    assert _last(msgs, "wavr/rooms/sala/coverage")[2] is True   # retained
    # ...as the ordinal: the rung's enum label stays off the broker.
    assert "position" not in " ".join(f"{t} {p}" for t, p, _ in msgs).lower()


def test_uncovered_room_coverage_row_reports_the_bottom_rung():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK)], ["sala", "porao"]))
    assert _body(msgs, "wavr/rooms/porao/coverage") == {
        "sensors": 0, "observing": 0, "health": "none", "precision_pct": 0}


def test_house_coverage_topic_counts_rooms_and_is_deduped():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    summary = _summary([_cam("sala", HEALTH_OK), _cam("quarto", HEALTH_OFFLINE)],
                       ["sala", "quarto", "porao"])
    eng.handle_coverage(summary)
    eng.handle_coverage(summary)           # unchanged -> no re-publish
    house = [m for m in msgs if m[0] == "wavr/house/coverage"]
    assert len(house) == 1
    body = json.loads(house[0][1])
    assert body == {"rooms": 3, "covered": 2, "uncovered": 1, "observing": 1}


def test_coverage_provider_is_pulled_and_throttled():
    calls = []
    clock = {"t": 0.0}

    def provider():
        calls.append(1)
        return _summary([_cam("sala", HEALTH_OFFLINE)], ["sala"])

    msgs, publish = _record()
    eng = RulesEngine(publish, coverage_provider=provider, coverage_interval_s=10.0,
                      monotonic=lambda: clock["t"])
    eng.handle(_rs("sala", False))
    eng.handle(_rs("sala", False))
    assert len(calls) == 1                      # throttled inside the window
    clock["t"] = 11.0
    eng.handle(_rs("sala", False))
    assert len(calls) == 2
    assert _last(msgs, "wavr/rooms/sala/availability")[1] == "offline"
    assert _body(msgs, "wavr/rooms/sala/state")["occupied"] is None


def test_coverage_provider_failure_keeps_the_last_known_coverage():
    clock = {"t": 0.0}
    state = {"boom": False}

    def provider():
        if state["boom"]:
            raise RuntimeError("store unreadable")
        return _summary([_cam("sala", HEALTH_OK)], ["sala"])

    msgs, publish = _record()
    eng = RulesEngine(publish, coverage_provider=provider, coverage_interval_s=1.0,
                      monotonic=lambda: clock["t"])
    eng.handle(_rs("sala", False))
    state["boom"] = True
    clock["t"] = 5.0
    eng.handle(_rs("sala", False))              # a bad read must not blank the house
    assert _last(msgs, "wavr/rooms/sala/availability")[1] == "online"
    assert _body(msgs, "wavr/rooms/sala/state")["occupied"] is False


def test_new_topics_are_legal_for_an_mqtt_hostile_room_name():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("Sala + Cozinha/Kids#1", HEALTH_OFFLINE)],
                                 ["Sala + Cozinha/Kids#1"]))
    eng.handle(_rs("Sala + Cozinha/Kids#1", False))
    room_topics = [t for t, _, _ in msgs if t.startswith("wavr/rooms/")]
    assert room_topics
    for t in room_topics:
        assert "#" not in t and "+" not in t
        assert t.count("/") == 3                # no injected topic level
        assert t.startswith("wavr/rooms/sala___cozinha_kids_1/")


def test_new_topics_and_payloads_carry_no_geometry_or_identity():
    msgs, publish = _record()
    eng = RulesEngine(publish)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OK, calibrated=True)],
                                 ["sala", "porao"]))
    eng.handle(_rs("sala", True, person_count=2, precision_level="position",
                   precision_pct=100, precision_next=None))
    blob = " ".join(f"{t} {p}" for t, p, _ in msgs).lower()
    # The same four words `test_derived_mqtt` scans the live stream for, checked
    # here against the topics this change adds.
    for word in ("target", "pose", "vital", "position", "identit"):
        assert word not in blob, f"leak: {word!r} reached MQTT"
    for _t, payload, _r in msgs:
        body = json.loads(payload) if payload.startswith("{") else {}
        assert not ({"x", "y", "px", "py", "targets", "vitals"} & set(body))


# ---- 4. the HA side: unknown, not "Clear" ----

def _cfg(msgs, topic):
    for t, p, _r in msgs:
        if t == topic:
            return json.loads(p)
    raise AssertionError(f"no discovery message for {topic}")


def test_occupancy_template_renders_unknown_for_a_blind_room():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    tmpl = _cfg(msgs, "homeassistant/binary_sensor/wavr_sala/config")["value_template"]
    # HA maps the literal "None" to state `unknown`; "OFF" is what it draws as
    # "Clear". These two must never be the same string.
    assert _render(tmpl, {"occupied": None, "available": False}) == "None"
    assert _render(tmpl, {"occupied": False, "available": False}) == "None"
    assert _render(tmpl, {"occupied": False, "available": True}) == "OFF"
    assert _render(tmpl, {"occupied": True, "available": True}) == "ON"


def test_occupancy_template_still_reads_a_legacy_retained_payload():
    # An HA install upgrading from an older Wavr has a retained payload with no
    # `available` key. It must keep rendering ON/OFF, not go blank.
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    tmpl = _cfg(msgs, "homeassistant/binary_sensor/wavr_sala/config")["value_template"]
    assert _render(tmpl, {"occupied": True, "confidence": 0.9, "ts": "t"}) == "ON"
    assert _render(tmpl, {"occupied": False, "confidence": 0.9, "ts": "t"}) == "OFF"


def test_confidence_template_is_unknown_for_a_blind_room():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    tmpl = _cfg(msgs, "homeassistant/sensor/wavr_sala_confidence/config")["value_template"]
    assert _render(tmpl, {"confidence": 0.9, "available": False}) == "None"
    # "90.0" is what `| round(0)` has always produced here -- pinned so the blind
    # branch cannot quietly change the number an available room reports.
    assert _render(tmpl, {"confidence": 0.9, "available": True}) == "90.0"
    assert _render(tmpl, {"confidence": 0.9}) == "90.0"        # legacy payload


def test_occupancy_entity_exposes_the_full_export_as_attributes():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg = _cfg(msgs, "homeassistant/binary_sensor/wavr_sala/config")
    assert cfg["json_attributes_topic"] == "wavr/rooms/sala/state"
    attrs = json.loads(_render(cfg["json_attributes_template"], {
        "occupied": False, "confidence": 0.42, "ts": "t", "person_count": 2,
        "precision_pct": 75,
        "sensors": 2, "observing": 1, "health": "ok", "available": True,
    }))
    assert attrs == {
        "confidence": 0.42, "person_count": 2, "precision_pct": 75,
        "sensors": 2, "sensors_observing": 1, "sensor_health": "ok",
        "available": True,
    }


def test_attributes_template_survives_a_legacy_retained_payload():
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    cfg = _cfg(msgs, "homeassistant/binary_sensor/wavr_sala/config")
    attrs = json.loads(_render(cfg["json_attributes_template"],
                               {"occupied": True, "confidence": 0.9, "ts": "t"}))
    assert attrs["confidence"] == 0.9
    assert attrs["person_count"] is None and attrs["sensor_health"] is None
    assert attrs["available"] is True      # absent key must not read as blind


def test_the_eight_existing_entities_and_their_topics_are_unchanged():
    # HA installs already have these unique_ids and state topics.
    msgs, publish = _record()
    publish_ha_discovery(publish, ROOMS)
    assert len(msgs) == len(ROOMS) * 4 + 3
    for room in ROOMS:
        assert _cfg(msgs, f"homeassistant/binary_sensor/wavr_{room}/config")[
            "unique_id"] == f"wavr_{room}_occupancy"
        assert _cfg(msgs, f"homeassistant/sensor/wavr_{room}_confidence/config")[
            "state_topic"] == f"wavr/rooms/{room}/state"
        assert f"homeassistant/binary_sensor/wavr_{room}_intrusion/config" in {
            t for t, _, _ in msgs}
        assert f"homeassistant/binary_sensor/wavr_{room}_routine_anomaly/config" in {
            t for t, _, _ in msgs}
    for topic, payload, retain in msgs:
        cfg = json.loads(payload)
        assert retain is True
        assert cfg["availability_topic"] == "wavr/status", topic
        assert cfg["device"]["identifiers"] == ["wavr"]


def test_discovery_state_topic_matches_what_the_engine_publishes():
    room = "Sala + Cozinha/Kids#1"
    disc, disc_pub = _record()
    publish_ha_discovery(disc_pub, [room])
    declared = _cfg(disc, "homeassistant/binary_sensor/wavr_sala___cozinha_kids_1/config")
    rules_msgs, rules_pub = _record()
    RulesEngine(rules_pub).handle(_rs(room, True))
    published = {t for t, _, _ in rules_msgs if t.endswith("/state")}
    assert declared["state_topic"] in published
    assert declared["json_attributes_topic"] == declared["state_topic"]


@pytest.mark.parametrize("prefix", ["wavr", "casa"])
def test_room_availability_topic_follows_the_prefix(prefix):
    from wavr.mqtt_topics import room_availability_topic
    msgs, publish = _record()
    eng = RulesEngine(publish, prefix=prefix)
    eng.handle_coverage(_summary([_cam("sala", HEALTH_OFFLINE)], ["sala"]))
    eng.handle(_rs("sala", False))
    assert _last(msgs, room_availability_topic(prefix, "sala"))[1] == "offline"


# -- The last mile: is any of this wired into the app that actually runs? ------

def test_the_running_app_gives_the_engine_a_coverage_provider(tmp_path, monkeypatch):
    """Everything above tests `RulesEngine` in isolation, which is the right
    place to test it and is also how the whole feature could have shipped
    switched off: nothing in `create_app` passed `coverage_provider`, so sensor
    health, per-room coverage and per-room availability were computed, tested,
    and never published. A room whose only camera had died still reached Home
    Assistant as a plain "Clear".

    This builds the REAL app with a recording publisher, takes the engine that
    app constructed, and asks it to refresh — so the provider exercised here is
    the one the application wired, reading the application's own coverage.
    """
    import wavr.app as app_module

    built = {}
    real = app_module.RulesEngine

    class Recording(real):
        def __init__(self, *a, **kw):
            built["kwargs"] = kw
            super().__init__(*a, **kw)
            built["engine"] = self

    monkeypatch.setattr(app_module, "RulesEngine", Recording)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))

    msgs, publish = _record()
    app_module.create_app(rules_publish=publish)

    assert "engine" in built, "the app built no RulesEngine with a publisher"
    provider = built["kwargs"].get("coverage_provider")
    assert provider is not None, (
        "create_app built the engine with no coverage_provider, so sensor "
        "health and per-room availability are computed and never published")

    # It resolves, and returns the application's own coverage shape rather than
    # raising: `_refresh_coverage` swallows a provider failure on purpose, so a
    # broken wire would look exactly like a quiet house.
    summary = provider()
    assert isinstance(summary, dict) and "rooms" in summary, summary

    built["engine"]._refresh_coverage()
    coverage_topics = [t for t, _p, _r in msgs if "/coverage" in t]
    assert coverage_topics, (
        "the wired provider published nothing; topics seen: "
        f"{sorted({t for t, _p, _r in msgs})}")
