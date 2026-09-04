"""Home Assistant's sensors as evidence, and the four things Wavr refuses to
infer from them.

The household that already owns six motion sensors has already bought most of
the hardware. The risk in accepting them is that HA's vocabulary is looser than
Wavr's: `unavailable` looks like `off`, a door contact looks like a sensor, and
an area name looks like a room. Each of those is one lie away from putting a
person somewhere they are not.
"""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.fusion import COUNTING_MODALITIES, RESOLUTION_SCOPE, TRUSTED_ABSENCE_MODALITIES
from wavr.ha_presence import (
    DEFAULT_CONFIDENCE, DEVICE_CLASS_MODALITY, HAPresenceError, HAPresenceStore,
    HomeAssistantSource, Mapping, descriptor, suggest, to_event,
)
from wavr.timebase import TimeBase

T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def hall(modality="pir", enabled=True):
    return Mapping("binary_sensor.hall_motion", "hall", modality, enabled)


def state(value="on", **kw):
    row = {"entity_id": "binary_sensor.hall_motion", "state": value,
           "last_changed": T0.isoformat()}
    row.update(kw)
    return row


def sensed(row, mapping):
    """`to_event` with the normalized time supplied.

    `at` is a required argument on purpose: it is the seam where HA's own clock
    has already been through `timebase`, and a default would let a caller feed
    HA's raw timestamp into fusion's freshness arithmetic without noticing."""
    return to_event(row, mapping, at=T0.isoformat())


# -- The refusals --------------------------------------------------------------

def test_unavailable_is_not_off():
    """HA uses `unavailable` for "this sensor is not reporting". Translating it
    into presence=False would turn a dead sensor into positive evidence that the
    room is empty."""
    for dead in ("unavailable", "unknown", "none", ""):
        assert sensed(state(dead), hall()) is None


def test_a_missing_state_produces_nothing():
    assert sensed({"entity_id": "x"}, hall()) is None


def test_a_motion_sensors_off_cannot_clear_a_room():
    """A person reading a book stops triggering a PIR long before they stop being
    in the room. `pir` sits outside TRUSTED_ABSENCE_MODALITIES for exactly that
    reason, and this pins the mapping that keeps it there."""
    assert DEVICE_CLASS_MODALITY["motion"] == "pir"
    assert "pir" not in TRUSTED_ABSENCE_MODALITIES


def test_ha_evidence_can_never_become_a_count():
    """Six motion sensors in one room are six opinions about presence. Adding
    them up would be the fabricated number this codebase refuses."""
    for modality in set(DEVICE_CLASS_MODALITY.values()):
        assert modality not in COUNTING_MODALITIES
        assert RESOLUTION_SCOPE[modality] == "room"
    assert sensed(state("on"), hall()).count is None


def test_the_declared_ceiling_matches_what_the_modalities_can_do():
    assert descriptor().precision_ceiling == "room"


def test_a_door_contact_is_not_a_presence_sensor():
    """A door opening is an event about a door. Treating it as presence puts a
    person in the hallway every time the wind moves."""
    rows = suggest([{"entity_id": "binary_sensor.front_door",
                     "device_class": "door", "friendly_name": "Front Door"}],
                   ["hall"])
    assert rows == []


# -- Where the room comes from -------------------------------------------------

def test_the_room_comes_from_the_mapping_not_the_payload():
    """Same rule as nodes.node_event: a renamed HA area must not silently
    relocate presence, and a compromised HA must not place a person in a room it
    was never given."""
    ev = sensed(state("on", room="bedroom", area="bedroom"), hall())
    assert ev.room == "hall"


def test_a_mapping_without_a_room_is_refused():
    """No default, for the reason `providers.describe` refuses a default reach:
    the comfortable value gets chosen when nobody is checking."""
    store = HAPresenceStore(":memory:")
    with pytest.raises(HAPresenceError):
        store.map("binary_sensor.hall_motion", "")


def test_a_malformed_entity_id_is_refused():
    store = HAPresenceStore(":memory:")
    with pytest.raises(HAPresenceError):
        store.map("not-an-entity", "hall")


