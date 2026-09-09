"""What the event stream and the SDKs' declared types promise, held to the wire.

Three separate promises live here because they failed the same way: something
that DESCRIBES the product — a type declaration, a document — drifted from the
thing it describes, and nothing in the build noticed.

  1. The experience context stopped shipping the operator-typed sensor name at
     protocol 2. `/ws/events` and `/api/events/recent` went on shipping it: same
     private string, same `presence:read` audience, a different route.
  2. `wavr.d.ts` declared `ContextDevice.name`, which the Core has never sent.
     TypeScript asserted a string and delivered `undefined`, and the
     capability-aware reference page rendered "on undefined".
  3. `docs/mcp-connect.md` said the HTTP transport exposes "the 4 read tools"
     while thirteen were registered, and four of them were in no table anywhere.

So each test reads the real producer, not a fixture of what it used to do.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from wavr.auth import AGENT_DEFAULT_TOOL_SCOPE, MCP_TOOL_NAMES
from wavr.experience import _visible_devices, _visible_sensors
from wavr.experience_session import targets_in
from wavr.spatial_events import (
    EV_AGREEMENT, EV_DISAGREEMENT, EV_SENSOR_OFFLINE, EV_SENSOR_ONLINE,
    SpatialEvents,
)

ROOT = Path(__file__).resolve().parents[2]
DTS = ROOT / "sdk" / "javascript" / "wavr.d.ts"


# -- fixtures shaped like the real thing ---------------------------------------
#
# `hall-cam` is not decoration. `sources/camera.py` stamps `sensor_id=self._name`
# on every event a camera produces, so this string is whatever the household
# typed into the camera form — the exact class of value protocol 2 removed.

def _src(sensor_id, modality="camera", presence=True, health="fresh"):
    return {"sensor_id": sensor_id, "modality": modality,
            "presence": presence, "health": health}


def _state(room="kitchen", sources=(), **kw):
    base = {"room": room, "occupied": True, "confidence": 0.8,
            "person_count": None, "precision_level": "room",
            "sources": list(sources), "ts": "2026-09-04T12:00:00+00:00"}
    base.update(kw)
    return base


def _flatten(event) -> str:
    """Everything in an event, as one searchable string."""
    return json.dumps(event, sort_keys=True, default=str)


# -- 1. the stream may not carry a name somebody typed -------------------------

def test_a_sensor_event_never_carries_the_operators_own_string():
    """The blocker. `/ws/events` is gated at `presence:read` — the same gate the
    redacted context is behind — so a name the context strips must not arrive
    over the socket instead."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("hall-cam"), _src("Sam office cam")]))
    out = ev.observe(_state(sources=[_src("hall-cam")]))

    assert [e["event"] for e in out] == [EV_SENSOR_OFFLINE]
    body = _flatten(out[0])
    assert "sensor_id" not in body, out[0]
    assert "Sam office cam" not in body, (
        "the event named the sensor with the string an operator typed")


def test_a_disagreement_names_its_sensors_the_same_derived_way():
    """The prose leak's sibling. `room.sensors_disagree` enumerates the room's
    sensors, and enumerating them with their ids is the same disclosure as the
    offline event, once per sensor."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("hall-cam", presence=True),
                               _src("Sam bedroom radar", "mmwave", presence=True)]))
    out = ev.observe(_state(sources=[_src("hall-cam", presence=False),
                                     _src("Sam bedroom radar", "mmwave",
                                          presence=True)]))

    row = [e for e in out if e["event"] == EV_DISAGREEMENT][0]
    body = _flatten(row)
    assert "sensor_id" not in body and "Sam bedroom radar" not in body, row
    assert {s["says"] for s in row["sensors"]} == {"occupied", "empty"}


def test_the_event_still_says_which_sensor_it_is_about():
    """Redaction that removes the subject is not redaction, it is deletion.
    `_fresh_sensors` excludes anonymous sources precisely because an event that
    cannot name the sensor is not actionable — so the derived name has to be
    there, and it has to describe the sensor."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("hall-cam"), _src("hall-radar", "mmwave")]))
    out = ev.observe(_state(sources=[_src("hall-cam")]))

    assert out[0]["label"] == "mmwave 1"
    assert out[0]["modality"] == "mmwave"
    assert out[0]["source"] == "wavr"
    assert out[0]["simulated"] is False


