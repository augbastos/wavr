"""The inbound contract an external positioning system is adapted onto.

Every enterprise vendor's API is behind a partner agreement, so Wavr defines the
shape it accepts and a twenty-line adapter translates. That makes THIS the part
that has to be right — and the tests below are almost entirely about what an
external system is not allowed to claim.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.provider_ingest import (
    ALLOWED_MODALITIES, SENSOR_PREFIX, ExternalProvider, ExternalProviderStore,
    IngestError, translate,
)
from wavr.storage import Storage

AT = "2026-09-04T12:00:00+00:00"


def store():
    return ExternalProviderStore(":memory:")


def provider(**kw):
    base = dict(provider_id="acme_rtls", label="Acme RTLS", reach="lan")
    base.update(kw)
    return store().register(**base)


def _client(monkeypatch, tmp_path):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


# -- Declaring one -------------------------------------------------------------

@pytest.fixture(autouse=True)
def _rede_fixa(monkeypatch):
    """What this machine believes its own address is, pinned.

    `in_subnet` compares a peer against it, so a test that builds a peer on
    192.168.1.x is asking a question whose answer depends on the network the
    test happens to run on: it pairs on a 192.168.1.x developer box, and is
    refused (or refused for the wrong reason) anywhere else. Same idiom as
    test_app.py and twenty-odd other modules here.
    """
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")


def test_a_provider_must_say_how_far_it_reaches():
    """No default, for the reason `providers.describe` refuses one: an unstated
    reach resolves to the most comfortable value at exactly the moment nobody is
    paying attention."""
    with pytest.raises(IngestError, match="how far it reaches"):
        store().register("acme", "Acme", reach="")


def test_a_declaration_cannot_buy_precision_the_modality_will_not_grant():
    """A declaration may lower a ceiling, never raise one — and a provider
    fusing as `node` would have its counts silently discarded downstream, which
    is worse than being capped here."""
    p = provider(modality="node", ceiling="count")
    assert p.ceiling == "room"
    assert p.may_count is False


def test_a_provider_may_declare_a_lower_ceiling_than_its_modality_allows():
    p = provider(modality="ble", ceiling="house")
    assert p.ceiling == "house"


def test_an_arbitrary_modality_is_refused():
    """Each allowed modality has absence semantics and a counting rule already
    reasoned about in fusion. Letting a provider name any modality would let it
    inherit trust decided for a different technology."""
    with pytest.raises(IngestError):
        provider(modality="camera")
    assert "camera" not in ALLOWED_MODALITIES


def test_an_unknown_reach_is_refused():
    with pytest.raises(IngestError):
        provider(reach="somewhere")


def test_registering_twice_updates_rather_than_duplicating():
    s = store()
    s.register("acme", "Acme", reach="lan")
    s.register("acme", "Acme Renamed", reach="cloud")
    rows = s.list()
    assert len(rows) == 1 and rows[0].label == "Acme Renamed"
    assert rows[0].reach == "cloud"


def test_the_descriptor_is_the_same_contract_every_provider_uses():
    d = provider().descriptor()
    assert d.reach == "lan" and d.leaves_the_home is False
    assert "presence" in d.observes


def test_a_cloud_provider_is_flagged_as_leaving_the_home():
    """The single question a privacy screen has to answer per provider."""
    assert provider(reach="cloud").descriptor().leaves_the_home is True


# -- What an observation may claim ---------------------------------------------

def test_a_room_the_space_does_not_have_is_refused():
    """A typo would otherwise conjure a room that appears on the dashboard,
    holds occupancy, and exists nowhere in the house."""
    events, problems = translate(
        provider(), [{"room": "ktichen", "present": True}],
        rooms=["kitchen"], at_fn=lambda _: AT)
    assert events == []
    assert "no room named" in problems[0]["why"]
    assert "kitchen" in problems[0]["why"], "the known rooms are listed"


def test_a_count_from_a_provider_that_cannot_count_is_dropped_and_reported():
    """Dropped rather than rejecting the batch — the presence is still real.
    Reported rather than dropped silently — an integrator has to find out from
    Wavr, not from six months of missing counts."""
    events, problems = translate(
        provider(modality="node"), [{"room": "kitchen", "present": True,
                                     "count": 3}],
        rooms=["kitchen"], at_fn=lambda _: AT)
    assert len(events) == 1 and events[0].count is None
    assert events[0].presence is True, "the presence survived"
    assert problems[0]["dropped"] == "count"
    assert "cannot assert a headcount" in problems[0]["why"]


def test_a_batch_is_bounded():
    with pytest.raises(IngestError, match="at most"):
        translate(provider(), [{"room": "kitchen"}] * 500,
                  rooms=["kitchen"], at_fn=lambda _: AT)


def test_junk_in_the_batch_is_dropped_without_taking_the_rest():
    events, problems = translate(
        provider(), ["nonsense", {"room": "kitchen", "present": True}],
        rooms=["kitchen"], at_fn=lambda _: AT)
    assert len(events) == 1 and len(problems) == 1


def test_absence_carries_no_mass():
    """Matching every other source: an "I do not see anybody" is much weaker
    evidence than a positive reading."""
    events, _ = translate(provider(), [{"room": "kitchen", "present": False,
                                        "confidence": 0.9}],
                          rooms=["kitchen"], at_fn=lambda _: AT)
    assert events[0].confidence == 0.0


def test_a_count_is_never_attached_to_an_absence():
    events, _ = translate(provider(modality="ble"),
                          [{"room": "kitchen", "present": False, "count": 2}],
                          rooms=["kitchen"], at_fn=lambda _: AT)
    assert events[0].count is None


def test_confidence_is_clamped_rather_than_trusted():
    events, _ = translate(provider(), [{"room": "kitchen", "present": True,
                                        "confidence": 9.5}],
                          rooms=["kitchen"], at_fn=lambda _: AT)
    assert events[0].confidence == 1.0


def test_a_non_numeric_confidence_is_reported_and_defaulted():
    events, problems = translate(
        provider(), [{"room": "kitchen", "present": True, "confidence": "high"}],
        rooms=["kitchen"], at_fn=lambda _: AT)
    assert events[0].confidence == 0.7
    assert problems[0]["dropped"] == "confidence"


def test_the_sensor_id_is_namespaced_by_provider():
    """So a reliability profile for an external sensor can never be confused
    with a camera of the same name."""
    events, _ = translate(provider(), [{"room": "kitchen", "present": True,
                                        "sensor_id": "camera"}],
                          rooms=["kitchen"], at_fn=lambda _: AT)
    assert events[0].sensor_id == f"{SENSOR_PREFIX}acme_rtls:camera"


def test_the_timestamp_goes_through_the_callers_clock_normalisation():
    """An enterprise positioning system keeps its own time, and feeding that
    straight into fusion's freshness arithmetic silently discards a slow one's
    evidence."""
    seen = []
    translate(provider(), [{"room": "kitchen", "present": True, "at": "whenever"}],
              rooms=["kitchen"], at_fn=lambda s: seen.append(s) or AT)
    assert seen == ["whenever"]


# -- Over HTTP -----------------------------------------------------------------

def test_an_unregistered_provider_cannot_post(monkeypatch, tmp_path):
    """Refused, not accepted-with-defaults: a default reach is a privacy failure
    and a default ceiling would let anything claim `position`."""
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/providers/whoever/observations",
                   json={"observations": [{"room": "sala", "present": True}]})
        assert r.status_code == 404
        assert "must declare its reach" in r.json()["detail"]


def test_the_whole_workflow_over_http(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]

        made = c.put("/api/providers/external/acme_rtls",
                     json={"label": "Acme RTLS", "reach": "lan",
                           "modality": "ble", "ceiling": "room"})
        assert made.status_code == 200 and made.json()["reach"] == "lan"

        posted = c.post("/api/providers/acme_rtls/observations", json={
            "observations": [{"room": room, "present": True, "confidence": 0.8}]})
        assert posted.status_code == 200
        assert posted.json()["accepted"] == 1

        state = c.get(f"/api/experience/context/{room}").json()
        assert state["occupied"] is True


def test_a_capped_ceiling_is_explained_at_registration(monkeypatch, tmp_path):
    """An integrator who asked for `count` and got `room` needs to know why
    their counts will be dropped, when they register rather than a week later."""
    with _client(monkeypatch, tmp_path) as c:
        body = c.put("/api/providers/external/acme_rtls",
                     json={"label": "Acme", "reach": "lan",
                           "modality": "node", "ceiling": "count"}).json()
        assert body["ceiling"] == "room"
        assert "Capped to 'room'" in body["note"]


def test_a_switched_off_provider_is_refused(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        c.put("/api/providers/external/acme_rtls",
              json={"label": "Acme", "reach": "lan"})
        c.post("/api/providers/external/acme_rtls/enabled", json={"enabled": False})
        r = c.post("/api/providers/acme_rtls/observations",
                   json={"observations": []})
        assert r.status_code == 409


def test_problems_do_not_fail_the_batch(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.put("/api/providers/external/acme_rtls",
              json={"label": "Acme", "reach": "lan"})
        body = c.post("/api/providers/acme_rtls/observations", json={
            "observations": [{"room": "nowhere", "present": True},
                             {"room": room, "present": True}]}).json()
        assert body["accepted"] == 1 and len(body["problems"]) == 1


def test_registering_needs_admin_and_posting_does_not(monkeypatch, tmp_path):
    """The separation IS the design. An adapter holding a credential that could
    also re-register its own provider could declare itself position-capable and
    start asserting coordinates."""
    import inspect
    from wavr.api_provider_ingest import build_router
    src = inspect.getsource(build_router)
    register_block = src[src.index("async def register("):src.index("async def set_enabled(")]
    post_block = src[src.index("async def observations("):]
    assert 'require_scope("admin")' in register_block
    assert 'require_local' in register_block
    assert 'require_scope("presence:write")' in post_block
    assert 'require_scope("admin")' not in post_block


# -- Who may post as a provider ------------------------------------------------

def _pair(app, client, role="user"):
    from fastapi.testclient import TestClient
    code = client.post("/api/pair-code", json={"role": role}).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    body = peer.post("/api/pair",
                     json={"code": code, "device_name": "adapter"}).json()
    return peer, body["device_id"], {"Authorization": f"Bearer {body['token']}"}


def _app_and_client(monkeypatch, tmp_path):
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


def test_an_unbound_provider_accepts_this_core_only(monkeypatch, tmp_path):
    """`presence:write` alone was not enough. Every `user` device holds it, and
    so does `guest`, whose entire documented purpose is registering its OWN
    presence — not fabricating occupancy for every room in the house."""
    app, client = _app_and_client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.put("/api/providers/external/acme", json={"label": "Acme",
                                                    "reach": "lan"})
        peer, _did, auth = _pair(app, c)
        r = peer.post("/api/providers/acme/observations", headers=auth,
                      json={"observations": [{"room": room, "present": True}]})
        assert r.status_code == 403
        assert "no device bound" in r.json()["detail"]


def test_the_bound_device_may_post_and_others_may_not(monkeypatch, tmp_path):
    app, client = _app_and_client(monkeypatch, tmp_path)
    with client as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        peer, device_id, auth = _pair(app, c)
        c.put("/api/providers/external/acme",
              json={"label": "Acme", "reach": "lan", "device_id": device_id})

        ok = peer.post("/api/providers/acme/observations", headers=auth,
                       json={"observations": [{"room": room, "present": True}]})
        assert ok.status_code == 200 and ok.json()["accepted"] == 1

        other, _oid, other_auth = _pair(app, c)
        nope = other.post("/api/providers/acme/observations", headers=other_auth,
                          json={"observations": [{"room": room, "present": True}]})
        assert nope.status_code == 403
        assert "one device only" in nope.json()["detail"]


def test_the_registration_publishes_which_device_may_speak(monkeypatch, tmp_path):
    """So an operator can see which adapter is allowed to act for a provider."""
    app, client = _app_and_client(monkeypatch, tmp_path)
    with client as c:
        body = c.put("/api/providers/external/acme",
                     json={"label": "Acme", "reach": "lan"}).json()
        assert body["loopback_only"] is True and body["device_id"] == ""


def test_the_note_to_integrators_does_not_claim_reach_is_enforced():
    """`reach` says where the VENDOR'S copy of the data goes. Wavr publishes it
    so a household knows what it agreed to, and refuses a provider that declines
    to state one — but nothing about an inbound POST can be checked against it,
    and `translate()` never reads it.

    This response body is read by integrators. It claimed both halves were
    enforced, which is the kind of sentence that ends up in somebody else's
    compliance document.
    """
    import inspect

    from wavr import api_provider_ingest, provider_ingest

    assert "reach" not in inspect.getsource(provider_ingest.translate),         "if reach IS enforced now, this test and the note should both change"

    note = ""
    for line in inspect.getsource(api_provider_ingest).splitlines():
        if '"note"' in line or (note and line.strip().startswith('"')):
            note += line
            if line.rstrip().endswith("),"):
                break
    assert "ENFORCED" in note and "cannot verify" in note, note
    assert "Both are enforced" not in note
