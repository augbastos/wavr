"""The context an application consumes, and the sentence it must never have to
infer.

`limitations` is the field these tests are really about. An application that has
to work out what Wavr cannot answer, by noticing which fields are missing, will
guess "empty" every time — because empty is the value that makes its code
simpler.
"""
import json

from wavr.contracts import version as contract_version
from wavr.experience import (
    CAP_ANCHORS, CAP_AUDIO, CAP_COUNT, CAP_DISPLAY, CAP_POSITION, CAP_PRESENCE,
    build_context, build_space_context,
)

SPACE = {"space_id": "sp_1", "name": "My Home"}


def cov(sensor_id="cam1", room="kitchen", modality="camera", health="ok",
        precision="count"):
    return {"sensor_id": sensor_id, "room": room, "modality": modality,
            "health": health, "precision_level": precision}


def rs(**kw):
    base = {"occupied": True, "confidence": 0.8, "person_count": 2,
            "precision_level": "count"}
    base.update(kw)
    return base


# -- Capabilities come from what WORKS -----------------------------------------

def test_capabilities_are_built_from_sensors_that_are_observing():
    """A camera that is switched off makes a room watchable, not watched. An
    application told it has `count` in a dark room shows a headcount that never
    changes."""
    ctx = build_context(room="kitchen", coverage_rows=[cov(health="disabled")])
    assert ctx.capabilities == ()
    assert ctx.can(CAP_PRESENCE) is False


def test_a_working_camera_offers_presence_and_count():
    ctx = build_context(room="kitchen", coverage_rows=[cov()])
    assert ctx.can(CAP_PRESENCE) and ctx.can(CAP_COUNT)
    assert not ctx.can(CAP_POSITION)


def test_position_is_only_offered_when_a_sensor_earned_it():
    ctx = build_context(room="kitchen",
                        coverage_rows=[cov(precision="position")])
    assert ctx.can(CAP_POSITION)


def test_a_house_wide_sensor_does_not_give_a_room_presence():
    """One antenna localizes to the house. Claiming room presence from it would
    put a person in whichever room the app happened to ask about."""
    ctx = build_context(room="kitchen",
                        coverage_rows=[cov(modality="network", precision="house")])
    assert CAP_PRESENCE not in ctx.capabilities


def test_anchors_and_screens_are_capabilities_too():
    ctx = build_context(
        room="kitchen",
        anchors=[{"anchor_id": "a1", "room": "kitchen", "name": "Counter"}],
        devices=[{"device_id": "d1", "room": "kitchen", "display": True,
                  "audio": True}])
    assert {CAP_ANCHORS, CAP_DISPLAY, CAP_AUDIO} <= set(ctx.capabilities)


# -- Occupancy, and the most tempting lie in the system ------------------------

def test_an_uncounted_room_reports_null_never_zero():
    ctx = build_context(room="kitchen", room_state=rs(person_count=None),
                        coverage_rows=[cov()])
    d = ctx.to_dict()
    assert d["occupancy"] is None
    assert d["occupancy_known"] is False


def test_a_real_zero_survives_as_a_zero():
    ctx = build_context(room="kitchen", room_state=rs(person_count=0,
                                                      occupied=False),
                        coverage_rows=[cov()])
    assert ctx.to_dict()["occupancy"] == 0
    assert ctx.to_dict()["occupancy_known"] is True


def test_a_room_nothing_reported_on_has_unknown_occupancy():
    ctx = build_context(room="attic")
    assert ctx.occupied is None and ctx.occupancy is None


# -- Limitations ---------------------------------------------------------------

def test_an_unsensed_room_says_so_in_a_sentence():
    lim = build_context(room="attic").limitations
    assert lim and "cannot tell whether anybody is here" in lim[0]


def test_a_room_whose_sensors_all_failed_says_nothing_is_being_observed():
    ctx = build_context(room="kitchen", coverage_rows=[cov(health="offline")])
    assert "not reporting" in ctx.limitations[0]


