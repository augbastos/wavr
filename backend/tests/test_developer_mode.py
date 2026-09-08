"""Developer Mode, and the simulator that makes it worth having.

The two properties under test:

  * the tools are OFF for a household and reachable only by a local admin when
    on — a hidden-but-open route is the worst of both;
  * simulated evidence carries a `sim:` label from the source all the way to
    every surface, so a simulated room can never be mistaken for a real one.

The second is not a nicety. A simulator producing indistinguishable events would
be a machine for generating convincing false claims about a house, which is the
one thing this codebase spends most of its effort refusing to do.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.simulator import (
    SCENARIOS, SIM_PREFIX, SimulationRun, get, is_simulated, list_scenarios,
    rooms_touched, sensors_used,
)
from wavr.storage import Storage


def _client(monkeypatch, tmp_path, developer=True):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    if developer:
        monkeypatch.setenv("WAVR_DEVELOPER_MODE", "1")
    else:
        monkeypatch.delenv("WAVR_DEVELOPER_MODE", raising=False)
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


# -- The label -----------------------------------------------------------------

def test_every_scenario_event_is_labelled_at_the_source():
    """Not at the API, not at the renderer. At the source, so no surface can
    forget."""
    for scenario in SCENARIOS.values():
        for step in scenario.steps:
            assert step.event.sensor_id.startswith(SIM_PREFIX), scenario.key
            assert is_simulated(step.event.sensor_id)


def test_a_real_sensor_id_is_not_mistaken_for_a_simulated_one():
    assert is_simulated("hall-camera") is False
    assert is_simulated("") is False


def test_the_scenario_list_says_what_the_label_guarantees():
    assert "never indistinguishable" in list_scenarios()["note"]


# -- Determinism ---------------------------------------------------------------

def test_a_scenario_produces_identical_events_every_run():
    """What lets a developer assert against one, and what lets a bug report say
    "run occupancy_arrives and watch step 4"."""
    first = [s.event.ts for s in get("occupancy_arrives").steps]
    second = [s.event.ts for s in get("occupancy_arrives").steps]
    assert first == second
    assert first[0] != first[1], "the steps are spread over time"


def test_a_run_walks_the_steps_once():
    run = SimulationRun(get("occupancy_arrives"))
    seen = []
    while True:
        step = run.next_step()
        if step is None:
            break
        seen.append(step)
    assert len(seen) == len(get("occupancy_arrives").steps) and run.finished
    assert run.next_step() is None


# -- The scenarios exist because applications get these wrong ------------------

def test_the_count_disappearing_scenario_ends_with_presence_but_no_count():
    """The case that catches almost everybody: an application showing "2 people"
    when the count becomes unknown must stop showing the number."""
    steps = get("count_appears_and_vanishes").steps
    assert steps[1].event.count == 2
    assert steps[-1].event.count is None
    assert steps[-1].event.presence is True


def test_the_offline_scenario_deliberately_ends_in_silence():
    """The silence IS the scenario. Fusion's freshness decay is the thing a
    developer needs to watch."""
    scenario = get("sensor_goes_offline")
    assert len(scenario.steps) == 2
    assert scenario.duration_s < 10


def test_the_two_room_scenario_refuses_to_claim_a_person_moved():
    teaches = get("two_rooms_change_together").teaches
    assert "person.entered_room" in teaches
    assert "your inference" in teaches


def test_every_scenario_says_what_it_teaches():
    """The list should read as a set of problems, not a set of button labels."""
    for s in SCENARIOS.values():
        assert s.teaches, s.key


def test_the_rooms_a_scenario_will_touch_are_published():
    """So an operator can see it is about to put simulated people in their
    actual kitchen."""
    assert rooms_touched(get("two_rooms_change_together")) == ["hall", "kitchen"]
    assert all(s.startswith(SIM_PREFIX)
               for s in sensors_used(get("occupancy_arrives")))


# -- The gate ------------------------------------------------------------------

def test_developer_mode_is_off_for_a_household(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, developer=False) as c:
        r = c.get("/api/dev/status")
        assert r.status_code == 403
        # A sentence naming the switch, not a bare 404 that sends somebody
        # looking for a Core version with the feature.
        assert "Settings" in r.json()["detail"]


def test_the_routes_exist_whether_or_not_the_switch_is_on(monkeypatch, tmp_path):
    """A hidden-but-open route is the worst of both: invisible to the person who
    owns the box and reachable by anything that guesses the path. So the switch
    controls VISIBILITY and the auth gate controls access."""
    with _client(monkeypatch, tmp_path, developer=False) as c:
        assert c.get("/api/dev/status").status_code == 403, "gated, not absent"


def test_developer_status_is_one_response(monkeypatch, tmp_path):
    """Discovering a platform one endpoint at a time is how people conclude a
    capability does not exist."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.get("/api/dev/status").json()
        # From `contracts`, not a literal. A literal here asserts that the
        # number never moves, which is the opposite of what a version is for --
        # and it fails the day a shape legitimately changes, in the test rather
        # than in the thing that broke.
        from wavr.contracts import version as contract_version
        assert (body["protocol"]["experience_context"]
                == contract_version("experience_context"))
        assert "/ws/events" in body["endpoints"].values()
        assert set(body["sdks"]) == {"javascript", "python", "kotlin"}


