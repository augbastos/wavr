"""Which experience, on which device, may read what.

This file exists because of a review finding, and the finding was the worst
kind: `experience_manifest` defined a spatial scope vocabulary and its docstring
said a person decides which an experience gets, "enforced at the API gate".
Nothing enforced it. Every caller holding `presence:read` received the full room
census — headcount, anchors, every device in the room — regardless of what its
manifest asked for.

A stated guarantee nothing implements is worse than no guarantee, so most of
these tests are about the enforcement being real.
"""
import pytest

from wavr.experience import build_context, redact
from wavr.experience_grants import DEFAULT_SCOPES, GrantError, GrantStore
from wavr.experience_manifest import (
    SCOPE_ANCHORS, SCOPE_COUNT, SCOPE_DEVICES, SCOPE_POSITION, SCOPE_PRESENCE,
    SPATIAL_SCOPES,
)


def cov(room="kitchen", precision="position"):
    return {"sensor_id": "cam1", "room": room, "modality": "camera",
            "health": "ok", "precision_level": precision}


def full(room="kitchen"):
    return build_context(
        room=room,
        room_state={"occupied": True, "person_count": 3, "confidence": 0.9,
                    "precision_level": "position"},
        coverage_rows=[cov(room)],
        anchors=[{"anchor_id": "a1", "room": room, "name": "Counter"}],
        devices=[{"device_id": "d1", "room": room, "display": True}],
    ).to_dict()


# -- The enforcement that was missing ------------------------------------------

def test_an_ungranted_caller_gets_presence_and_nothing_else():
    """Not everything, and not an error. Presence is enough for an application
    to be worth running while it waits to be trusted."""
    out = redact(full(), DEFAULT_SCOPES)
    assert out["occupied"] is True
    assert out["occupancy"] is None and out["occupancy_known"] is False
    assert out["anchors"] == [] and out["devices"] == []
    assert set(out["withheld"]) == {"count", "position", "anchors", "devices"}


def test_a_granted_count_comes_through():
    out = redact(full(), {SCOPE_PRESENCE, SCOPE_COUNT})
    assert out["occupancy"] == 3 and out["occupancy_known"] is True
    assert "count" not in out["withheld"]


def test_each_scope_unlocks_exactly_its_own_field():
    for scope, field, empty in ((SCOPE_ANCHORS, "anchors", []),
                                (SCOPE_DEVICES, "devices", [])):
        without = redact(full(), {SCOPE_PRESENCE})
        with_it = redact(full(), {SCOPE_PRESENCE, scope})
        assert without[field] == empty
        assert with_it[field] != empty, scope


def test_a_withheld_capability_is_removed_from_the_capability_list():
    """Otherwise an application is told it CAN count here and then reads a null,
    which reads as a Wavr bug rather than a permission."""
    out = redact(full(), DEFAULT_SCOPES)
    assert "count" not in out["capabilities"]
    assert "position" not in out["capabilities"]
    assert "presence" in out["capabilities"]


def test_withholding_is_said_out_loud():
    """An application that cannot see a headcount needs to know whether the room
    cannot produce one or whether it was not granted it. Those need completely
    different responses, and a silent removal makes them identical."""
    out = redact(full(), DEFAULT_SCOPES)
    assert "not granted them" in out["note"]
    assert out["withheld"]


def test_a_fully_granted_caller_sees_everything_and_nothing_is_marked_withheld():
    out = redact(full(), SPATIAL_SCOPES)
    assert out["occupancy"] == 3 and out["anchors"] and out["devices"]
    assert "withheld" not in out


def test_unscoped_means_loopback_root_and_passes_through_untouched():
    """The Core's own screens, the developer tools and MCP already hold every
    authority in the product. A second weaker gate in front of them would only
    be a second thing to keep correct."""
    body = full()
    assert redact(body, None) == body


def test_a_field_added_later_is_absent_from_a_restricted_view():
    """The guarantee `experience.redact` states, now actually tested.

    This test used to assert the OPPOSITE of its own docstring. It checked that
    an unknown field SURVIVED redaction, on the reasoning that `redact` removes
    what it knows about and the defence is that whoever adds a field also adds
    it to the removal list. That defence had already failed by the time anybody
    read it: `sensors` was added to the context and to nothing in `redact`, so
    the least-trusted paired caller received the room's whole sensing
    inventory — while `withheld` assured it the response had been narrowed.

    The worse half is that the test made the leak durable. Anybody implementing
    the guarantee would have turned this red, and its failure message told them
    to change the comment rather than keep the fix.
    """
    out = redact({**full(), "some_field_added_later": "sensitive"},
                 DEFAULT_SCOPES)
    assert "some_field_added_later" not in out
    # The floor is still there: an application that cannot tell whether anybody
    # is in the room has nothing to run on.
    assert out["room"] and "occupied" in out and "limitations" in out