def test_a_partly_broken_room_says_WHICH_sensor_is_quiet_without_naming_it():
    """An application has to be able to tell "one of three sensors is down" from
    "the only sensor is down" — those justify completely different confidence in
    the same reading. So the sentence must identify the sensor.

    It must not identify it by the name an OPERATOR typed. This test used to
    assert the raw id appeared, which is how the leak survived a review: the
    test encoded the bug as the requirement.
    """
    ctx = build_context(room="kitchen", coverage_rows=[
        cov("cam1"), cov("Alex's radar", modality="mmwave",
                         health="offline")])
    said = " ".join(ctx.limitations)
    assert "mmwave 1" in said, said
    assert "Alex" not in said and "radar" not in said.replace("mmwave", "")


def test_the_prose_and_the_sensor_list_agree_on_what_a_sensor_is_called():
    """Two functions deriving labels separately is the bug one step later: an
    application reading "mmwave 2 is not reporting" needs to find a `mmwave 2`
    in the list it was given."""
    ctx = build_context(room="kitchen", coverage_rows=[
        cov("a", modality="mmwave"),
        cov("b", modality="mmwave", health="offline")]).to_dict()
    listed = {s["label"] for s in ctx["sensors"]}
    assert "mmwave 2" in listed
    assert any("mmwave 2" in line for line in ctx["limitations"])


def test_a_presence_only_room_says_it_cannot_count():
    ctx = build_context(room="hall",
                        coverage_rows=[cov(room="hall", modality="pir",
                                           precision="room")])
    assert any("not how many" in s for s in ctx.limitations)


def test_a_counting_room_says_it_cannot_place():
    ctx = build_context(room="kitchen", coverage_rows=[cov()])
    assert any("not place them" in s for s in ctx.limitations)


def test_the_radar_phantom_is_stated_rather_than_left_in_a_docstring():
    """fusion documents the honest cost — a very still person disappears from a
    radar, so a radar-only room keeps a bounded phantom — where no application
    can read it. Here it becomes a field."""
    ctx = build_context(room="bedroom",
                        coverage_rows=[cov(room="bedroom", modality="mmwave")])
    assert any("stays very still" in s for s in ctx.limitations)


def test_a_camera_room_does_not_carry_the_phantom_warning():
    """A camera SEES the room, so a still person stays visible. Warning about it
    anyway would train people to ignore the field."""
    ctx = build_context(room="kitchen", coverage_rows=[cov()])
    assert not any("stays very still" in s for s in ctx.limitations)


def test_the_worst_limitation_comes_first():
    """An application rendering only the first one should get the one that most
    changes what it can do."""
    ctx = build_context(room="kitchen", coverage_rows=[cov(health="offline")])
    assert "not reporting" in ctx.limitations[0]


def test_the_note_says_absence_is_not_a_negative():
    assert "never that the room is" in build_context(room="x").to_dict()["note"]


# -- What a context refuses to carry -------------------------------------------

def test_no_identity_ever_reaches_a_context():
    """An experience that can name the people in a room is a different product
    with a different consent conversation attached."""
    ctx = build_context(room="kitchen",
                        room_state={**rs(), "identities": [{"name": "Alex"}]},
                        coverage_rows=[cov()])
    assert "Alex" not in str(ctx.to_dict())
    assert "identities" not in ctx.to_dict()


def test_no_target_coordinates_reach_a_context():
    """Anchors describe furniture; targets describe people."""
    ctx = build_context(room="kitchen",
                        room_state={**rs(), "targets": [{"x": 1.0, "y": 2.0}]},
                        coverage_rows=[cov(precision="position")])
    assert "targets" not in ctx.to_dict()