# -- Running a scenario --------------------------------------------------------

def test_a_scenario_runs_into_the_real_fusion_engine(monkeypatch, tmp_path):
    """There is no parallel simulated engine. One that ran against different
    fusion would eventually disagree with production, and the disagreement would
    be found by somebody's application rather than by us."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/scenarios/occupancy_arrives/run",
                      json={"realtime": False}).json()
        assert len(body["steps"]) == len(get("occupancy_arrives").steps)
        # By the room it was GIVEN, not the one the scenario names — the run
        # remaps onto this Space's rooms and publishes the mapping.
        room = body["room_mapping"]["kitchen"]
        assert body["rooms"][room] is not None
        assert "occupied" in body["rooms"][room]


def test_the_resulting_state_carries_the_simulated_label(monkeypatch, tmp_path):
    """Through fusion into `sources[]`, which is what every other surface reads."""
    with _client(monkeypatch, tmp_path) as c:
        run = c.post("/api/dev/scenarios/occupancy_arrives/run",
                     json={"realtime": False}).json()
        state = c.get("/api/dev/status").json()
        assert state["simulated_rooms"] == [run["room_mapping"]["kitchen"]]


def test_the_simulated_room_list_is_derived_not_a_flag(monkeypatch, tmp_path):
    """A room is simulated for exactly as long as simulated evidence is still
    voting in it — a fact about fusion, not about whether a run happened."""
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/dev/status").json()["simulated_rooms"] == []


def test_a_missing_scenario_is_a_404(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.post("/api/dev/scenarios/nope/run",
                      json={"realtime": False}).status_code == 404


def test_the_precision_scenario_actually_moves_the_room_up_the_ladder(monkeypatch, tmp_path):
    """The scenario claims capabilities are not fixed. This checks the claim
    against the real engine rather than against the scenario's own description."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/scenarios/precision_improves/run",
                      json={"realtime": False}).json()
        office = body["rooms"][body["room_mapping"]["office"]]
        assert office["precision_level"] in ("count", "position")


def test_a_scenario_cannot_show_a_capability_the_product_lacks(monkeypatch, tmp_path):
    """Scenarios build SensingEvents and hand them to the real engine. A PIR
    cannot produce a count here for the same reason it cannot in a real house."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/scenarios/count_appears_and_vanishes/run",
                      json={"realtime": False}).json()
        living = body["rooms"][body["room_mapping"]["living"]]
        # The camera stopped reporting and only the PIR is fresh, so the room
        # keeps presence and cannot honestly hold a count from a PIR.
        assert living["precision_level"] in ("room", "count")


# -- The manifest validator ----------------------------------------------------

def test_the_validator_answers_is_my_document_correct(monkeypatch, tmp_path):
    """Different question from "will it work here". A developer with a typo who
    gets UNSUPPORTED for a house-shaped reason goes to the wrong place."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/manifest/validate", json={"manifest": {
            "id": "x", "name": "X", "requires": ["presence"],
            "scopes": ["room.presence"]}}).json()
        assert body["valid"] is True and body["warnings"] == []
        assert "different question" in body["note"]


def test_a_broken_manifest_is_explained_rather_than_rejected(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/manifest/validate",
                      json={"manifest": {"name": "no id"}}).json()
        assert body["valid"] is False and body["error"]


def test_a_valid_manifest_can_still_carry_warnings(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/manifest/validate", json={"manifest": {
            "id": "x", "name": "X", "requires": ["count"],
            "scopes": ["room.presence"]}}).json()
        assert body["valid"] is True
        assert any("room.count" in w for w in body["warnings"])


# -- The provider view ---------------------------------------------------------

