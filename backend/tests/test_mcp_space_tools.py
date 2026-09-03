"""The Space over MCP: what an agent can ask, and what it must never get back.

The privacy assertions come first. These tools assemble the richest model Wavr
has, so if the projections leak, nothing else about them matters.
"""
import pytest

from wavr.auth import (
    AGENT_DEFAULT_TOOL_SCOPE, AGENT_READ_TOOL_SCOPE, MCP_TOOL_NAMES,
    effective_tool_scopes, tool_call_allowed,
)
from wavr.mcp import (
    explain_room_state, get_core_health, get_device_context, get_sensor_coverage,
    get_space_context,
)


class _Provider:
    """The StateProvider shape the MCP tools consume."""

    def __init__(self, rooms):
        self._rooms = rooms

    def list_rooms(self):
        return list(self._rooms)

    def room_state(self, room):
        return self._rooms.get(room)


def _room(name, **kw):
    base = {
        "room": name, "occupied": True, "confidence": 0.82, "person_count": 1,
        "precision_level": "room", "precision_pct": 50, "precision_next": "count",
        "sources": [{"modality": "camera", "confidence": 0.9},
                    {"modality": "network", "confidence": 0.5}],
        "explanation": "camera sees one person; a known phone is on the network",
        "ts": "2026-09-03T01:00:00+00:00",
        # Everything below MUST be withheld by every tool in this file.
        "vitals": {"breathing_bpm": 14.2, "heart_bpm": 61},
        "targets": [{"id": 1, "x": 2.4, "y": 1.1, "posture": "sitting"}],
        "identities": [{"person": "Augusto", "source": "ble", "rssi": -54}],
    }
    base.update(kw)
    return base


@pytest.fixture
def provider():
    return _Provider({"sala": _room("sala"),
                      "cozinha": _room("cozinha", occupied=False, confidence=0.1,
                                       person_count=None),
                      # No sensor reports on the garage at all: no sources, and
                      # therefore no honest occupancy claim either.
                      "garagem": _room("garagem", sources=[], occupied=False,
                                       confidence=0.0, person_count=None,
                                       precision_level="none", explanation="")})


SPACE = {"space_id": "abc123", "name": "My Home", "kind": "home", "epoch": 3}
TOPOLOGY = {
    "primary_core_id": "core-a", "contested": False, "leaderless": False,
    "primary_stale": False,
    "cores": [{"core_id": "core-a", "name": "Laptop", "status": "primary",
               "stale": False, "epoch": 3, "platform": "windows",
               "portable": True, "room": "Office",
               # Must be stripped: LAN addressing and a TLS pin.
               "base_url": "https://192.168.1.20:8000",
               "cert_fingerprint": "AA:BB:CC:DD"}],
}


# -- What must never come back ------------------------------------------------

def _blob(obj) -> str:
    import json
    return json.dumps(obj, default=str)


def test_no_space_tool_leaks_vitals_targets_or_person_names(provider):
    for payload in (
        get_space_context(lambda: SPACE, provider, lambda: TOPOLOGY, lambda: 4),
        explain_room_state(provider, "sala"),
        get_sensor_coverage(provider, lambda: COVERAGE),
        get_core_health(lambda: TOPOLOGY),
    ):
        blob = _blob(payload)
        assert "breathing_bpm" not in blob and "heart_bpm" not in blob
        assert "posture" not in blob
        assert "Augusto" not in blob, "person labels are PII; MCP is stripped"
        assert "rssi" not in blob


def test_explain_room_state_cannot_widen_get_room_context(provider):
    # It projects through the SAME allowlist, reshaped. If someone adds a field
    # to one, this catches the other silently gaining it.
    from wavr.mcp import _ROOM_CONTEXT_FIELDS, get_room_context

    narrow = set(_blob(get_room_context(provider, "sala")))
    wide = explain_room_state(provider, "sala")
    assert "vitals" not in _blob(wide) and "targets" not in _blob(wide)
    assert set(_ROOM_CONTEXT_FIELDS) >= {"room", "occupied", "confidence"}
    assert narrow  # the baseline tool still answers