def test_the_pairing_name_never_reaches_an_experience():
    """The name is typed by whoever paired the device and is very often
    "Alex's iPhone" — which is an identity, and this module promises
    identities never appear. An earlier version returned it, and the
    contradiction between the promise and the code was caught in review."""
    ctx = build_context(room="kitchen", devices=[
        {"device_id": "d1", "name": "Alex's iPhone", "room": "kitchen",
         "display": True, "person_id": "p_augusto", "token": "secret"}])
    d = ctx.to_dict()["devices"][0]
    assert "Alex" not in str(ctx.to_dict())
    assert "name" not in d
    assert "person_id" not in d and "token" not in d


def test_a_device_gets_a_label_derived_from_what_it_is_and_where():
    """"living room screen" is enough for an application to ask "Continue on
    which?", and tells an experience nothing about whose phone it is."""
    ctx = build_context(room="living", devices=[
        {"device_id": "d1", "name": "Alex's TV", "room": "living",
         "display": True}])
    assert ctx.to_dict()["devices"][0]["label"] == "living screen"


def test_two_of_a_kind_are_distinguishable_without_naming_anybody():
    ctx = build_context(room="living", devices=[
        {"device_id": "a", "name": "Sam's tablet", "room": "living", "display": True},
        {"device_id": "b", "name": "Bea's tablet", "room": "living", "display": True}])
    labels = [d["label"] for d in ctx.to_dict()["devices"]]
    assert labels == ["living screen", "living screen 2"]
    assert "Sam" not in str(labels) and "Bea" not in str(labels)


def test_a_device_that_never_said_is_not_reported_as_a_no():
    """An application choosing a screen should be able to tell "no display" from
    "did not say"."""
    ctx = build_context(room="kitchen", devices=[{"device_id": "d1",
                                                  "room": "kitchen"}])
    assert ctx.to_dict()["devices"][0]["display"] is None


def test_devices_in_other_rooms_are_not_offered():
    ctx = build_context(room="kitchen", devices=[
        {"device_id": "d1", "room": "hall", "display": True}])
    assert ctx.devices == () and CAP_DISPLAY not in ctx.capabilities


# -- The whole Space -----------------------------------------------------------

def test_the_space_view_names_the_rooms_nothing_can_answer_for():
    """So an application does not have to scan every room to discover the house
    is half dark."""
    out = build_space_context(
        rooms=["kitchen", "attic"], space=SPACE,
        states={"kitchen": rs()}, coverage_rows=[cov()])
    assert out["rooms_without_sensing"] == ["attic"]
    assert out["space"]["name"] == "My Home"


def test_each_room_carries_its_own_limitations():
    out = build_space_context(rooms=["kitchen", "attic"], space=SPACE,
                              states={"kitchen": rs()}, coverage_rows=[cov()])
    by_room = {r["room"]: r for r in out["rooms"]}
    assert by_room["attic"]["limitations"]
    assert by_room["kitchen"]["limitations"] != by_room["attic"]["limitations"]


def test_the_space_note_warns_against_treating_rooms_as_equal():
    out = build_space_context(rooms=[], space=SPACE)
    assert "darkest room" in out["note"]


def test_the_context_is_versioned():
    """The contract applications build against. Unversioned is how a field can
    never be changed again."""
    assert (build_context(room="x").to_dict()["protocol_version"]
            == contract_version("experience_context"))


# -- Over HTTP, against a real Core --------------------------------------------

