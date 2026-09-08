"""Exporting a configuration, and a diagnostic bundle, without exporting a
secret.

Almost every test here is the same test: something that must not be in the file
is not in the file. That repetition is the point — these two documents are the
ones a person emails to a stranger, and the allowlists are only as good as the
last person who added a field.
"""
import json
import sys

import pytest

from wavr.config_export import (
    CONFIG_VERSION, SECRET_SETTINGS, TransferError, audit, check_import,
    diagnostic_bundle, export_config, preview_import,
)

HOUSE = {"version": 2, "floors": [{"level": 0, "rooms": [
    {"name": "kitchen", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]},
    {"name": "hall", "polygon": [[4, 0], [6, 0], [6, 3], [4, 3]]}]}]}

CAMERA = {"name": "hall-cam", "room": "hall", "level": 0, "confidence": 0.5,
          "rtsp_url": "rtsp://admin:hunter2@192.168.1.9/stream",
          "mac": "aa:bb:cc:dd:ee:ff"}

NODE = {"node_id": "n1", "label": "Hall PIR", "room": "hall",
        "sensor_type": "pir", "modality": "pir", "state": "active",
        "transport": "native", "token": "super-secret-node-token"}


def full_export(**kw):
    base = dict(space={"name": "My Home", "kind": "home", "space_id": "sp_abc"},
                house=HOUSE, cameras=[CAMERA], nodes=[NODE],
                anchors=[{"name": "Counter", "room": "kitchen",
                          "kind": "logical", "anchor_id": "anc_1"}],
                ha_mappings=[{"entity_id": "binary_sensor.hall", "room": "hall",
                              "modality": "pir", "enabled": True, "label": ""}],
                providers=[{"provider_id": "acme", "label": "Acme", "kind":
                            "spatial", "reach": "lan", "modality": "node",
                            "ceiling": "room", "confidence": "none",
                            "enabled": True, "notes": ""}],
                settings=[{"key": "instance_name", "value": "Wavr"},
                          {"key": "ha_token", "value": "eyJhbGciOi-a-real-token"}])
    base.update(kw)
    return export_config(**base)


# -- The secrets that must not travel ------------------------------------------

def test_a_camera_url_is_never_exported():
    """An RTSP URL carries a username and a password, and a camera reachable
    from outside is a camera anybody can watch."""
    body = full_export()
    assert "hunter2" not in str(body)
    assert "rtsp://" not in str(body)
    assert body["cameras"][0]["name"] == "hall-cam", "the camera still travels"
    assert body["cameras"][0]["room"] == "hall"


def test_a_node_token_is_never_exported():
    body = full_export()
    assert "super-secret-node-token" not in str(body)
    assert body["nodes"][0]["label"] == "Hall PIR"


def test_a_settings_secret_is_named_but_not_valued():
    """So an operator importing sees what they still have to supply, rather than
    finding out when a camera stays dark."""
    body = full_export()
    assert "eyJhbGciOi-a-real-token" not in str(body)
    assert "ha_token" in body["secrets_you_must_supply_again"]
    assert "ha_token" not in body["settings"]


def test_ordinary_settings_do_travel():
    assert full_export()["settings"]["instance_name"] == "Wavr"


def test_the_export_is_an_allowlist_not_a_blocklist():
    """A blocklist leaks by default the day somebody adds a column. This checks
    the property directly: an unknown field simply does not appear."""
    body = export_config(cameras=[{**CAMERA, "some_new_field_added_later":
                                   "a-secret-nobody-thought-about"}])
    assert "a-secret-nobody-thought-about" not in str(body)


def test_the_space_id_is_not_exported():
    """Importing into a second Core should create a NEW Space, not a second
    Core claiming to be the first — which is the split-brain `core_registry`
    exists to detect."""
    body = full_export()
    assert "sp_abc" not in str(body)
    assert body["space"]["name"] == "My Home"


