"""Whether a Space can support an experience — and the line the evaluator must
never cross.

A manifest is a REQUEST. `evaluate()` answers what a room can produce, never who
may read it. The test that matters most in this file is the one asserting the
evaluator has no way to authorise anything, because that is the property that
keeps a compatibility check from quietly becoming a permission system.
"""
import pytest

from wavr.experience import build_context, build_space_context
from wavr.experience_manifest import (
    CAPABILITY_SCOPE, FULLY_SUPPORTED, PARTIALLY_SUPPORTED, SCOPE_COUNT,
    SCOPE_DEVICES, SCOPE_PRESENCE, SPATIAL_SCOPES, UNSUPPORTED, ManifestError,
    evaluate, evaluate_space, parse, validate,
)

RAW = {"id": "kitchen-timer", "name": "Kitchen Timer",
       "requires": ["presence"], "optional": ["count", "display"],
       "scopes": ["room.presence", "room.count", "devices.read"]}


def cov(room="kitchen", modality="camera", health="ok", precision="count",
        sensor_id="cam1"):
    return {"sensor_id": sensor_id, "room": room, "modality": modality,
            "health": health, "precision_level": precision}


def ctx(room="kitchen", **kw):
    return build_context(room=room, **kw)


# -- The line: a manifest is a request -----------------------------------------

def test_the_evaluator_cannot_authorise_anything():
    """It takes no credential and reads no grant table, so it has nothing to
    authorise WITH. That is the property, not a convention."""
    import inspect
    params = set(inspect.signature(evaluate).parameters)
    assert params == {"manifest", "context"}
    src = inspect.getsource(evaluate)
    for forbidden in ("token", "credential", "grant", "device_id", "scopes"):
        assert forbidden not in src, f"evaluate() touches {forbidden}"


def test_the_verdict_says_out_loud_that_it_is_not_permission():
    v = evaluate(parse(RAW), ctx(coverage_rows=[cov()]))
    assert "not permission" in v.to_dict()["note"]


def test_full_support_is_not_a_grant_of_the_scopes_asked_for():
    """A room producing a headcount and an experience being allowed to read it
    are different facts."""
    v = evaluate(parse(RAW), ctx(coverage_rows=[cov()],
                                 devices=[{"device_id": "d", "room": "kitchen",
                                           "display": True}]))
    assert v.status == FULLY_SUPPORTED
    assert "granted" not in str(v.to_dict())


# -- The three verdicts --------------------------------------------------------

def test_everything_present_is_fully_supported():
    v = evaluate(parse(RAW), ctx(coverage_rows=[cov()],
                                 devices=[{"device_id": "d", "room": "kitchen",
                                           "display": True}]))
    assert v.status == FULLY_SUPPORTED and v.usable


def test_a_missing_optional_is_partial_not_broken():
    v = evaluate(parse(RAW), ctx(coverage_rows=[cov()]))
    assert v.status == PARTIALLY_SUPPORTED
    assert v.missing_optional == ("display",)
    assert v.usable is True


def test_a_missing_requirement_is_unsupported():
    v = evaluate(parse(RAW), ctx(room="attic"))
    assert v.status == UNSUPPORTED and v.usable is False
    assert v.missing_required == ("presence",)


def test_the_verdict_is_deterministic():
    """Same manifest, same Space, same answer — which is what makes it something
    a developer can test against."""
    m, c = parse(RAW), ctx(coverage_rows=[cov()])
    assert evaluate(m, c).to_dict() == evaluate(m, c).to_dict()


def test_a_verdict_accepts_the_object_or_the_dict():
    """The SDKs hand back a dict and in-process callers hold the object. Making
    one of them convert guarantees the two paths eventually disagree."""
    m, c = parse(RAW), ctx(coverage_rows=[cov()])
    assert evaluate(m, c).status == evaluate(m, c.to_dict()).status


# -- Fail closed on the unknown ------------------------------------------------