def test_a_sensor_keeps_its_name_from_going_quiet_to_coming_back():
    """The property a per-response label does not need and a STREAM does. An
    application pairs `sensor.offline` with the `sensor.online` that follows;
    a name that changed in between makes the pair unreadable."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("hall-cam"), _src("hall-radar", "mmwave")]))
    gone = ev.observe(_state(sources=[_src("hall-cam")]))
    back = ev.observe(_state(sources=[_src("hall-cam"), _src("hall-radar", "mmwave")]))

    assert [e["event"] for e in gone] == [EV_SENSOR_OFFLINE]
    assert [e["event"] for e in back] == [EV_SENSOR_ONLINE]
    assert gone[0]["label"] == back[0]["label"] == "mmwave 1"


def test_two_sensors_of_one_modality_stay_distinguishable():
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("cam-a"), _src("cam-b")]))
    first = ev.observe(_state(sources=[_src("cam-a")]))
    second = ev.observe(_state(sources=[]))

    assert first[0]["label"] == "camera 2"
    assert second[0]["label"] == "camera 1"


def test_where_the_evidence_came_from_survives_the_redaction():
    """`simulated` is a boolean rather than a prefix a client has to remember to
    parse, exactly as in the context. A guarantee a client can forget to check
    is not a guarantee."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("sim:kitchen-1"), _src("ha:binary_sensor.x",
                                                           "pir")]))
    out = ev.observe(_state(sources=[]))
    by_label = {e["label"]: e for e in out}

    assert by_label["camera 1"]["source"] == "simulated"
    assert by_label["camera 1"]["simulated"] is True
    assert by_label["pir 1"]["source"] == "home_assistant"
    assert by_label["pir 1"]["simulated"] is False


def test_the_names_use_the_same_vocabulary_the_context_does():
    """One vocabulary, so a person reading an alert and a person reading a
    context screen are looking at the same kind of word."""
    rows = [{"sensor_id": "hall-cam", "modality": "camera", "health": "ok",
             "precision_level": "count", "room": "kitchen"}]
    context_label = _visible_sensors(rows)[0]["label"]

    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("hall-cam")]))
    out = ev.observe(_state(sources=[]))

    assert out[0]["label"] == context_label == "camera 1"


def test_forgetting_a_room_forgets_its_sensor_names_too():
    """A deleted room that kept its numbering would name the next camera
    installed under that name "camera 2" for a room that has one."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("old-cam")]))
    ev.observe(_state(sources=[]))
    ev.forget("kitchen")

    ev.observe(_state(sources=[_src("new-cam")]))
    out = ev.observe(_state(sources=[]))
    assert out[0]["label"] == "camera 1"


def test_agreement_and_disagreement_describe_sensors_identically():
    """Two events, one vocabulary. A consumer switching on `event` must not also
    have to switch on the shape of `sensors[]`."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("cam", presence=True),
                               _src("radar", "mmwave", presence=True)]))
    disagree = ev.observe(_state(sources=[_src("cam", presence=False),
                                          _src("radar", "mmwave", presence=True)]))
    agree = ev.observe(_state(sources=[_src("cam", presence=True),
                                       _src("radar", "mmwave", presence=True)]))

    shape = {"label", "modality", "source", "simulated", "says"}
    for out, kind in ((disagree, EV_DISAGREEMENT), (agree, EV_AGREEMENT)):
        row = [e for e in out if e["event"] == kind][0]
        for sensor in row["sensors"]:
            assert set(sensor) == shape, sensor


# -- 2. a declared type may not assert a field the wire does not carry ---------

def _declared_fields(interface: str) -> set[str]:
    """The property names of one `interface`/`class` block in `wavr.d.ts`.

    A regex rather than a TypeScript parse: the declaration file is hand-written
    beside the JavaScript on purpose (the SDK is zero-build), so there is no
    compiler in this repository to ask.
    """
    src = DTS.read_text(encoding="utf-8")
    match = re.search(rf"^export (?:interface|class) {interface} \{{(.*?)^\}}",
                      src, re.S | re.M)
    assert match, f"{interface} is no longer declared in wavr.d.ts"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??\s*:", body, re.M))


def test_the_declared_device_type_matches_the_device_the_core_serves():
    """`ContextDevice.name` was declared and never served. `_visible_devices`
    derives a `label` — deliberately, because the pairing name is very often a
    person's — so the type asserted a string that was always `undefined`."""
    served = set(_visible_devices(
        [{"device_id": "d1", "room": "kitchen", "display": True,
          "audio": None, "uwb": None, "functions": ["cast"]}],
        "kitchen")[0])

    assert _declared_fields("ContextDevice") == served


def test_the_declared_sensor_type_matches_the_sensor_the_core_serves():
    served = set(_visible_sensors(
        [{"sensor_id": "hall-cam", "modality": "camera", "health": "ok",
          "precision_level": "count"}])[0])

    assert _declared_fields("ContextSensor") == served


def test_the_declared_session_target_matches_the_target_the_core_serves():
    """Same field, same reason, one module over: `experience_session.targets_in`
    builds `label` and refuses to take one from the caller."""
    served = set(targets_in("kitchen", [
        {"device_id": "tv1", "room": "kitchen", "display": True}])[0])

    assert _declared_fields("SessionTarget") == served