def test_providers_and_their_health_arrive_together(monkeypatch, tmp_path):
    """"What could I use" and "is it working" are the same question when you are
    debugging."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.get("/api/dev/providers").json()
        assert body["providers"], "the catalogue"
        assert "health" in body, "and the live state"


# -- A scenario cannot lie about itself ----------------------------------------

def test_every_scenario_produces_the_end_state_it_claims():
    """The test that keeps the teaching honest.

    Three of these scenarios were WRONG when first written: they described an
    outcome fusion correctly does not produce on that timescale — a room does not
    go vacant the moment a camera reports empty, because a 45-second dwell exists
    so one dropped frame cannot fire a "nobody home" automation on somebody
    sitting still. A scenario that ignored the dwell would teach a developer to
    expect something the product does not do, which is worse than no scenario.
    """
    from wavr.fusion import FusionEngine
    for key, scenario in SCENARIOS.items():
        assert scenario.expects, f"{key} claims no outcome, so nothing checks it"
        engine = FusionEngine()
        for step in scenario.steps:
            engine.update(step.event)
        for room, expected in scenario.expects.items():
            state = engine.state(room)
            assert state is not None, f"{key}: no state for {room}"
            actual = state.to_dict()
            for field, want in expected.items():
                assert actual.get(field) == want, (
                    f"{key} / {room}: says {field}={want!r}, "
                    f"the engine produced {actual.get(field)!r}")


def test_a_scenarios_declared_outcome_is_published():
    """So a developer can read what to expect before running it, and so a
    disagreement between the story and the engine is visible rather than
    discovered."""
    row = get("occupancy_arrives").to_dict()
    assert row["expects"]["kitchen"]["occupied"] is False


def test_the_dwell_is_visible_in_the_occupancy_scenario():
    """It ends vacant, and it takes more than the 45 seconds that makes it so."""
    scenario = get("occupancy_arrives")
    assert scenario.expects["kitchen"]["occupied"] is False
    assert scenario.duration_s > 45


# -- The reference experiences are reachable -----------------------------------

def test_the_experiences_are_served_and_listed(monkeypatch, tmp_path):
    """One click from the machine that has the data, rather than something a
    developer hosts themselves and then fights CORS over."""
    with _client(monkeypatch, tmp_path) as c:
        listed = c.get("/api/dev/status").json()["experiences"]
        assert {e["name"] for e in listed} == {"spatial-web", "capability-aware",
                                               "anchor-demo"}
        for entry in listed:
            page = c.get(entry["url"])
            assert page.status_code == 200, entry["url"]
            assert "sdk/javascript/wavr.js" in page.text


def test_the_sdk_the_pages_import_is_served_too(monkeypatch, tmp_path):
    """The real file, not a copy. A served copy would drift from the one in the
    repository, and the drift would be found by whoever trusted the page."""
    with _client(monkeypatch, tmp_path) as c:
        js = c.get("/sdk/javascript/wavr.js")
        assert js.status_code == 200
        assert "class WavrClient" in js.text


def test_the_experiences_are_not_served_with_developer_mode_off(monkeypatch, tmp_path):
    """These pages read the Space. One served to anybody who can reach the port
    is a page that reads the Space for anybody."""
    with _client(monkeypatch, tmp_path, developer=False) as c:
        assert c.get("/experiences/spatial-web/").status_code == 403
        assert c.get("/sdk/javascript/wavr.js").status_code == 403


def test_a_path_traversal_in_an_experience_name_is_refused(monkeypatch, tmp_path):
    """The name comes from a URL, and `..` in it would otherwise read any file
    the process can.
    A literal `/experiences/../` is NOT among the cases: the HTTP client
    normalises it away before the request leaves, so testing it would check
    httpx rather than the route. These are the encoded forms that survive to
    the handler.
    """
    with _client(monkeypatch, tmp_path) as c:
        for evil in ("%2e%2e", "%2e%2e%2f%2e%2e%2fbackend", "spatial-web%2f.."):
            r = c.get(f"/experiences/{evil}/")
            assert r.status_code in (403, 404, 405), evil
            assert "def create_app" not in r.text, f"{evil} leaked a source file"


# -- A scenario runs in YOUR rooms ---------------------------------------------

def test_a_scenario_is_remapped_onto_the_rooms_the_space_actually_has():
    """Watching your own kitchen become occupied is the demonstration. Watching
    an invented one is a puzzle — and a room that appears on the dashboard
    without being on the floor plan looks like a bug."""
    from wavr.simulator import remap
    scenario, mapping = remap(get("occupancy_arrives"), ["sala", "quarto"])
    assert mapping == {"kitchen": "sala"}
    assert {s.event.room for s in scenario.steps} == {"sala"}
    assert set(scenario.expects) == {"sala"}


def test_remapping_is_deterministic():
    """A scenario whose rooms moved between runs would be useless to assert
    against."""
    from wavr.simulator import remap
    rooms = ["sala", "quarto", "quintal"]
    a, ma = remap(get("two_rooms_change_together"), rooms)
    b, mb = remap(get("two_rooms_change_together"), rooms)
    assert ma == mb
    assert [s.event.room for s in a.steps] == [s.event.room for s in b.steps]


def test_remapping_keeps_the_simulated_label():
    """The one field that must survive every transformation."""
    from wavr.simulator import remap
    scenario, _ = remap(get("occupancy_arrives"), ["sala"])
    assert all(is_simulated(s.event.sensor_id) for s in scenario.steps)


def test_a_space_with_no_floor_plan_keeps_the_scenarios_own_names():
    """A developer with a fresh install is exactly who most needs this to run."""
    from wavr.simulator import remap
    scenario, mapping = remap(get("occupancy_arrives"), [])
    assert mapping == {}
    assert {s.event.room for s in scenario.steps} == {"kitchen"}


def test_the_mapping_is_published_with_the_run(monkeypatch, tmp_path):
    """A silent rename leaves somebody watching the wrong card."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/dev/scenarios/occupancy_arrives/run",
                      json={"realtime": False}).json()
        assert "room_mapping" in body
        # The default house map is in Portuguese, so a remap must have happened.
        assert body["room_mapping"], "kitchen is not a room in the default map"
        assert list(body["rooms"]) == list(body["room_mapping"].values())