def test_an_unsupported_modality_is_refused():
    store = HAPresenceStore(":memory:")
    with pytest.raises(HAPresenceError):
        store.map("binary_sensor.x", "hall", "camera")


# -- The event -----------------------------------------------------------------

def test_presence_carries_a_declared_confidence_below_a_camera():
    """A number Wavr declares rather than measures: HA hands over a boolean.
    Reliability measures the real figure per sensor and scales this DOWN."""
    ev = sensed(state("on"), hall())
    assert ev.presence is True
    assert ev.confidence == DEFAULT_CONFIDENCE < 0.9


def test_absence_carries_no_mass():
    """Matching every first-party source: an "I do not see anybody" from a PIR is
    much weaker evidence than a camera seeing an empty room."""
    ev = sensed(state("off"), hall())
    assert ev.presence is False and ev.confidence == 0.0


def test_the_sensor_id_is_namespaced():
    """So a reliability profile for an HA sensor can never be confused with a
    camera that happens to be called `motion`."""
    assert sensed(state(), hall()).sensor_id == "ha:binary_sensor.hall_motion"


def test_a_disabled_mapping_stays_silent():
    assert sensed(state("on"), hall(enabled=False)) is None


def test_device_tracker_home_counts_as_present():
    m = Mapping("device_tracker.phone", "house", "node")
    assert sensed(state("home"), m).presence is True
    assert sensed(state("not_home"), m).presence is False


# -- Suggestions, which are suggestions ----------------------------------------

def test_a_room_is_matched_from_the_name_and_offered_never_applied():
    rows = suggest([{"entity_id": "binary_sensor.hall_motion",
                     "device_class": "motion", "friendly_name": "Hall motion"}],
                   ["hall", "kitchen"])
    assert rows[0]["suggested_room"] == "hall"
    assert rows[0]["modality"] == "pir"
    assert "room" not in rows[0], "a suggestion is not a mapping"


def test_the_longest_room_match_wins():
    """A house with `bath` and `bathroom` must not put the bathroom sensor in the
    bath."""
    rows = suggest([{"entity_id": "binary_sensor.bathroom_motion",
                     "device_class": "motion",
                     "friendly_name": "Bathroom motion"}],
                   ["bath", "bathroom"])
    assert rows[0]["suggested_room"] == "bathroom"


def test_no_confident_match_leaves_the_room_empty_for_a_human():
    rows = suggest([{"entity_id": "binary_sensor.pir_3",
                     "device_class": "motion", "friendly_name": "PIR 3"}],
                   ["hall"])
    assert rows[0]["suggested_room"] == ""


def test_an_unknown_device_class_gets_the_conservative_modality():
    """Guessing upward means claiming a capability the sensor may not have."""
    rows = suggest([{"entity_id": "device_tracker.phone", "device_class": "",
                     "friendly_name": "Phone"}], [])
    assert rows[0]["modality"] == "node"


# -- The store -----------------------------------------------------------------

def test_a_mapping_round_trips_and_can_be_switched_off():
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.hall_motion", "hall", "pir", label="Hall PIR")
    assert [m.room for m in store.list()] == ["hall"]
    store.set_enabled("binary_sensor.hall_motion", False)
    assert store.list(enabled_only=True) == []
    assert store.unmap("binary_sensor.hall_motion") is True
    assert store.list() == []


def test_remapping_moves_the_room_rather_than_duplicating_the_entity():
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.x", "hall", "pir")
    store.map("binary_sensor.x", "kitchen", "pir")
    assert [(m.entity_id, m.room) for m in store.list()] == [("binary_sensor.x", "kitchen")]


# -- The provider declaration --------------------------------------------------

def test_home_assistant_is_lan_reach_and_never_leaves_the_home():
    """It contacts the operator's own HA over the local network and nothing
    else — the distinction REACH_INTERNET and REACH_CLOUD exist to make."""
    d = descriptor()
    assert d.reach == "lan"
    assert d.leaves_the_home is False
    assert d.needs_internet is False


def test_the_declaration_names_what_the_operator_must_supply():
    assert "long-lived access token" in descriptor().requires


# -- The source ----------------------------------------------------------------