def test_nobody_is_in_a_configuration():
    """A configuration describes a BUILDING. Who lives in it is a different
    thing with a different consent conversation."""
    body = full_export()
    for forbidden in ("people", "person", "persons", "identities", "occupants"):
        assert forbidden not in body


# -- The audit that catches what the allowlist missed ---------------------------

def test_the_audit_finds_a_leaked_url():
    """Belt and braces over the finished document, because an allowlist is only
    as good as the last person who added a field."""
    leaked = {**full_export()}
    leaked["cameras"][0]["rtsp_url"] = "rtsp://admin:hunter2@cam/stream"
    assert audit(leaked)


def test_the_audit_finds_a_key_that_names_a_secret():
    assert audit({"settings": {"mqtt_password": "x"}}) == ["$.settings.mqtt_password"]


def test_the_audit_does_not_fire_on_the_fields_whose_job_is_naming_secrets():
    """`secrets_you_must_supply_again` lists names and carries no values. A
    check that fired on it would train somebody to ignore the check."""
    assert audit(full_export()) == []


def test_the_audit_ignores_prose_that_mentions_secrets():
    """The note explains that credentials are absent, so the words appear there
    legitimately."""
    assert audit({"note": "No password or token is in this file."}) == []


def test_a_clean_bundle_passes_its_own_audit():
    bundle = diagnostic_bundle(config=full_export(), version="1.0",
                               platform="linux")
    assert audit(bundle) == []


# -- The diagnostic bundle ------------------------------------------------------

def test_a_bundle_carries_health_not_history():
    """"The kitchen was occupied at 23:40" is a fact about somebody's evening,
    and a support bundle ends up in a ticket system."""
    bundle = diagnostic_bundle(
        config=full_export(),
        coverage=[{"sensor_id": "hall-cam", "health": "offline"}],
        source_health=[{"name": "hall-cam", "state": "failed"}],
        recent_events=[{"event": "room.occupancy_changed", "room": "hall",
                        "occupied": True, "at": "2026-09-04T23:40:00+00:00"}])
    assert bundle["coverage"][0]["health"] == "offline"
    assert "room_states" not in bundle and "history" not in bundle
    assert "no history of who was where" in bundle["note"]


def test_a_bundle_is_bounded():
    bundle = diagnostic_bundle(recent_events=[{"event": "x"}] * 5000)
    assert len(bundle["recent_events"]) <= 500


def test_a_bundle_includes_the_clock_estimates():
    """"Your Home Assistant is an hour behind" is exactly the kind of thing that
    otherwise costs somebody an evening."""
    bundle = diagnostic_bundle(clocks={"unusable": ["home_assistant"]})
    assert bundle["clocks"]["unusable"] == ["home_assistant"]


def test_the_bundle_carries_a_safe_space_fingerprint_not_the_join_secret():
    """`space_id` is the credential `api_space.join_space` accepts to join
    this Space — it belongs on the same list as a token, not a room name. A
    supporter comparing two bundles from one household still needs to tell
    they came from the same install; a one-way hash gives them that without
    handing back the value the hash was made from."""
    bundle = diagnostic_bundle(config=full_export(),
                               space={"space_id": "the-real-join-secret-abc123"})
    assert bundle["space_fingerprint"]
    assert "the-real-join-secret-abc123" not in json.dumps(bundle)
    assert bundle["space_fingerprint"].startswith("sp_")

    # Stable across calls, so two bundles pulled a week apart from the same
    # Core can be compared without a person retyping anything.
    again = diagnostic_bundle(config=full_export(),
                              space={"space_id": "the-real-join-secret-abc123"})
    assert bundle["space_fingerprint"] == again["space_fingerprint"]

    # And it changes with the Space, so it is not just a constant that always
    # reads the same regardless of what was fed in.
    other = diagnostic_bundle(config=full_export(),
                              space={"space_id": "a-totally-different-space"})
    assert other["space_fingerprint"] != bundle["space_fingerprint"]