# -- the scenario has to reach the engine ALIVE --------------------------------

def test_a_scenario_actually_moves_a_room_in_a_production_shaped_app(tmp_path,
                                                                     monkeypatch):
    """The onboarding this file's other tests describe, run for real.

    Scenario steps carry `simulator.EPOCH + at_s`, a fixed 2026-01-01, which is
    what makes a scenario deterministic and assertable. Fed to production's
    FusionEngine — which ages evidence against the wall clock — every reading
    arrived roughly eight months stale: `health: "dead"`, mass 0.0, and the
    room stayed `occupied: false, confidence: 0.0, precision_level: "none"`.
    A developer with no sensors followed sdk/README.md's "Developing without
    the sensors", ran a scenario, watched nothing happen, and had no way to
    tell whether the feature or their code was broken.

    The existing tests missed it because their fixture INJECTS a FusionEngine,
    and the injected one is not the one `create_app` builds. So this builds the
    app the way `wavr.serve` does — no injected fusion, no injected storage —
    and asserts the thing a developer is actually looking for: a room that
    changed.
    """
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "sim.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_DEVELOPER_MODE", "1")
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})

    keys = [s["key"] for s in c.get("/api/dev/scenarios").json()["scenarios"]]
    assert "count_appears_and_vanishes" in keys, keys
    out = c.post("/api/dev/scenarios/count_appears_and_vanishes/run",
                 json={"realtime": False}).json()

    rooms = {r: s for r, s in (out.get("rooms") or {}).items() if s}
    assert rooms, out
    assert any(s.get("occupied") for s in rooms.values()), (
        f"every room came back vacant: {rooms}. The scenario's evidence is "
        f"reaching fusion already decayed — check that the steps are being "
        f"restamped at ingest rather than kept at simulator.EPOCH.")
    live = [s for s in rooms.values() if s.get("occupied")]
    assert any((s.get("confidence") or 0) > 0.2 for s in live), (
        f"occupied at ~0% confidence is the stale-evidence signature: {live}")
    assert any(src.get("health") == "fresh"
               for s in live for src in (s.get("sources") or [])), (
        "no source in an occupied room is fresh, so nothing a developer "
        "watches will move")


def test_the_story_keeps_its_shape_when_it_is_restamped():
    """Restamping must move the timeline, not flatten it. If every step landed
    at the same instant, a scenario about something happening OVER TIME —
    a sensor going offline, precision degrading — would stop being about
    anything, and would still look fine in the assertion above."""
    from datetime import datetime, timezone
    from wavr.api_developer import _restamp
    from wavr.simulator import SCENARIOS

    sc = SCENARIOS["count_appears_and_vanishes"]
    end = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    stamps = []
    for step in sc.steps:
        from datetime import timedelta
        at = end - timedelta(seconds=max(0.0, sc.duration_s - step.at_s))
        moved = _restamp(step, at)
        stamps.append(moved.event.ts)
        assert moved.at_s == step.at_s, "at_s is the story, and must not move"
        assert moved.event.room == step.event.room
        assert moved.event.sensor_id == step.event.sensor_id
    assert len(set(stamps)) > 1, (
        f"every step landed at the same instant: {set(stamps)}")
    assert stamps == sorted(stamps), "the story came back out of order"