def test_core_health_strips_addresses_and_cert_pins():
    blob = _blob(get_core_health(lambda: TOPOLOGY))
    assert "192.168.1.20" not in blob, "LAN addressing is not an agent's business"
    assert "AA:BB:CC:DD" not in blob, "a TLS pin is exactly what a caller would need"
    assert "core-a" in blob and "primary" in blob


def test_device_context_omits_names_people_and_addresses():
    rows = [{
        "device_id": "d1", "functions": ["node", "client"], "room": "Kitchen",
        "portable": False, "platform": "linux",
        "capabilities": {"capabilities": {"camera": True, "ble": None},
                         "compute_tier": "medium"},
        # Must not survive the projection:
        "name": "Ana's phone", "person_id": "p1", "base_url": "https://10.0.0.9",
    }]
    blob = _blob(get_device_context(lambda: rows))
    assert "Ana" not in blob and "person_id" not in blob
    assert "10.0.0.9" not in blob
    assert "d1" in blob and "Kitchen" in blob


# -- What it must actually answer ---------------------------------------------

def test_space_context_is_the_assembly_point(provider):
    body = get_space_context(lambda: SPACE, provider, lambda: TOPOLOGY, lambda: 4)
    assert body["space"]["name"] == "My Home"
    assert body["rooms_total"] == 3 and body["rooms_occupied"] == 1
    assert body["people_registered"] == 4
    assert body["cores"]["primary_core_id"] == "core-a"


def test_space_context_is_honest_when_there_is_no_space(provider):
    body = get_space_context(lambda: None, provider)
    assert body["space"] is None
    assert body["available"] is True, "we could read it; there is simply none"
    assert "not been set up" in body["note"]


def test_a_core_it_cannot_read_is_not_reported_as_unconfigured(provider):
    """The failure this exists to prevent: an agent told a configured house had
    never been set up, because the bridge could not reach the app.

    "There is no Space" is a claim about the operator's home. "I could not find
    out" is a claim about this process. Only the second one is safe to guess."""
    def unreadable():
        raise OSError("connection refused")

    body = get_space_context(unreadable, provider)
    assert body["space"] is None
    assert body["available"] is False
    assert "not been set up" not in body["note"]
    assert "NOT a statement" in body["note"]


def test_a_build_with_no_space_model_says_so_rather_than_guessing(provider):
    body = get_space_context(None, provider)
    assert body["space"] is None and body["available"] is False
    assert "not been set up" not in body["note"]


def test_an_unknown_person_count_stays_null_never_zero(provider):
    body = get_space_context(lambda: SPACE, provider)
    counts = {r["room"]: r["person_count"] for r in body["rooms"]}
    assert counts["sala"] == 1
    assert counts["cozinha"] is None, "unknown must never be reported as zero"


COVERAGE = {
    "rooms": [
        {"room": "sala", "observing": True, "precision_level": "count",
         "sensors": [{"sensor_id": "hall-cam", "kind": "camera",
                      "modality": "camera", "health": "ok", "observing": True,
                      "calibrated": False, "precision_level": "count"}]},
        # Installed, and silent. The case the old live-state derivation could
        # not express at all.
        {"room": "cozinha", "observing": False, "precision_level": "none",
         "sensors": [{"sensor_id": "kitchen-radar", "kind": "node",
                      "modality": "mmwave", "health": "offline",
                      "observing": False, "calibrated": False,
                      "precision_level": "count"}]},
    ],
    "uncovered": ["garagem"],
    "house_wide": [],
    "note": ("A room in `uncovered` has no sensor at all — Wavr cannot see it, "
             "which is not the same as it being empty."),
}