def test_an_unknown_required_capability_is_unsupported_not_ignored():
    """Wavr cannot verify a capability it has never heard of, and calling an
    unverifiable requirement satisfied is how an experience is told it will work
    and then does not."""
    m = parse({**RAW, "requires": ["telepathy"], "optional": []})
    v = evaluate(m, ctx(coverage_rows=[cov()]))
    assert v.status == UNSUPPORTED
    assert any("can provide or verify" in r for r in v.reasons)


def test_an_unknown_optional_capability_can_never_be_satisfied():
    m = parse({**RAW, "requires": [], "optional": ["telepathy"]})
    v = evaluate(m, ctx(coverage_rows=[cov()]))
    assert v.status == PARTIALLY_SUPPORTED
    assert v.missing_optional == ("telepathy",)


def test_a_room_restriction_is_honoured():
    m = parse({**RAW, "rooms": ["kitchen"], "optional": []})
    assert evaluate(m, ctx(room="hall",
                           coverage_rows=[cov(room="hall")])).status == UNSUPPORTED


# -- Reasons and fallbacks are usable ------------------------------------------

def test_a_reason_reuses_the_rooms_own_words():
    """Two explanations of the same gap drift, and the developer finds the
    disagreement before we do."""
    m = parse({"id": "x", "name": "X", "requires": ["count"],
               "scopes": ["room.count"]})
    c = ctx(room="hall", coverage_rows=[cov(room="hall", modality="pir",
                                            precision="room")])
    v = evaluate(m, c)
    assert v.reasons[0] in c.limitations


def test_a_fallback_says_what_to_write_not_to_degrade_gracefully():
    v = evaluate(parse(RAW), ctx(coverage_rows=[cov(precision="room",
                                                    modality="pir")]))
    assert any("'someone is here' rather than a number" in f for f in v.fallbacks)


def test_every_missing_optional_gets_a_fallback():
    v = evaluate(parse(RAW), ctx(room="attic"))
    assert len(v.fallbacks) == len(v.missing_optional)


# -- Parsing an untrusted document ---------------------------------------------

def test_a_manifest_needs_an_id():
    with pytest.raises(ManifestError):
        parse({"name": "No id"})


def test_an_unknown_field_is_refused_rather_than_ignored():
    """A typo in a field name would otherwise be a requirement that is silently
    never checked."""
    with pytest.raises(ManifestError, match="unknown manifest fields"):
        parse({**RAW, "requres": ["presence"]})


def test_an_unknown_scope_is_refused():
    with pytest.raises(ManifestError, match="unknown scopes"):
        parse({**RAW, "scopes": ["everything"]})


def test_a_capability_cannot_be_both_required_and_optional():
    """One of the two lists is what the developer meant, and guessing changes
    whether a Space that lacks it reads as UNSUPPORTED or as PARTIAL."""
    with pytest.raises(ManifestError, match="both required and optional"):
        parse({**RAW, "requires": ["count"], "optional": ["count"]})


def test_a_string_where_a_list_belongs_is_refused():
    with pytest.raises(ManifestError):
        parse({**RAW, "requires": "presence"})


def test_the_lists_are_bounded():
    with pytest.raises(ManifestError):
        parse({**RAW, "requires": [f"cap{i}" for i in range(64)]})


def test_junk_inside_a_list_is_refused():
    with pytest.raises(ManifestError):
        parse({**RAW, "requires": [{"nested": "object"}]})


def test_duplicates_collapse_rather_than_doubling_a_requirement():
    m = parse({**RAW, "requires": ["presence", "presence"], "optional": []})
    assert m.requires == ("presence",)


# -- Validation warns about what will bite later --------------------------------

def test_a_capability_without_its_scope_is_warned_about():
    """Wavr would report the capability as available and then refuse to hand over
    the data — a 403 in the field for a manifest that evaluated as fine."""
    m = parse({"id": "x", "name": "X", "requires": ["count"],
               "scopes": ["room.presence"]})
    assert any("'room.count' scope" in w for w in validate(m))


def test_asking_for_a_scope_nothing_needs_is_warned_about():
    m = parse({"id": "x", "name": "X", "requires": ["presence"],
               "scopes": ["room.presence", "room.position"]})
    assert any("no capability needs it" in w for w in validate(m))


