"""The native client command contract names routes the Core really has.

`wavr.client_commands` is data -- command name -> method + path -- that every
native client interprets (through `native/`). A row that drifts from the API
would ship a button that can only ever answer 404 or 405, and nothing else
would notice: the conformance fixture checks the native runtime against the
Python table, not the table against the Core. This file does that.

It also holds two authentication facts native clients depend on:
  * a command marked auth "none" (sent without a token) targets one of the
    Core's unauthenticated onboarding routes, and no other command does;
  * the token header `wavr status` sends is one the Core accepts from a LAN
    device (it used to send X-Wavr-Token, which is read on loopback only).
"""
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from wavr import client_commands as cc

LAN = ("192.168.1.50", 4444)


def _sample(type_):
    return {"string": "x", "bool": True, "int": 1, "strings": ["a"], "any": 1}[type_]


@pytest.fixture
def app(monkeypatch, tmp_path):
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.core_registry import CoreRegistry
    from wavr.discovery_inbox import DiscoveryInbox
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.settings_store import SettingsStore
    from wavr.space_store import SpaceStore
    from wavr.storage import Storage

    db = str(tmp_path / "wavr.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")
    monkeypatch.setenv("WAVR_DB", db)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(db), core_registry=CoreRegistry(db),
        settings_store=SettingsStore(db), discovery_inbox=DiscoveryInbox(db)), db


def _routes_for(app, method, path):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    return [r for r in app.router.routes
            if hasattr(r, "matches") and r.matches(scope)[0] == Match.FULL]


def test_every_command_names_a_route_the_core_has(app):
    app, _db = app
    missing = []
    for name, spec in cc.COMMANDS.items():
        req = cc.request_for(name, {k: _sample(a["type"]) for k, a in spec["args"].items()})
        if not _routes_for(app, req["method"], req["path"]):
            missing.append(f"{name}: {req['method']} {req['path']}")
    assert not missing, "commands with no route behind them:\n  " + "\n  ".join(missing)


def test_the_route_check_can_fail(app):
    # Control: a path the Core does not serve, and a real path with the wrong
    # method, are both reported as missing.
    app, _db = app
    assert not _routes_for(app, "POST", "/api/no-such-thing")
    assert not _routes_for(app, "DELETE", "/api/watch")
    assert _routes_for(app, "POST", "/api/watch")


def test_tokenless_commands_are_exactly_the_onboarding_routes(app):
    from wavr.app import _UNAUTH_ONBOARDING_PATHS
    for name, spec in cc.COMMANDS.items():
        onboarding = spec["path"] in _UNAUTH_ONBOARDING_PATHS
        assert (spec["auth"] == "none") == onboarding, (
            f"{name}: auth {spec['auth']!r} but the route is "
            f"{'an' if onboarding else 'not an'} unauthenticated onboarding route")


def test_the_token_header_wavr_status_sends_authenticates_a_lan_device(app, monkeypatch):
    app, db = app
    from wavr import status
    from wavr.devices import DeviceStore

    store = DeviceStore(db)
    try:
        _device_id, token = store.add("phone", "user")
    finally:
        store.close()
    sent = {}

    def capture(req, timeout):
        sent.update(req.header_items())
        raise OSError("captured, not sent")

    monkeypatch.setattr(status, "open_url", capture)
    with pytest.raises(OSError):
        status._get("https://192.168.1.1:8000", "/api/runtime", token)
    with TestClient(app, client=LAN) as lan:
        assert lan.get("/api/runtime", headers=sent).status_code == 200
        # Control: the same request without the credential is refused.
        without = {k: v for k, v in sent.items() if k.lower() != "authorization"}
        assert lan.get("/api/runtime", headers=without).status_code == 403


def test_a_malformed_call_never_becomes_a_request():
    for name, args, code in [
        ("nope", {}, "unknown_command"),
        ("watch.set", [], "not_object"),
        ("watch.set", {"on": True, "x": 1}, "unknown_argument"),
        ("watch.set", {}, "missing_argument"),
        ("watch.set", {"on": "true"}, "wrong_type"),
        ("device.revoke", {"id": ".."}, "bad_path_segment"),
    ]:
        with pytest.raises(cc.CommandError) as e:
            cc.request_for(name, args)
        assert e.value.code == code, (name, args)
    # A path argument stays ONE segment, whatever it contains.
    assert cc.request_for("device.revoke", {"id": "../../api/block"})["path"] == \
        "/api/devices/..%2F..%2Fapi%2Fblock"