class FakeHA:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or {}
        self.fail = fail
        self.asked = []

    def get_state(self, entity_id):
        self.asked.append(entity_id)
        if self.fail:
            raise OSError("home assistant is rebooting")
        return self.rows[entity_id]


@pytest.mark.asyncio
async def test_nothing_is_polled_when_nothing_is_mapped():
    """The "Wavr works fully with every external provider switched off" property,
    true by construction rather than by intention."""
    ha = FakeHA()
    src = HomeAssistantSource(HAPresenceStore(":memory:"), ha, interval=0.5)
    gen = src.events()
    with pytest.raises(TimeoutError):
        import asyncio
        await asyncio.wait_for(gen.__anext__(), timeout=0.2)
    assert ha.asked == []
    await gen.aclose()


@pytest.mark.asyncio
async def test_a_mapped_sensor_becomes_an_event():
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.hall_motion", "hall", "pir")
    ha = FakeHA({"binary_sensor.hall_motion": state("on")})
    src = HomeAssistantSource(store, ha, interval=0.05,
                              timebase=TimeBase(now_fn=lambda: T0))
    gen = src.events()
    ev = await gen.__anext__()
    assert ev.room == "hall" and ev.presence is True
    assert ev.modality == "pir"
    await gen.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_home_assistant_produces_no_event_not_a_false_one():
    """HA reboots and updates. Wavr stops claiming to know, and fusion's decay
    retires the evidence on its own."""
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.hall_motion", "hall", "pir")
    src = HomeAssistantSource(store, FakeHA(fail=True), interval=0.05)
    gen = src.events()
    import asyncio
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=0.2)
    await gen.aclose()


@pytest.mark.asyncio
async def test_a_slow_home_assistant_clock_is_corrected_not_discarded():
    """The whole reason timebase exists: HA's `last_changed` is stamped by HA's
    clock, and feeding a two-minute-slow one into fusion's freshness arithmetic
    means its evidence is silently thrown away."""
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.hall_motion", "hall", "pir")
    late = (T0 - timedelta(seconds=25)).isoformat()
    ha = FakeHA({"binary_sensor.hall_motion": state("on", last_changed=late)})
    src = HomeAssistantSource(store, ha, interval=0.05,
                              timebase=TimeBase(now_fn=lambda: T0))
    gen = src.events()
    ev = await gen.__anext__()
    assert ev.ts == T0.isoformat(), "shifted forward by the measured offset"
    assert src.clocks()["clocks"][0]["source_id"] == "home_assistant"
    await gen.aclose()


@pytest.mark.asyncio
async def test_a_timezone_sized_skew_is_reported_rather_than_absorbed():
    store = HAPresenceStore(":memory:")
    store.map("binary_sensor.hall_motion", "hall", "pir")
    wrong_tz = (T0 - timedelta(hours=1)).isoformat()
    ha = FakeHA({"binary_sensor.hall_motion": state("on", last_changed=wrong_tz)})
    src = HomeAssistantSource(store, ha, interval=0.05,
                              timebase=TimeBase(now_fn=lambda: T0))
    gen = src.events()
    await gen.__anext__()
    assert src.clocks()["unusable"] == ["home_assistant"]
    assert "timezone" in src.clocks()["clocks"][0]["note"]
    await gen.aclose()


# -- The API, and the wiring behind it -----------------------------------------