def test_an_ungranted_caller_cannot_enumerate_the_rooms_sensors():
    """`sensors` answers "what hardware is in this room", which is the question
    `devices.read` gates — and it sat on the line beside `devices` in the same
    `to_dict`, ungated. A caller with the minimum credential could list every
    sensor in the house and see which ones were currently dark."""
    body = {**full(), "sensors": [
        {"label": "camera 1", "modality": "camera", "health": "ok"},
        {"label": "pir 1", "modality": "pir", "health": "offline"}]}
    out = redact(body, DEFAULT_SCOPES)
    assert out["sensors"] == []
    assert "devices" in out["withheld"]


def test_the_quiet_sensor_warning_survives_without_naming_the_sensors():
    """An application must know its answer rests on fewer sensors than it
    should. It must not learn the inventory from the sentence that says so."""
    body = {**full(),
            "sensors": [{"label": "camera 1"}, {"label": "pir 1"}],
            "limitations": ["Not everything here is reporting: pir 1. What Wavr "
                            "says about kitchen rests on fewer sensors than it "
                            "should."]}
    out = redact(body, DEFAULT_SCOPES)
    said = " ".join(out["limitations"])
    assert "fewer sensors" in said, "the warning was lost, not just narrowed"
    assert "pir 1" not in said and "camera 1" not in said


# -- The store -----------------------------------------------------------------

def test_a_device_with_no_grant_gets_the_default():
    assert GrantStore(":memory:").scopes_for("dev_1", "app") == DEFAULT_SCOPES


def test_a_grant_is_read_back():
    s = GrantStore(":memory:")
    s.grant("dev_1", "recipe", [SCOPE_COUNT, SCOPE_ANCHORS])
    got = s.scopes_for("dev_1", "recipe")
    assert SCOPE_COUNT in got and SCOPE_ANCHORS in got


def test_a_grant_is_scoped_to_one_experience_on_one_device():
    s = GrantStore(":memory:")
    s.grant("dev_1", "recipe", [SCOPE_COUNT])
    assert SCOPE_COUNT not in s.scopes_for("dev_1", "other_app")
    assert SCOPE_COUNT not in s.scopes_for("dev_2", "recipe")


def test_presence_survives_even_an_empty_grant():
    """An application that cannot tell whether anybody is in the room has
    nothing to run on. The honest way to express "nothing" is to unpair."""
    s = GrantStore(":memory:")
    s.grant("dev_1", "recipe", [])
    assert s.scopes_for("dev_1", "recipe") == DEFAULT_SCOPES


def test_an_unknown_scope_is_refused_rather_than_dropped():
    """A grant that silently ignored half of what an admin selected would leave
    them believing they had given more — or less — than they had."""
    s = GrantStore(":memory:")
    with pytest.raises(GrantError, match="unknown scopes"):
        s.grant("dev_1", "recipe", [SCOPE_COUNT, "room.everything"])


def test_no_grant_can_include_identity():
    """There is no scope for it, so there is nothing to grant — and
    `experience.py` strips names before this module is consulted. Two
    independent reasons, which is the right number for the one thing that must
    not leak."""
    assert not any("identity" in s or "person" in s or "name" in s
                   for s in SPATIAL_SCOPES)


def test_revoking_removes_it():
    s = GrantStore(":memory:")
    s.grant("dev_1", "recipe", [SCOPE_COUNT])
    assert s.revoke("dev_1", "recipe") is True
    assert s.scopes_for("dev_1", "recipe") == DEFAULT_SCOPES


def test_unpairing_a_device_drops_every_grant_it_held():
    """Otherwise pairing a replacement under the same id inherits the old one's
    spatial scopes — the kind of inheritance nobody remembers granting."""
    s = GrantStore(":memory:")
    s.grant("dev_1", "a", [SCOPE_COUNT])
    s.grant("dev_1", "b", [SCOPE_POSITION])
    assert s.forget_device("dev_1") == 2
    assert s.list("dev_1") == []


def test_a_broken_store_fails_to_the_default_not_to_everything():
    """One that failed by raising would take a context route down; one that
    failed open would hand out the census."""
    s = GrantStore(":memory:")
    s.close()
    assert s.scopes_for("dev_1", "recipe") == DEFAULT_SCOPES


def test_an_anonymous_caller_gets_the_default():
    assert GrantStore(":memory:").scopes_for(None) == DEFAULT_SCOPES


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
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return app, TestClient(app, headers={"X-Wavr-Local": "1"})