def test_a_bundle_with_no_space_has_no_fingerprint_rather_than_a_fake_one():
    assert diagnostic_bundle(config=full_export())["space_fingerprint"] is None


def test_the_bundle_reports_node_health_as_counts_not_names():
    """The same "shape, not content" rule `config_shape` follows, applied to
    nodes: a node's own name can carry a person's name, exactly like the
    anchor-name leak this module's docstring already describes finding once."""
    bundle = diagnostic_bundle(config=full_export())
    assert bundle["nodes"]["count"] == 1
    assert bundle["nodes"]["by_state"] == {"active": 1}
    assert bundle["nodes"]["by_modality"] == {"pir": 1}
    assert "Hall PIR" not in json.dumps(bundle["nodes"])


# -- Importing ------------------------------------------------------------------

def test_a_file_from_a_newer_wavr_is_refused_not_partially_read():
    """A field whose meaning changed would be applied with the old meaning,
    silently, and the operator would have no way to tell."""
    with pytest.raises(TransferError, match="newer Wavr"):
        check_import({"wavr_config_version": CONFIG_VERSION + 1})


def test_a_file_with_no_version_is_refused():
    with pytest.raises(TransferError, match="does not say which"):
        check_import({"house": HOUSE})


def test_junk_is_refused_with_a_readable_reason():
    for bad in ("not json at all", b"{", 42, [1, 2, 3]):
        with pytest.raises(TransferError):
            check_import(bad)


def test_a_file_that_travelled_through_an_inbox_is_shape_checked():
    with pytest.raises(TransferError, match="must be a list"):
        check_import({"wavr_config_version": 1, "cameras": {"not": "a list"}})


def test_an_oversized_list_is_refused():
    with pytest.raises(TransferError, match="more than"):
        check_import({"wavr_config_version": 1, "anchors": [{}] * 5000})


def test_a_json_string_is_accepted():
    import json
    assert check_import(json.dumps(full_export()))["wavr_config_version"] == 1


# -- The preview, which is the point of not applying immediately ---------------

def test_the_preview_names_the_rooms_that_would_be_lost():
    """An import is destructive in a way an operator cannot easily reverse: a
    floor plan is hours of work, and "it replaced my rooms" is the complaint
    this avoids."""
    out = preview_import(full_export(),
                         existing_rooms=["kitchen", "office", "garage"])
    assert out["rooms_replaced"] == ["kitchen"]
    assert out["rooms_lost"] == ["garage", "office"]
    assert "Nothing has been written" in out["note"]


def test_the_preview_counts_what_is_coming():
    out = preview_import(full_export())
    assert out["anchors_incoming"] == 1 and out["cameras_incoming"] == 1
    assert out["space"]["name"] == "My Home"


def test_the_preview_repeats_what_still_has_to_be_supplied():
    assert "ha_token" in preview_import(full_export())["secrets_needed"]


def test_every_secret_setting_is_actually_a_secret():
    """A guard on the list itself: something added here that is NOT a secret
    would be silently dropped from every export."""
    for key in SECRET_SETTINGS:
        assert any(w in key for w in ("token", "password", "endpoint")), key


# -- Over HTTP -----------------------------------------------------------------

def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.housemap import SAMPLE_MAP
    from wavr.camera_store import CameraStore
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    # WAVR_HOUSE_MAP defaults to a CWD-RELATIVE "house.json". Any test that
    # writes a floor plan without pinning this drops one in `backend/`, where
    # it becomes the default map for every other test in the run — which is
    # exactly what happened: three developer-mode tests started failing with
    # "kitchen is not a room in the default map", nowhere near the cause.
    # Written out, not just pinned: the product's fallback map is empty now
    # (a fresh install has no rooms), and this test exports a camera bound to
    # one.
    import json
    _house = tmp_path / "house.json"
    _house.write_text(json.dumps(SAMPLE_MAP), encoding="utf-8")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(_house))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_a_real_export_carries_no_camera_url(monkeypatch, tmp_path):
    """End to end, against a Core with a camera actually configured."""
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        added = c.post("/api/cameras", json={
            "name": "hall-cam", "room": room,
            "rtsp_url": "rtsp://admin:hunter2@192.168.1.9/stream"})
        assert added.status_code == 200, added.text

        body = c.get("/api/config/export")
        assert body.status_code == 200
        text = body.text
        assert "hunter2" not in text and "rtsp://" not in text
        assert "hall-cam" in text, "the camera itself still travels"


