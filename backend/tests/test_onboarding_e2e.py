"""End-to-end onboarding, and the acceptance criterion it has to satisfy.

The product criterion (the "ordinary user" test) is that a normal installation
must not require the operator to:

  * edit `.env`
  * run Python by hand
  * type a backend IP or a port
  * open a terminal
  * edit YAML
  * enter an internal identifier

The first four are properties of the *installer*, which lives outside pytest.
What CAN be pinned down here is the API contract those installers depend on:
that every step from "a server is running" to "a named Space with an Owner, a
Core, people, device functions and LAN access" is reachable through calls a UI
makes, with **no environment variable set by anyone**, and without the caller
ever being handed an internal id to retype.

So this file drives the whole journey with a completely empty environment. If
any step ever starts needing a `WAVR_*` variable, it fails here.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.core_registry import CoreRegistry
from wavr.discovery_inbox import DiscoveryInbox
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.settings_store import CONSENT_PHRASE, SettingsStore
from wavr.space_store import SpaceStore
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}


@pytest.fixture
def core(monkeypatch):
    """A Core booted with NO Wavr environment at all — the state a fresh
    install is in before anyone has touched a config file."""
    import os

    for key in [k for k in os.environ if k.startswith("WAVR_")]:
        monkeypatch.delenv(key, raising=False)

    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(":memory:"), core_registry=CoreRegistry(":memory:"),
        settings_store=SettingsStore(":memory:"),
        discovery_inbox=DiscoveryInbox(":memory:"))
    with TestClient(app) as client:
        yield client


def test_the_full_first_run_journey_with_an_empty_environment(core):
    # 1. The app asks itself whether it is set up. This is the only thing the
    #    UI needs to know before deciding what to show.
    status = core.get("/api/setup/status", headers=LOCAL).json()
    assert status["needs_setup"] is True

    # 2. It looks at the machine. No input, no configuration, no consent needed
    #    yet — this reads local OS state only.
    scan = core.post("/api/setup/scan", headers=LOCAL).json()
    recommendation = scan["recommendation"]
    assert recommendation["functions"], "the scan must propose something"
    assert recommendation["reasons"], "and be able to say why"

    # 3. The operator names the place and themselves, and accepts the proposal.
    #    Two strings; nothing technical.
    created = core.post("/api/setup/create-space", headers=LOCAL, json={
        "name": "My Home", "kind": "home", "owner_name": "Augusto",
        "functions": recommendation["functions"]}).json()
    assert created["space"]["name"] == "My Home"
    assert created["owner"]["role"] == "owner"

    # 4. Wavr is now a named Space with someone in charge of it.
    space = core.get("/api/space", headers=LOCAL).json()
    assert space["name"] == "My Home"
    assert [p["display_name"] for p in space["people"]] == ["Augusto"]
    assert space["topology"]["primary_core_id"]
    assert space["topology"]["contested"] is False

    # 5. Family. Choosing a role shows what credential it implies, at the moment
    #    of choosing rather than after something goes wrong.
    ana = core.post("/api/space/people", headers=LOCAL,
                    json={"display_name": "Ana", "role": "admin"}).json()
    assert ana["device_role"] == "central"
    kid = core.post("/api/space/people", headers=LOCAL,
                    json={"display_name": "Kid", "role": "user"}).json()
    assert kid["device_role"] == "user"

    # 6. Letting other devices in is a separate, explicit act — and it happens
    #    here, over HTTP, not in a text editor.
    refused = core.put("/api/settings/lan_access", headers=LOCAL,
                       json={"value": True})
    assert refused.status_code == 422, "a sensitive switch must not flip silently"

    allowed = core.put("/api/settings/lan_access", headers=LOCAL,
                       json={"value": True, "consent": CONSENT_PHRASE})
    assert allowed.status_code == 200 and allowed.json()["value"] == "1"

    # 7. Nothing in any of that required a WAVR_* variable to exist.
    import os
    assert not [k for k in os.environ if k.startswith("WAVR_")], (
        "onboarding must not depend on the environment")


def test_no_step_asks_the_operator_to_type_an_address(core):
    """The Core tells the UI its own address; the operator never types one.

    A first-run flow that asks for an IP has already failed the criterion, so
    assert the address is *supplied*, and that it is a URL a browser can use."""
    status = core.get("/api/setup/status", headers=LOCAL).json()
    assert status["lan_url"].startswith(("http://", "https://"))
    # And with TLS off (the default), it must not claim https — a scheme that
    # cannot connect is the same class of failure as no address at all.
    assert status["lan_url"].startswith("http://")


def test_no_step_hands_back_an_identifier_to_retype(core):
    """Ids exist, but they travel in the payload — never as something a human
    is expected to copy from one screen into another."""
    created = core.post("/api/setup/create-space", headers=LOCAL,
                        json={"name": "My Home"}).json()
    # Everything the next call needs is already in this response.
    core_id = created["core"]["core_id"]
    assert core.post(f"/api/space/cores/{core_id}/room", headers=LOCAL,
                     json={"room": "Office"}).status_code == 200


def test_an_existing_install_is_adopted_without_losing_anything(core):
    """Someone who already ran Wavr must not be told to start over."""
    body = core.post("/api/setup/adopt", headers=LOCAL, json={
        "name": "The Flat", "owner_name": "Augusto"}).json()
    assert body["space"]["name"] == "The Flat"
    assert body["owner"]["role"] == "owner"
    assert body["core"]["status"] == "primary"
    assert core.get("/api/setup/status", headers=LOCAL).json()["needs_setup"] is False


def test_the_default_install_is_still_loopback_only(core):
    """The onboarding makes LAN access reachable; it must not make it the
    default. Everything above ran on a Core that is closed to the network."""
    core.post("/api/setup/create-space", headers=LOCAL, json={"name": "My Home"})
    rows = {r["key"]: r for r in core.get("/api/settings", headers=LOCAL).json()["settings"]}
    assert rows["lan_access"]["value"] == "0"
    assert rows["bind_host"]["value"] == "127.0.0.1"
    assert rows["net_inventory"]["value"] == "0"
    assert rows["identity_enabled"]["value"] == "0"


def test_every_switch_that_changes_exposure_is_marked_sensitive(core):
    """'Discover aggressively, activate conservatively' as an assertion.

    Any setting that widens what Wavr can see or who can reach it must require
    the explicit acknowledgement, so it can never be flipped as a side effect of
    a generic save."""
    rows = {r["key"]: r for r in core.get("/api/settings", headers=LOCAL).json()["settings"]}
    for key in ("lan_access", "bind_host", "nodes_enabled", "peers_enabled",
                "net_inventory", "onvif_probe", "identity_enabled"):
        assert rows[key]["sensitive"] is True, f"{key} should require consent"
    # ...and the harmless ones must NOT, or the acknowledgement becomes noise
    # that people learn to click through.
    for key in ("instance_name", "port", "fusion_threshold"):
        assert rows[key]["sensitive"] is False, f"{key} should not need consent"


def test_a_setting_the_operator_pinned_in_the_environment_is_shown_as_locked(
        core, monkeypatch):
    """If a real `.env` beats the UI, the UI has to say so rather than
    accepting a click that will never take effect."""
    monkeypatch.setenv("WAVR_INSTANCE_NAME", "SetByHand")
    rows = {r["key"]: r for r in core.get("/api/settings", headers=LOCAL).json()["settings"]}
    assert rows["instance_name"]["value"] == "SetByHand"
    assert rows["instance_name"]["source"] == "env"
    assert rows["instance_name"]["locked"] is True