def test_sensor_coverage_separates_unsensed_from_empty(provider):
    body = get_sensor_coverage(provider, lambda: COVERAGE)
    rooms = {r["room"]: r for r in body["rooms"]}
    assert set(rooms) == {"sala", "cozinha"}
    assert body["uncovered"] == ["garagem"]
    assert body["available"] is True
    # The distinction is the whole point, and it is stated to the caller.
    assert "not the same as it being empty" in body["note"]


def test_sensor_coverage_tells_a_silent_sensor_from_no_sensor(provider):
    """Two blind spots, opposite remedies.

    "Buy a sensor" and "go plug yours back in" are different errands, and the
    previous derivation — which read live room state — reported both as
    uncovered. An agent acting on that gives the wrong advice half the time.
    """
    body = get_sensor_coverage(provider, lambda: COVERAGE)
    rooms = {r["room"]: r for r in body["rooms"]}
    assert rooms["cozinha"]["observing"] is False, "installed but silent"
    assert rooms["cozinha"]["sensors"][0]["health"] == "offline"
    assert "cozinha" not in body["uncovered"], "it HAS a sensor"
    assert body["uncovered"] == ["garagem"], "this one genuinely has none"


def test_sensor_coverage_admits_when_it_cannot_enumerate(provider):
    """Silence about sensors must not read as "there are none"."""
    def unreadable():
        raise OSError("db locked")

    for fn in (None, unreadable):
        body = get_sensor_coverage(provider, fn)
        assert body["available"] is False
        assert body["rooms"] == [] and body["uncovered"] == []


def test_explain_room_state_separates_confidence_from_precision(provider):
    body = explain_room_state(provider, "sala")
    assert body["confidence"] == 0.82
    assert body["precision"]["level"] == "room"
    assert body["precision"]["next"] == "count"
    assert body["evidence"], "an explanation with no evidence is an assertion"
    assert "different axis" in body["note"]


def test_explain_room_state_is_none_for_an_unknown_room(provider):
    assert explain_room_state(provider, "nowhere") is None


def test_every_tool_degrades_instead_of_crashing_when_unwired(provider):
    assert get_core_health(None)["available"] is False
    assert get_device_context(None)["available"] is False
    assert get_space_context(None, provider)["space"] is None


# -- Scopes -------------------------------------------------------------------

def test_the_new_tools_are_in_the_scope_vocabulary():
    for name in ("get_space_context", "explain_room_state", "get_sensor_coverage",
                 "get_core_health", "get_device_context"):
        assert name in MCP_TOOL_NAMES, f"{name} is unreachable without a scope entry"


def test_the_coarse_tools_are_in_the_default_agent_grant():
    for name in ("get_space_context", "explain_room_state", "get_sensor_coverage",
                 "get_core_health"):
        assert name in AGENT_DEFAULT_TOOL_SCOPE


def test_a_device_census_needs_an_explicit_grant():
    # Even stripped of names and addresses, "what every device is for and what it
    # can sense" is a household census — same class as get_network_inventory.
    assert "get_device_context" not in AGENT_DEFAULT_TOOL_SCOPE
    assert "get_device_context" in AGENT_READ_TOOL_SCOPE


def test_a_default_agent_is_actually_denied_the_census():
    scopes = effective_tool_scopes("agent", None)
    assert tool_call_allowed(scopes, "get_space_context") is True
    assert tool_call_allowed(scopes, "get_device_context") is False


def test_no_write_tool_crept_into_the_read_bundle():
    assert "call_ha_service" not in AGENT_READ_TOOL_SCOPE
    assert "call_ha_service" not in AGENT_DEFAULT_TOOL_SCOPE


def test_there_is_still_no_tool_for_per_person_position():
    # ADR-0002 keeps targets live-only; ADR-0008 strips them from MCP. An agent
    # polling a targets tool every few seconds would reconstruct exactly the
    # movement history the invariant exists to prevent storing.
    for name in MCP_TOOL_NAMES:
        assert "target" not in name and "position" not in name
        assert "vital" not in name and "biometric" not in name