def test_an_unknown_capability_is_warned_about_at_validation_time():
    m = parse({"id": "x", "name": "X", "requires": ["telepathy"], "scopes": []})
    assert any("not a capability Wavr knows about" in w for w in validate(m))


def test_a_correct_manifest_produces_no_warnings():
    assert validate(parse(RAW)) == []


def test_every_capability_maps_to_a_scope():
    """Otherwise a capability could be read with no permission behind it at
    all — the gap a future capability would fall through."""
    from wavr.experience import SPATIAL_CAPABILITIES
    assert set(CAPABILITY_SCOPE) == set(SPATIAL_CAPABILITIES)
    assert set(CAPABILITY_SCOPE.values()) <= SPATIAL_SCOPES


def test_there_is_no_scope_that_grants_identity():
    """Stated as a constant so a future capability cannot be added without
    somebody meeting the line."""
    assert not any("identity" in s or "person" in s for s in SPATIAL_SCOPES)


# -- The whole Space -----------------------------------------------------------

def test_the_space_evaluation_lists_rooms_rather_than_ranking_them():
    """Ranking would need Wavr to weigh a screen against a headcount for
    somebody else's application."""
    space = build_space_context(
        rooms=["kitchen", "attic"],
        states={"kitchen": {"occupied": True, "person_count": 1,
                            "precision_level": "count", "confidence": 0.8}},
        coverage_rows=[cov()])
    out = evaluate_space(parse(RAW), space)
    assert out["partially_supported"] == ["kitchen"]
    assert out["supported_anywhere"] is True
    assert "not ranked" in out["note"]
    assert [v["room"] for v in out["verdicts"]] == ["kitchen", "attic"]


def test_an_experience_no_room_supports_says_so():
    space = build_space_context(rooms=["attic"], states={})
    out = evaluate_space(parse(RAW), space)
    assert out["supported_anywhere"] is False


# -- Over HTTP -----------------------------------------------------------------

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


def test_compatibility_over_http_evaluates_the_whole_space(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/experience/compatibility", json={"manifest": RAW}).json()
        assert body["experience"]["experience_id"] == "kitchen-timer"
        assert body["verdicts"], "one verdict per room"
        assert body["supported_anywhere"] is False, "no sources are configured"


def test_compatibility_can_be_asked_about_one_room(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        body = c.post("/api/experience/compatibility",
                      json={"manifest": RAW, "room": room}).json()
        assert body["room"] == room and body["status"] == UNSUPPORTED


def test_an_anchor_changes_the_verdict(monkeypatch, tmp_path):
    """End to end: creating an anchor makes an anchors-requiring experience go
    from unsupported to supported in that room."""
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        m = {"id": "anchor-demo", "name": "Anchor demo",
             "requires": ["anchors"], "scopes": ["anchors.read"]}
        first = c.post("/api/experience/compatibility",
                       json={"manifest": m, "room": room}).json()
        assert first["status"] == UNSUPPORTED

        c.post("/api/anchors", json={"name": "Counter", "room": room})
        second = c.post("/api/experience/compatibility",
                        json={"manifest": m, "room": room}).json()
        assert second["status"] == FULLY_SUPPORTED


def test_a_malformed_manifest_is_a_400(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/experience/compatibility",
                   json={"manifest": {"name": "no id"}})
        assert r.status_code == 400


def test_warnings_travel_with_the_verdict(monkeypatch, tmp_path):
    """So a developer sees the 403 coming before they ship."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.post("/api/experience/compatibility", json={"manifest": {
            "id": "x", "name": "X", "requires": ["count"],
            "scopes": ["room.presence"]}}).json()
        assert any("room.count" in w for w in body["warnings"])


def test_the_scope_vocabulary_is_published(monkeypatch, tmp_path):
    """So the absence of an identity scope is visible rather than merely true."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.get("/api/experience/scopes").json()
        assert "room.presence" in body["scopes"]
        assert "never learn who" in body["identity"]
        assert not any("identity" in s for s in body["scopes"])