def _app(monkeypatch, *, ha=True, tmp=":memory:"):
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    if ha:
        # Port 1 on loopback: refuses instantly, never leaves the machine, and
        # never resolves a name. A hostname here would make these tests depend on
        # whatever network they run on — and would actually CONTACT a real Home
        # Assistant on the machine of anybody who has one.
        monkeypatch.setenv("WAVR_HA_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("WAVR_HA_TOKEN", "t0ken")
    else:
        monkeypatch.delenv("WAVR_HA_URL", raising=False)
        monkeypatch.delenv("WAVR_HA_TOKEN", raising=False)
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return app, TestClient(app, headers={"X-Wavr-Local": "1"})


def test_mapping_through_the_api_brings_the_source_up_without_a_restart(monkeypatch, tmp_path):
    """An operator who maps their first sensor should see it working, not read a
    note about restarting Wavr."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app, client = _app(monkeypatch)
    with client as c:
        before = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "home_assistant" not in before, "nothing mapped, nothing polled"

        r = c.put("/api/ha/presence/binary_sensor.hall_motion",
                  json={"room": "hall", "modality": "pir"})
        assert r.status_code == 200 and r.json()["room"] == "hall"

        after = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "home_assistant" in after


def test_unmapping_the_last_sensor_stops_the_polling(monkeypatch, tmp_path):
    """Rather than leaving a source quietly talking to a Home Assistant nobody
    is using any more."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app, client = _app(monkeypatch)
    with client as c:
        c.put("/api/ha/presence/binary_sensor.hall_motion", json={"room": "hall"})
        c.delete("/api/ha/presence/binary_sensor.hall_motion")
        names = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "home_assistant" not in names


def test_a_mapping_without_a_room_is_a_400_not_a_500(monkeypatch, tmp_path):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app, client = _app(monkeypatch)
    with client as c:
        r = c.put("/api/ha/presence/binary_sensor.x", json={"room": ""})
        assert r.status_code == 400


def test_suggestions_say_ha_is_not_configured_rather_than_failing(monkeypatch, tmp_path):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app, client = _app(monkeypatch, ha=False)
    with client as c:
        r = c.get("/api/ha/presence/suggestions")
        assert r.status_code == 409
        assert "not configured" in r.json()["detail"]


def test_an_unreachable_home_assistant_is_a_502_not_a_wavr_bug(monkeypatch, tmp_path):
    """"Check your Home Assistant" and "file a Wavr bug" are different next
    steps, and the status code is what tells them apart."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app, client = _app(monkeypatch)
    with client as c:
        r = c.get("/api/ha/presence/suggestions")
        assert r.status_code == 502


# -- The loop Wavr must not close ----------------------------------------------

def test_wavrs_own_sensors_cannot_be_mapped_back_in():
    """Wavr publishes its conclusions to Home Assistant over MQTT Discovery.
    Reading one back as evidence closes a loop: Wavr decides the kitchen is
    occupied, publishes it, reads its own publication as an independent sensor,
    and reinforces itself. The room then holds occupancy on the strength of
    nothing — and every screen agrees, because from the inside it looks real."""
    from wavr.ha_presence import is_wavr_entity
    store = HAPresenceStore(":memory:")
    with pytest.raises(HAPresenceError, match="own evidence"):
        store.map("binary_sensor.wavr_kitchen_occupancy", "kitchen")
    assert is_wavr_entity("binary_sensor.wavr_hall_occupancy")


def test_the_refusal_is_at_the_store_not_only_in_the_suggestions():
    """An operator can map an entity id by hand. The suggestion list is a
    convenience, not a gate."""
    import inspect
    from wavr import ha_presence
    src = inspect.getsource(ha_presence.HAPresenceStore.map)
    assert "is_wavr_entity" in src


def test_a_renamed_wavr_entity_is_still_caught_by_its_friendly_name():
    from wavr.ha_presence import is_wavr_entity
    assert is_wavr_entity({"entity_id": "binary_sensor.hall_2",
                           "friendly_name": "Wavr Hall occupancy"})


def test_an_ordinary_sensor_is_not_mistaken_for_one_of_wavrs():
    from wavr.ha_presence import is_wavr_entity
    assert is_wavr_entity({"entity_id": "binary_sensor.hall_motion",
                           "friendly_name": "Hall motion"}) is False


def test_a_wavr_entity_is_listed_with_the_reason_rather_than_hidden():
    """An operator scanning for their hall sensor and finding Wavr's own "Hall
    occupancy" simply missing would reasonably assume something is broken."""
    rows = suggest([{"entity_id": "binary_sensor.wavr_hall_occupancy",
                     "device_class": "occupancy",
                     "friendly_name": "Wavr Hall occupancy"}], ["hall"])
    assert len(rows) == 1
    assert rows[0]["suggested_room"] == ""
    assert "own evidence" in rows[0]["refused"]