def test_the_declared_event_type_covers_every_field_an_event_carries():
    """The declaration may be wider than one event — an event is a union of
    shapes — but it may not be narrower than all of them put together."""
    ev = SpatialEvents()
    ev.observe(_state(sources=[_src("cam", presence=True),
                               _src("radar", "mmwave", presence=True)]))
    emitted = set()
    for state in (
        _state(occupied=False, person_count=2, precision_level="count",
               precision_next="calibrate_camera_position",
               sources=[_src("cam", presence=False),
                        _src("radar", "mmwave", presence=True)]),
        _state(sources=[_src("cam", presence=True),
                        _src("radar", "mmwave", presence=True),
                        _src("extra", "pir")]),
    ):
        for event in ev.observe(state):
            emitted |= set(event)

    declared = _declared_fields("SpatialEvent")
    assert emitted - declared == set(), (
        f"the event stream ships fields wavr.d.ts does not declare: "
        f"{sorted(emitted - declared)}")


def test_the_declared_event_type_no_longer_promises_a_sensor_id():
    """The type is a promise to a compiler. Leaving `sensor_id?: string` in it
    would tell an application the field may arrive — and an application that
    branches on it would render nothing, for ever, without an error."""
    declared = _declared_fields("SpatialEvent")
    assert "sensor_id" not in declared
    # And not smuggled back inside the disagreement rows, which is where it also
    # lived. `_declared_fields` only reads the interface's own top level.
    src = DTS.read_text(encoding="utf-8")
    rows = re.search(r"sensors\?: Array<\{(.*?)\}>", src, re.S)
    assert rows and "sensor_id" not in rows.group(1), rows


# -- 3. a document may not describe a toolset the server does not register -----

MCP_DOC = ROOT / "docs" / "mcp-connect.md"
READ_TOOLS = MCP_TOOL_NAMES - {"call_ha_service"}


def _doc_tool_table() -> dict[str, list[str]]:
    """The Tools table of `mcp-connect.md`, as `{tool: [cells]}`."""
    rows = {}
    for line in MCP_DOC.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*`([a-z_]+)`\s*\|(.*)\|\s*$", line)
        if match:
            rows[match.group(1)] = [c.strip() for c in match.group(2).split("|")]
    return rows


def test_the_documented_tool_table_is_the_registered_toolset():
    """It said "the 4 read tools" while thirteen were registered, and four of
    them appeared in no table at all — so an integrator reading this page could
    not discover them, and a reviewer reading it could not see that the LAN
    census and the occupancy timeline were reachable over the network."""
    documented = set(_doc_tool_table())
    assert documented == set(MCP_TOOL_NAMES), {
        "in the document, not registered": sorted(documented - MCP_TOOL_NAMES),
        "registered, not in the document": sorted(MCP_TOOL_NAMES - documented),
    }


def test_every_registered_tool_name_is_actually_registered_in_mcp_py():
    """`MCP_TOOL_NAMES` is what the scope axis and the document are both checked
    against, so it has to be checked against the server itself — otherwise all
    three agree with each other and none of them with the code."""
    src = (ROOT / "backend" / "wavr" / "mcp.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'@server\.tool\(name="([a-z_]+)"\)', src))
    assert registered == set(MCP_TOOL_NAMES)


def test_the_documented_grant_column_is_the_default_agent_scope():
    """The column exists so nobody has to reconstruct least-privilege from
    prose. A ✓ against a tool an agent cannot call is worse than no column."""
    marked = {name for name, cells in _doc_tool_table().items()
              if cells[3] == "✓"}
    assert marked == set(AGENT_DEFAULT_TOOL_SCOPE)


def test_the_control_tool_is_documented_as_absent_over_http():
    """Not "disabled". `expose_control=False` means it is never registered, and
    a document that says "off" invites somebody to look for the switch."""
    cells = _doc_tool_table()["call_ha_service"]
    assert cells[0] == "control"
    assert "✗" in cells[2] and "never registered" in cells[2]


@pytest.mark.parametrize("claim", [
    "13 read + `call_ha_service`",
    "all 13 read tools",
])
def test_the_transport_summary_counts_the_tools_that_exist(claim):
    doc = MCP_DOC.read_text(encoding="utf-8")
    assert str(len(READ_TOOLS)) == "13", (
        "the read-tool count changed; the sentences below need it too")
    assert claim in doc, claim


# -- the provider example has to be one an integrator can follow ---------------

def test_the_provider_registration_example_binds_a_device():
    """`POST /observations` refuses everything but loopback when a provider has
    no device bound — `presence:write` is held by every paired phone and by
    `guest`, so an unbound provider would be an open occupancy-writing hole. The
    example omitted `device_id`, so following the page exactly produced a
    provider that 403'd its own adapter on the first observation."""
    doc = (ROOT / "docs" / "EXTERNAL-PROVIDERS.md").read_text(encoding="utf-8")
    i = doc.index("PUT /api/providers/external/")
    example = doc[i:doc.index("```", i)]
    assert "device_id" in example, example