def test_a_grant_round_trips_over_http(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        made = c.put("/api/experience/grants/dev_1/recipe",
                     json={"scopes": ["room.count", "anchors.read"]})
        assert made.status_code == 200
        assert set(made.json()["scopes"]) == {"room.count", "anchors.read"}

        listed = c.get("/api/experience/grants").json()
        assert listed["grants"][0]["device_id"] == "dev_1"
        assert listed["default"] == ["room.presence"]

        assert c.delete("/api/experience/grants/dev_1/recipe").status_code == 200
        assert c.get("/api/experience/grants").json()["grants"] == []


def test_an_unknown_scope_is_a_400_over_http(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        r = c.put("/api/experience/grants/dev_1/recipe",
                  json={"scopes": ["room.everything"]})
        assert r.status_code == 400 and "unknown scopes" in r.json()["detail"]


def test_loopback_root_still_sees_the_whole_context(monkeypatch, tmp_path):
    """The dashboard and the developer tools run there and already hold every
    authority in the product."""
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.post("/api/anchors", json={"name": "Counter", "room": room})
        body = c.get(f"/api/experience/context/{room}").json()
        assert body["anchors"], "root is not redacted"
        assert "withheld" not in body


def _pair(app, client, role="user"):
    """Pair a device the way the rest of the suite does.

    From a LAN address, not loopback: the auth middleware resolves `root` for a
    loopback caller regardless of any token, so a loopback "paired device" would
    silently be testing the unscoped path — which is the one path this file is
    NOT about.
    """
    from fastapi.testclient import TestClient
    code = client.post("/api/pair-code", json={"role": role}).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    body = peer.post("/api/pair",
                     json={"code": code, "device_name": "phone"}).json()
    return peer, body["device_id"], {"Authorization": f"Bearer {body['token']}"}


def test_a_paired_device_without_a_grant_is_redacted(monkeypatch, tmp_path):
    """The finding this whole file exists for: a `presence:read` credential used
    to receive the full room census whatever its manifest claimed to need."""
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.post("/api/anchors", json={"name": "Counter", "room": room})
        peer, _device_id, auth = _pair(app, c)

        body = peer.get(f"/api/experience/context/{room}", headers=auth).json()
        assert body["anchors"] == [], "an ungranted device gets no anchors"
        assert body["occupancy"] is None
        assert "anchors" in body["withheld"]


def test_granting_the_device_lets_the_anchors_through(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.post("/api/anchors", json={"name": "Counter", "room": room})
        peer, device_id, auth = _pair(app, c)

        c.put(f"/api/experience/grants/{device_id}/recipe",
              json={"scopes": ["anchors.read"]})
        body = peer.get(f"/api/experience/context/{room}?experience=recipe",
                        headers=auth).json()
        assert [a["name"] for a in body["anchors"]] == ["Counter"]


def test_a_grant_for_one_experience_does_not_cover_another(monkeypatch, tmp_path):
    """The whole point of granting per experience rather than per device."""
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.post("/api/anchors", json={"name": "Counter", "room": room})
        peer, device_id, auth = _pair(app, c)
        c.put(f"/api/experience/grants/{device_id}/recipe",
              json={"scopes": ["anchors.read"]})

        other = peer.get(f"/api/experience/context/{room}?experience=game",
                         headers=auth).json()
        assert other["anchors"] == []


def test_unpairing_a_device_drops_its_grants_over_http(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    with client as c:
        _peer, device_id, _auth = _pair(app, c)
        c.put(f"/api/experience/grants/{device_id}/recipe",
              json={"scopes": ["room.count"]})
        out = c.delete(f"/api/devices/{device_id}").json()
        assert out["grants_dropped"] == 1
        assert c.get("/api/experience/grants").json()["grants"] == []


def test_the_unauthenticated_half_of_a_grant_is_stated_out_loud():
    """Grants are per (device, experience). The device half comes from the
    bearer token; the experience half is a query parameter nobody verifies.

    That is a real limit and it may well be the right trade for now — a
    per-experience credential is a feature, not a patch. What is NOT acceptable
    is leaving it unsaid, because a boundary nobody names is a boundary
    everybody assumes. This test fails if the explanation is deleted while the
    hole is still there, which is the only way prose stays honest.
    """
    from wavr import experience_grants

    doc = experience_grants.__doc__ or ""
    assert "self-asserted" in doc,         "the experience half of a grant is unauthenticated and the module no "         "longer says so"
    assert "most-privileged grant" in doc,         "the consequence for two apps on one device is no longer spelled out"


def test_every_field_the_context_produces_is_accounted_for_by_redact():
    """The allowlist closed the leak direction. This closes the other one.

    `redact` now drops what it does not recognise, which is right — but it means
    a field added to `to_dict` and forgotten here VANISHES for every scoped
    caller while working fine on the loopback screens the author tests on. That
    is the same shape of bug as the leak, pointing the other way, and it would
    be found by an integrator rather than by us.

    So: every key the context emits must be either always-visible or explicitly
    handled by a scope. Adding a field forces a decision about which.
    """
    from wavr.experience import _ALWAYS_VISIBLE, build_context, redact
    from wavr.experience_manifest import SPATIAL_SCOPES

    produced = set(build_context(room="kitchen").to_dict())
    # What redact re-adds under a scope, whatever the caller was granted.
    scoped = set(redact(build_context(room="kitchen").to_dict(),
                        frozenset(SPATIAL_SCOPES)))

    unaccounted = produced - set(_ALWAYS_VISIBLE) - scoped
    assert not unaccounted, (
        f"these context fields are neither always-visible nor restored by any "
        f"scope, so they silently disappear for every scoped caller: "
        f"{sorted(unaccounted)}")

    # And a fully-granted caller loses nothing at all — the strongest form of
    # the same check, and the one an integrator would notice first.
    assert produced <= scoped | {"withheld", "note"}, sorted(produced - scoped)