def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_the_space_context_answers_for_every_room(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        body = c.get("/api/experience/context").json()
        assert body["protocol_version"] == contract_version("experience_context")
        assert body["rooms"], "the default house has rooms"
        for room in body["rooms"]:
            assert "limitations" in room and "capabilities" in room


def test_an_anchor_shows_up_in_its_rooms_context(monkeypatch, tmp_path):
    """The anchors unit is load-bearing here rather than a model with no
    consumer: creating one changes what an application is told."""
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        before = c.get(f"/api/experience/context/{room}").json()
        assert before["anchors"] == []

        c.post("/api/anchors", json={"name": "Counter", "room": room})
        after = c.get(f"/api/experience/context/{room}").json()
        assert [a["name"] for a in after["anchors"]] == ["Counter"]
        assert "anchors" in after["capabilities"]


def test_a_room_that_does_not_exist_is_a_404_not_an_empty_context(monkeypatch, tmp_path):
    """An empty context reads as "this room has no sensors", and an application
    would go looking for hardware to fix instead of correcting a typo."""
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/experience/context/nowhere").status_code == 404


def test_an_unsensed_house_says_so_rather_than_reporting_empty_rooms(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        body = c.get("/api/experience/context").json()
        assert body["rooms_without_sensing"], "no sources are configured here"
        first = body["rooms"][0]
        assert first["occupancy"] is None and first["occupancy_known"] is False


# -- The operator's own words, in the field beside the one already fixed -------

def test_a_sensor_never_carries_the_name_an_operator_typed():
    """`sensor_coverage` uses the CAMERA'S NAME as its `sensor_id`, and the
    node's name for a node. In a fixture that reads "cam1". In a real house it
    reads "<person>'s office cam" — the identity this module's docstring says
    never appears, in the field directly beside `devices`, which an appsec pass
    already had to fix for exactly this reason.

    This is the guard on the second copy. The first copy was found by a review;
    nothing would have found this one.
    """
    ctx = build_context(
        room="kitchen",
        room_state={"occupied": True, "confidence": 0.8,
                    "precision_level": "room"},
        coverage_rows=[cov(sensor_id="Alex's office cam", room="kitchen"),
                       cov(sensor_id="baby monitor", room="kitchen",
                           modality="mmwave")],
    ).to_dict()

    blob = json.dumps(ctx)
    assert "Alex" not in blob and "baby monitor" not in blob,         f"an operator's own words reached an experience: {blob[:300]}"

    labels = [s["label"] for s in ctx["sensors"]]
    assert labels == ["camera 1", "mmwave 1"], labels
    # What an application actually needs is still there: a stale answer and a
    # wrong one are distinguishable only if health survives.
    assert all("health" in s and "precision_level" in s for s in ctx["sensors"])


def test_two_sensors_of_the_same_kind_stay_distinguishable():
    """A label that collapsed both cameras into one string would make a room
    with a working camera and a dead one indistinguishable from a room with
    one dead camera."""
    ctx = build_context(
        room="hall",
        room_state={"occupied": True, "confidence": 0.5,
                    "precision_level": "room"},
        coverage_rows=[cov(sensor_id="a", room="hall", health="ok"),
                       cov(sensor_id="b", room="hall", health="offline")],
    ).to_dict()
    labels = [s["label"] for s in ctx["sensors"]]
    assert labels == ["camera 1", "camera 2"]
    assert [s["health"] for s in ctx["sensors"]] == ["ok", "offline"]


def test_simulated_evidence_is_a_boolean_not_a_prefix_to_parse():
    """The guarantee is that simulated evidence is never indistinguishable from
    real evidence. That is only worth something if a client cannot fail to
    notice it — and a client that has to remember `startswith("sim:")` can."""
    ctx = build_context(
        room="lab",
        room_state={"occupied": True, "confidence": 0.9,
                    "precision_level": "room"},
        coverage_rows=[cov(sensor_id="sim:kitchen-cam", room="lab"),
                       cov(sensor_id="ha:binary_sensor.hall", room="lab",
                           modality="pir"),
                       cov(sensor_id="ext:vendor-1", room="lab",
                           modality="node"),
                       cov(sensor_id="hall-cam", room="lab",
                           modality="mmwave")],
    ).to_dict()
    by_modality = {s["modality"]: s for s in ctx["sensors"]}
    assert by_modality["camera"]["simulated"] is True
    assert by_modality["camera"]["source"] == "simulated"
    assert by_modality["pir"]["source"] == "home_assistant"
    assert by_modality["node"]["source"] == "external"
    assert by_modality["mmwave"]["source"] == "wavr"
    assert by_modality["mmwave"]["simulated"] is False
