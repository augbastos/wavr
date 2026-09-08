"""Tests for the Python SDK.

    python -m pytest sdk/python/tests -q

Run against a fake transport rather than a live Core: an SDK test that needs a
running Wavr is a test nobody runs.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wavr_sdk import (  # noqa: E402
    PROTOCOL_VERSION, RoomContext, Wavr, WavrAuthError, WavrError, WavrNotFound,
    WavrProtocolError, WavrUnreachable,
)
import urllib.error  # noqa: E402

SPACE = {
    # The version the Core actually ships (contracts.py). See the note in
    # the JavaScript fixture: pinned to 1, this agreed with a stale SDK
    # constant and hid the bump from both suites.
    "protocol_version": 2,
    "space": {"space_id": "sp_1", "name": "My Home"},
    "rooms": [
        {"room": "kitchen", "precision": "count", "confidence": 0.8,
         "occupied": True, "occupancy": 2, "occupancy_known": True,
         "capabilities": ["presence", "count"], "anchors": [], "devices": [],
         "sensors": [], "limitations": ["Wavr can count people in kitchen, "
                                        "not place them within it."]},
        {"room": "attic", "precision": "none", "confidence": 0.0,
         "occupied": None, "occupancy": None, "occupancy_known": False,
         "capabilities": [], "anchors": [], "devices": [], "sensors": [],
         "limitations": ["No sensor covers attic."]},
    ],
    "rooms_without_sensing": ["attic"],
}


def opener(routes, log=None):
    """A transport that answers from a table and records what was asked."""
    def _open(req):
        path = req.full_url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        if log is not None:
            log.append({"path": path, "method": req.get_method(),
                        "body": req.data, "headers": dict(req.headers)})
        hit = routes.get(path)
        if hit is None:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        if isinstance(hit, int):
            raise urllib.error.HTTPError(req.full_url, hit, "err", {}, None)
        if isinstance(hit, Exception):
            raise hit
        return json.dumps(hit).encode()
    return _open


def wavr(routes, log=None, **kw):
    return Wavr("http://core.test:8000", opener=opener(routes, log), **kw)


# -- Connecting and version negotiation ----------------------------------------

def test_connect_learns_the_space_and_the_contract_version():
    w = wavr({"/api/experience/context": SPACE}).connect()
    assert w.space["name"] == "My Home"
    assert w.protocol_version == PROTOCOL_VERSION
    assert w.protocol_ahead is False


def test_a_newer_core_is_surfaced_not_raised():
    """Refusing would break every tool the day a Core updates; ignoring it
    would let a field whose meaning changed pass straight through."""
    w = wavr({"/api/experience/context": {**SPACE, "protocol_version": 99}})
    w.connect()
    assert w.protocol_ahead is True


def test_the_token_rides_on_every_request():
    log = []
    w = wavr({"/api/experience/context": SPACE}, log, token="abc123")
    w.connect()
    assert log[0]["headers"]["Authorization"] == "Bearer abc123"


# -- The null that must never become a zero ------------------------------------

def test_an_uncounted_room_reports_none_and_says_so():
    rooms = {r.room: r for r in wavr({"/api/experience/context": SPACE}).context()}
    assert rooms["attic"].occupancy is None
    assert rooms["attic"].occupancy_known is False


def test_a_real_count_survives():
    rooms = {r.room: r for r in wavr({"/api/experience/context": SPACE}).context()}
    assert rooms["kitchen"].occupancy == 2
    assert rooms["kitchen"].occupancy_known is True


def test_capabilities_are_a_question():
    ctx = RoomContext.from_dict(SPACE["rooms"][0])
    assert ctx.can("count") and not ctx.can("position")


def test_limitations_reach_the_caller_verbatim():
    ctx = RoomContext.from_dict(SPACE["rooms"][1])
    assert ctx.limitations == ("No sensor covers attic.",)


def test_a_device_that_never_reported_a_display_is_not_a_no():
    ctx = RoomContext.from_dict({"room": "k", "devices": [
        {"device_id": "a", "display": True},
        {"device_id": "b", "display": None},
        {"device_id": "c", "display": False}]})
    assert [d["device_id"] for d in ctx.displays()] == ["a"]


# -- Typed failures ------------------------------------------------------------

def test_a_refused_credential_is_its_own_error():
    """A script that retries this forever is a script nobody can debug."""
    with pytest.raises(WavrAuthError):
        wavr({"/api/experience/context": 401}).connect()


def test_a_missing_room_is_not_found():
    with pytest.raises(WavrNotFound):
        wavr({"/api/experience/context": SPACE}).context("nowhere")


def test_an_unreachable_core_names_the_address():
    w = Wavr("http://core.test:8000",
             opener=opener({"/api/experience/context": OSError("refused")}))
    with pytest.raises(WavrUnreachable, match="core.test:8000"):
        w.connect()


def test_a_non_json_answer_is_a_protocol_error():
    def bad(_req):
        return b"<html>not json</html>"
    with pytest.raises(WavrProtocolError):
        Wavr("http://core.test:8000", opener=bad).connect()


def test_every_error_is_a_wavr_error():
    """So a caller that does not care about the distinction can catch one
    thing."""
    for exc in (WavrAuthError, WavrNotFound, WavrUnreachable, WavrProtocolError):
        assert issubclass(exc, WavrError)


# -- The rest of the surface ---------------------------------------------------

def test_rooms_lists_names():
    assert wavr({"/api/experience/context": SPACE}).rooms() == ["kitchen", "attic"]


def test_a_room_name_is_url_encoded():
    log = []
    w = wavr({"/api/experience/context/living%20room": SPACE["rooms"][0]}, log)
    w.context("living room")
    assert log[0]["path"] == "/api/experience/context/living%20room"


def test_anchors_can_be_scoped_to_a_room():
    w = wavr({"/api/anchors?room=kitchen": {"anchors": [{"anchor_id": "a1"}]}})
    assert w.anchors("kitchen")[0]["anchor_id"] == "a1"


def test_resolving_an_external_id_returns_every_match():
    w = wavr({"/api/anchors/resolve/arkit/ABC":
              {"anchors": [{"anchor_id": "a"}, {"anchor_id": "b"}]}})
    assert len(w.resolve_anchor("arkit", "ABC")) == 2


def test_compatibility_posts_the_manifest():
    log = []
    w = wavr({"/api/experience/compatibility": {"status": "FULLY_SUPPORTED"}}, log)
    out = w.compatibility({"id": "x", "name": "X"}, "kitchen")
    assert out["status"] == "FULLY_SUPPORTED"
    assert log[0]["method"] == "POST"
    assert json.loads(log[0]["body"]) == {"manifest": {"id": "x", "name": "X"},
                                          "room": "kitchen"}


# -- Events --------------------------------------------------------------------

def test_events_resume_from_the_last_one_seen():
    """A loop that restarts from "now" is worse than none: the tool looks
    healthy and is quietly wrong about the room."""
    log = []
    routes = {"/api/events/recent?since=&limit=200":
              {"events": [{"event": "room.occupancy_changed", "room": "kitchen",
                           "at": "2026-09-04T12:00:00+00:00"}]},
              "/api/events/recent?since=2026-09-04T12%3A00%3A00%2B00%3A00&limit=200":
              {"events": [{"event": "room.count_changed", "room": "kitchen",
                           "at": "2026-09-04T12:00:05+00:00"}]}}
    w = wavr(routes, log)
    seen = []
    for ev in w.events(sleep=lambda _s: None):
        seen.append(ev["event"])
        if len(seen) == 2:
            break
    assert seen == ["room.occupancy_changed", "room.count_changed"]


def test_events_can_be_filtered_to_one_room():
    routes = {"/api/events/recent?since=&limit=200": {"events": [
        {"event": "a", "room": "hall", "at": "t1"},
        {"event": "b", "room": "kitchen", "at": "t2"}]}}
    w = wavr(routes)
    got = next(iter(w.events(room="kitchen", sleep=lambda _s: None)))
    assert got["room"] == "kitchen"


def test_a_refused_credential_stops_the_loop_rather_than_retrying():
    w = wavr({"/api/events/recent?since=&limit=200": 403})
    with pytest.raises(WavrAuthError):
        next(iter(w.events(sleep=lambda _s: None)))


def test_an_unreachable_core_is_retried():
    """It reboots, it updates. A tool should still be running afterwards."""
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("down")
        return json.dumps({"events": [{"event": "ok", "room": "k", "at": "t"}]}).encode()

    w = Wavr("http://core.test:8000", opener=flaky)
    got = next(iter(w.events(sleep=lambda _s: None)))
    assert got["event"] == "ok" and calls["n"] == 3


def test_reconnect_can_be_switched_off_for_a_one_shot_tool():
    w = wavr({"/api/events/recent?since=&limit=200": OSError("down")})
    with pytest.raises(WavrUnreachable):
        next(iter(w.events(reconnect=False, sleep=lambda _s: None)))


# -- TLS -----------------------------------------------------------------------

def test_verification_is_on_unless_somebody_turns_it_off():
    """The default is the safe one, and the escape hatch is greppable."""
    assert Wavr("https://x")._ctx is None, "None means urllib's verifying default"
    assert Wavr("https://x", verify_tls=False)._ctx is not None