def test_a_real_bundle_carries_no_camera_url(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]
        c.post("/api/cameras", json={"name": "hall-cam", "room": room,
                                     "rtsp_url": "rtsp://a:b@cam/s"})
        text = c.get("/api/diagnostics/bundle").text
        assert "rtsp://" not in text and "b@cam" not in text


def _client_with_nodes(monkeypatch, tmp_path):
    """`_client`, plus everything node enrollment needs — its own fixture
    rather than a flag on `_client`, because turning on multidevice/nodes
    pulls in three extra on-disk stores that every other test in this file has
    no reason to carry."""
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.core_registry import CoreRegistry
    from wavr.discovery_inbox import DiscoveryInbox
    from wavr.fusion import FusionEngine
    from wavr.housemap import SAMPLE_MAP
    from wavr.hub import Hub
    from wavr.settings_store import SettingsStore
    from wavr.space_store import SpaceStore
    from wavr.storage import Storage

    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")
    db = str(tmp_path / "w.db")
    monkeypatch.setenv("WAVR_DB", db)
    _house = tmp_path / "house.json"
    _house.write_text(json.dumps(SAMPLE_MAP), encoding="utf-8")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(_house))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True,
                     space_store=SpaceStore(db), core_registry=CoreRegistry(db),
                     settings_store=SettingsStore(db),
                     discovery_inbox=DiscoveryInbox(db))
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_a_real_token_and_a_real_camera_password_never_leave_in_either_file(
        monkeypatch, tmp_path):
    """The test this whole module exists to make possible: seed a REAL node
    bearer token (minted by the actual enrollment flow, not typed into a
    fixture) and a REAL camera password into a running Core, then prove
    neither string reaches either exported file. A secret that leaks into a
    bundle somebody emails to a stranger is the worst defect this surface can
    have.
    """
    with _client_with_nodes(monkeypatch, tmp_path) as c:
        room = c.get("/api/house").json()["floors"][0]["rooms"][0]["name"]

        camera_password = "N0tAPlaceholder-7f3c9d"
        added = c.post("/api/cameras", json={
            "name": "hall-cam", "room": room,
            "rtsp_url": f"rtsp://admin:{camera_password}@192.168.1.9/stream"})
        assert added.status_code == 200, added.text

        req = c.post("/api/nodes/request",
                     json={"name_hint": "esp32", "sensor_hint": "ld2450"})
        assert req.status_code == 200, req.text
        node_id = req.json()["node_id"]
        request_id = req.json()["request_id"]

        approved = c.post(f"/api/nodes/{node_id}/approve", json={
            "name": "Hall radar", "sensor_type": "ld2450", "room": room})
        assert approved.status_code == 200, approved.text

        claimed = c.post("/api/nodes/claim", json={"request_id": request_id})
        assert claimed.status_code == 200, claimed.text
        node_token = claimed.json()["token"]
        assert node_token, "the token must actually have been minted"

        for path in ("/api/config/export", "/api/diagnostics/bundle"):
            resp = c.get(path)
            # A real 200 with the secret gone, not a 500 that merely refused
            # to answer — the audit gate refusing IS a defence, but a test
            # that also passes on a refusal is not proving the allowlist
            # actually redacts anything.
            assert resp.status_code == 200, (
                f"{path} did not build cleanly: {resp.text[:200]}")
            text = resp.text
            assert camera_password not in text, f"camera password leaked via {path}"
            assert node_token not in text, f"node token leaked via {path}"
            assert "rtsp://" not in text, f"a raw camera URL leaked via {path}"


def test_the_bundle_carries_network_reachability_from_an_injected_check(monkeypatch):
    """Off the event loop, on demand, never able to crash the bundle — see
    wavr.lan_reachability's own docstring. Injected here so this test never
    shells out to the real Windows firewall, and `serves_the_lan`/`bound_host`/
    `bound_port` are monkeypatched rather than read from real process state,
    which is a module-level global another test in this same run may have
    already changed."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from wavr import lan_reachability
    from wavr.api_config_export import build_router

    monkeypatch.setattr(lan_reachability, "serves_the_lan", lambda: True)
    monkeypatch.setattr(lan_reachability, "bound_host", lambda: "0.0.0.0")
    monkeypatch.setattr(lan_reachability, "bound_port", lambda *a, **kw: 8000)

    calls = []

    def fake_check(program, port):
        calls.append((program, port))
        return SimpleNamespace(to_dict=lambda: {
            "state": "blocked",
            "reason": "an enabled inbound BLOCK rule matches this Core",
            "rules": ["WavrCoreBlock"], "checked": True})

    router = build_router(
        gather_config=lambda: dict(space=None, house={}, cameras=[], nodes=[],
                                   anchors=[], ha_mappings=[], providers=[],
                                   settings=[], topology={}),
        gather_bundle=lambda: {},
        existing_rooms_fn=lambda: [], existing_anchors_fn=lambda: [],
        require_local=lambda: None, require_scope=lambda scope: (lambda: None),
        reachability_fn=fake_check)

    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/api/diagnostics/bundle").json()

    assert body["network_reachability"] == {
        "state": "blocked",
        "reason": "an enabled inbound BLOCK rule matches this Core",
        "rules": ["WavrCoreBlock"], "checked": True,
        "serves_the_lan": True, "bound_host": "0.0.0.0"}
    assert calls == [(sys.executable, 8000)], (
        "the Core's own listener, not a default, must reach the check")


def test_a_failing_reachability_check_degrades_rather_than_500s():
    """`lan_reachability.check` promises never to raise. This proves the
    ROUTE keeps that promise even if a future check somehow does — an
    unwired or broken firewall probe must not take the whole diagnostic
    download down with it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from wavr.api_config_export import build_router

    def broken_check(program, port):
        raise RuntimeError("netsh exploded")

    router = build_router(
        gather_config=lambda: dict(space=None, house={}, cameras=[], nodes=[],
                                   anchors=[], ha_mappings=[], providers=[],
                                   settings=[], topology={}),
        gather_bundle=lambda: {},
        existing_rooms_fn=lambda: [], existing_anchors_fn=lambda: [],
        require_local=lambda: None, require_scope=lambda scope: (lambda: None),
        reachability_fn=broken_check)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/api/diagnostics/bundle")
    assert resp.status_code == 200
    assert resp.json()["network_reachability"]["state"] == "unknown"


def test_the_audit_is_a_gate_not_a_report(monkeypatch, tmp_path):
    """A refused download is an obvious bug that gets fixed on the spot. A
    leaked one is discovered by whoever received it."""
    import inspect

    from wavr.api_config_export import build_router
    src = inspect.getsource(build_router)
    assert "raise HTTPException" in src.split("def _checked")[1].split("return")[0]
    assert "status_code=500" in src


def test_previewing_a_broken_file_is_a_400(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        r = c.post("/api/config/preview", json={"config": {"no": "version"}})
        assert r.status_code == 400


def test_validating_says_readable_or_why_not(monkeypatch, tmp_path):
    """A file that will not parse and a file that would replace three rooms are
    different problems, and one answer for both would bury the second."""
    with _client(monkeypatch, tmp_path) as c:
        good = c.get("/api/config/export").json()
        assert c.post("/api/config/validate",
                      json={"config": good}).json()["readable"] is True
        bad = c.post("/api/config/validate",
                     json={"config": {"wavr_config_version": 999}}).json()
        assert bad["readable"] is False and "newer Wavr" in bad["error"]


def test_a_round_trip_previews_without_writing(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        exported = c.get("/api/config/export").json()
        before = c.get("/api/house").json()
        out = c.post("/api/config/preview", json={"config": exported}).json()
        assert out["rooms_lost"] == [], "its own export loses nothing"
        assert c.get("/api/house").json() == before, "nothing was written"


# -- The guarantee the settings export LEANS ON --------------------------------

def test_the_settings_store_refuses_to_hold_a_secret_at_all():
    """`export_config` sends every stored setting, filtered only by name against
    `SECRET_SETTINGS`. That is safe for exactly one reason: the settings store
    is itself an allowlist and raises on any key outside `SETTING_SPECS`.

    An audit read the export as a blocklist hiding inside an allowlist. The
    export is fine; the protection is one layer down and was written nowhere.
    So it is written here — and if the store ever starts accepting arbitrary
    keys, this test fails before anybody's configuration file does.
    """
    import tempfile
    from pathlib import Path

    from wavr.settings_store import SettingsError, SettingsStore

    store = SettingsStore(str(Path(tempfile.mkdtemp()) / "s.db"))
    for secret_shaped in ("ha_token", "mqtt_password", "some_new_api_key",
                          "aws_secret_access_key"):
        try:
            store.set(secret_shaped, "hunter2")
        except SettingsError:
            continue
        raise AssertionError(
            f"the settings store accepted {secret_shaped!r}. The config export "
            f"relies on it refusing — either strip the value in export_config "
            f"or keep the store closed.")

    # And the thing it DOES accept is harmless and travels, because losing it
    # is the reason somebody exports a configuration in the first place.
    store.set("instance_name", "Upstairs")
    assert any(row.get("key") == "instance_name" for row in store.describe())


def test_the_secret_name_list_has_not_silently_started_matching_real_settings():
    """`SECRET_SETTINGS` matches no real setting today, which is the correct
    state for a belt-and-braces list and not evidence that it works. If one of
    those names ever becomes a real setting, the value must be stripped — this
    is where that gets checked rather than assumed."""
    from wavr.config_export import SECRET_SETTINGS, export_config
    from wavr import settings_store

    real = {getattr(spec, "key", spec) for spec in settings_store.SETTING_SPECS}
    overlap = sorted(set(SECRET_SETTINGS) & real)
    if not overlap:
        return                       # today's state; nothing to prove

    doc = export_config(settings=[{"key": k, "value": "hunter2"}
                                  for k in overlap])
    for key in overlap:
        assert key not in doc["settings"], (
            f"{key} is now a real setting AND named as a secret, and its value "
            f"is being exported")


# -- The round trip: export, preview, import -----------------------------------

def test_a_plan_refuses_an_anchor_whose_room_the_import_does_not_bring():
    """An export from a Space that has since been re-drawn carries anchors in
    rooms the incoming floor plan no longer has. Writing one creates an anchor
    pointing at nothing; dropping it silently loses something a person made."""
    from wavr.config_export import export_config, plan_import

    doc = export_config(
        space={"name": "Home", "kind": "home"},
        house={"floors": [{"rooms": [{"name": "kitchen"}]}]},
        anchors=[{"name": "counter", "room": "kitchen"},
                 {"name": "desk", "room": "study"}])
    plan = plan_import(doc)
    assert [a["name"] for a in plan["anchors"]["write"]] == ["counter"]
    assert plan["anchors"]["skipped_no_such_room"] == [
        {"name": "desk", "room": "study"}]


def test_a_camera_comes_back_named_and_not_working():
    """The URL carries a password and never travels. The camera is REPORTED as
    needing one rather than created half-working, so "why is this camera dark"
    is answered before it is asked."""
    from wavr.config_export import export_config, plan_import

    doc = export_config(
        house={"floors": [{"rooms": [{"name": "hall"}]}]},
        cameras=[{"name": "hall-cam", "room": "hall",
                  "rtsp_url": "rtsp://admin:hunter2@10.0.0.9/s"}])
    plan = plan_import(doc)
    assert plan["cameras"] == [{"name": "hall-cam", "room": "hall",
                                "needs_url": True}]
    assert "hunter2" not in json.dumps(plan)


def test_the_digest_changes_with_the_file_and_not_with_the_call():
    from wavr.config_export import config_digest, export_config

    a = export_config(space={"name": "Home", "kind": "home"})
    b = export_config(space={"name": "Other", "kind": "home"})
    assert config_digest(a) == config_digest(a), "not stable across calls"
    assert config_digest(a) != config_digest(b)


def test_importing_without_the_digest_is_refused(monkeypatch, tmp_path):
    """Preview and apply are two requests. Without a confirmation an operator
    can be shown the consequences of one file and apply another — by accident,
    with two tabs, or because a script rebuilt the file in between."""
    c = _client(monkeypatch, tmp_path)
    doc = c.get("/api/config/export").json()

    bare = c.post("/api/config/import", json={"config": doc})
    assert bare.status_code == 400
    assert "preview" in bare.json()["detail"].lower()

    wrong = c.post("/api/config/import",
                   json={"config": doc, "confirm_digest": "0" * 32},
                   )
    assert wrong.status_code == 409
    assert "not the file you previewed" in wrong.json()["detail"]


def test_a_previewed_file_imports_and_says_what_it_did(monkeypatch, tmp_path):
    """The half that did not exist. Export, preview and validate all worked;
    nothing applied anything, so the feature whose purpose is "the Pi died and
    I want my Space back" stopped one step short of giving it back.

    Built from a REAL export rather than a hand-written document, because a
    hand-written one proves the importer accepts what I typed and this proves it
    accepts what Wavr produces — which is the only version anybody will use.
    """
    c = _client(monkeypatch, tmp_path)
    put = c.put("/api/house", json={
        "version": 2, "units": "m",
        "floors": [{"level": 0, "id": "ground", "name": "Ground",
                    "rooms": [{"id": "r1", "name": "kitchen",
                               "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}]}]})
    assert put.status_code == 200, put.text

    doc = c.get("/api/config/export").json()
    assert any(r["name"] == "kitchen"
               for f in doc["house"]["floors"] for r in f["rooms"])

    # Two anchors, one of which names a room this file does not carry.
    doc["anchors"] = [{"name": "counter", "room": "kitchen", "kind": "logical"},
                      {"name": "orphan", "room": "nowhere", "kind": "logical"}]

    seen = c.post("/api/config/preview", json={"config": doc})
    assert seen.status_code == 200, seen.text
    digest = seen.json()["digest"]

    done = c.post("/api/config/import",
                  json={"config": doc, "confirm_digest": digest})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["written"]["rooms"] >= 1
    assert body["written"]["anchors"] == 1
    assert body["anchors_skipped"] == [{"name": "orphan", "room": "nowhere"}]

    # And it is really in the Space, not just in the response.
    anchors = c.get("/api/anchors").json()["anchors"]
    assert [a["name"] for a in anchors] == ["counter"]


def test_a_floor_plan_the_importer_would_reject_is_caught_in_the_preview(
        monkeypatch, tmp_path):
    """The preview says "nothing has been written". If the apply then fails on
    the floor plan, an operator has seen a green preview and a red apply for the
    same file, which teaches them the preview means nothing."""
    c = _client(monkeypatch, tmp_path)
    doc = c.get("/api/config/export").json()
    doc["house"] = {"floors": [{"rooms": [{"name": "kitchen"}]}]}   # no version

    seen = c.post("/api/config/preview", json={"config": doc})
    assert seen.status_code == 400, (
        "the preview accepted a floor plan the import would reject: "
        + seen.text[:200])
    assert "floor plan" in seen.json()["detail"].lower()
