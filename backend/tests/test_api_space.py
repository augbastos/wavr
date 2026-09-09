"""The Space HTTP surface: its auth gates first, then the onboarding journey.

Gates before features, deliberately. These routes create the Space, mint its
Owner and change what Wavr is exposed to — if the gating is wrong, nothing else
in this file matters.
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


@pytest.fixture(autouse=True)
def _rede_fixa(monkeypatch):
    """Every test here builds a peer; none of them care which /24 the
    machine running them is on, and all of them silently did."""
    # `in_subnet` compares the peer against what THIS MACHINE thinks its
    # own address is, so an unpinned LAN peer makes this test true on a
    # 192.168.1.x developer box and false (or true for the wrong reason)
    # anywhere else. Same idiom as test_app.py.
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")


def build(client=None, **kw):
    """A Core with every new store in memory, so a Space created by one test
    cannot decide the outcome of the next."""
    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(":memory:"), core_registry=CoreRegistry(":memory:"),
        settings_store=SettingsStore(":memory:"),
        discovery_inbox=DiscoveryInbox(":memory:"), **kw)
    return TestClient(app, **({"client": client} if client else {}))


# -- Gates -------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/setup/status"),
    ("post", "/api/setup/scan"),
    ("post", "/api/setup/create-space"),
    ("get", "/api/space"),
    ("get", "/api/space/people"),
    ("get", "/api/space/cores"),
    ("get", "/api/settings"),
    ("get", "/api/discoveries"),
])
def test_every_space_route_needs_the_csrf_header(method, path):
    # A same-origin drive-by fetch() from a hostile page must not be able to
    # re-found the Space or read the household roster using the operator's
    # own browser session.
    with build() as c:
        assert getattr(c, method)(path).status_code == 403


@pytest.mark.parametrize("path", [
    "/api/setup/status", "/api/space", "/api/settings", "/api/discoveries"])
def test_a_lan_peer_cannot_reach_them_at_all_by_default(path):
    # Default install is loopback-only; the middleware rejects before routing.
    with build(client=("192.168.1.77", 5555)) as c:
        assert c.get(path, headers=LOCAL).status_code == 403


def test_fail_closed_when_a_gate_is_not_wired():
    from wavr.api_space import build_space_router

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(build_space_router(SpaceStore(":memory:"),
                                          CoreRegistry(":memory:")))
    with TestClient(app) as c:
        r = c.get("/api/space")
        assert r.status_code == 403
        assert "no auth gate wired" in r.json()["detail"]


def test_ownership_transfer_is_gated_more_strictly_than_administration():
    """Giving the Space away must not ride the ordinary admin gate.

    Nothing in a request says WHICH PERSON holds a credential, so the store's
    "only the Owner may transfer" rule cannot be enforced from an admin token.
    The route therefore carries its own dependency list, and wiring only the
    admin gate must leave transfer closed rather than open."""
    from fastapi import Depends, FastAPI, HTTPException

    from wavr.api_space import build_space_router

    def _allow():
        return None

    def _deny():
        raise HTTPException(status_code=403, detail="owner gate")

    app = FastAPI()
    # Admin gate wide open, owner gate closed: the split must hold.
    app.include_router(build_space_router(
        SpaceStore(":memory:"), CoreRegistry(":memory:"),
        deps=[Depends(_allow)], owner_deps=[Depends(_deny)]))
    with TestClient(app) as c:
        assert c.get("/api/space/people").status_code == 200
        assert c.post("/api/space/transfer",
                      json={"to_person_id": "x"}).status_code == 403


def test_transfer_fails_closed_when_its_gate_is_not_wired():
    from fastapi import Depends, FastAPI

    from wavr.api_space import build_space_router

    app = FastAPI()
    app.include_router(build_space_router(
        SpaceStore(":memory:"), CoreRegistry(":memory:"),
        deps=[Depends(lambda: None)]))      # owner_deps deliberately omitted
    with TestClient(app) as c:
        r = c.post("/api/space/transfer", json={"to_person_id": "x"})
        assert r.status_code == 403
        assert "no auth gate wired" in r.json()["detail"]


# -- First run ---------------------------------------------------------------

def test_a_fresh_core_reports_that_it_needs_setup():
    with build() as c:
        body = c.get("/api/setup/status", headers=LOCAL).json()
        assert body["needs_setup"] is True
        assert body["space"] is None and body["people"] == 0


def test_capability_scan_returns_a_manifest_and_a_reasoned_recommendation():
    with build() as c:
        body = c.post("/api/setup/scan", headers=LOCAL).json()
        assert set(body) == {"manifest", "recommendation"}
        assert body["manifest"]["platform"] in (
            "windows", "linux", "macos", "android", "ios", "unknown")
        assert body["recommendation"]["label"]
        assert body["recommendation"]["reasons"], "a bare recommendation is a guess"


def test_the_whole_create_space_journey():
    with build() as c:
        r = c.post("/api/setup/create-space", headers=LOCAL, json={
            "name": "My Home", "kind": "home", "owner_name": "Alex",
            "functions": ["core", "node", "client"]})
        assert r.status_code == 200
        body = r.json()
        assert body["space"]["name"] == "My Home"
        assert body["owner"]["role"] == "owner"
        assert body["core"]["status"] == "primary"
        # Promotion fenced the Space's epoch, so a later Core cannot replay it.
        assert body["core"]["epoch"] >= 2

        assert c.get("/api/setup/status", headers=LOCAL).json()["needs_setup"] is False
        space = c.get("/api/space", headers=LOCAL).json()
        assert space["name"] == "My Home"
        assert space["topology"]["primary_core_id"] == body["core"]["core_id"]
        assert space["topology"]["contested"] is False


def test_a_space_cannot_be_created_twice():
    with build() as c:
        c.post("/api/setup/create-space", headers=LOCAL, json={"name": "One"})
        r = c.post("/api/setup/create-space", headers=LOCAL, json={"name": "Two"})
        assert r.status_code == 400
        assert "already belongs" in r.json()["detail"]


def test_unknown_device_function_is_refused():
    with build() as c:
        r = c.post("/api/setup/create-space", headers=LOCAL,
                   json={"name": "Home", "functions": ["core", "toaster"]})
        assert r.status_code == 422


def test_nearby_reports_unavailable_rather_than_failing_without_zeroconf():
    with build() as c:
        body = c.get("/api/setup/nearby", headers=LOCAL).json()
        assert set(body) >= {"cores"}
        assert isinstance(body["cores"], list)


def test_space_404s_before_it_exists():
    with build() as c:
        assert c.get("/api/space", headers=LOCAL).status_code == 404


# -- Multidevice: where the DeviceStore is real -------------------------------
# Nothing else in the suite builds a multidevice app, which is exactly why a
# wrong method name on `DeviceStore` survived: `_devices` is None otherwise, so
# the broken call was never reached.

def test_an_existing_install_is_recognised_as_one(monkeypatch):
    """Regression (HIGH): `DeviceStore` exposes `list()`, not `list_devices()`.

    The wrong name sat inside a bare `except Exception`, so an operator with a
    working multidevice install saw `existing_devices: 0` — which makes the
    wizard say "Welcome to Wavr" and never offer the adopt path at all."""
    from wavr.devices import DeviceStore

    devices = DeviceStore(":memory:")
    devices.add("laptop", "central")
    devices.add("phone", "user")

    from wavr.api_space import build_setup_router

    from fastapi import Depends, FastAPI

    app = FastAPI()
    app.include_router(build_setup_router(
        SpaceStore(":memory:"), SettingsStore(":memory:"), CoreRegistry(":memory:"),
        devices=devices, deps=[Depends(lambda: None)]))
    with TestClient(app) as c:
        body = c.get("/api/setup/status").json()
        assert body["existing_devices"] == 2, (
            "an existing install must be seen, or the adopt path is never offered")


def test_adopt_actually_adopts_the_existing_devices():
    """Second half of the same regression: `adopted_devices` was always 0, so
    no existing `central` device was ever associated with the new Owner."""
    from wavr.devices import DeviceStore

    from wavr.api_space import build_setup_router

    from fastapi import Depends, FastAPI

    devices = DeviceStore(":memory:")
    devices.add("laptop", "central")
    devices.add("phone", "user")
    space = SpaceStore(":memory:")

    app = FastAPI()
    app.include_router(build_setup_router(
        space, SettingsStore(":memory:"), CoreRegistry(":memory:"),
        devices=devices, deps=[Depends(lambda: None)]))
    with TestClient(app) as c:
        body = c.post("/api/setup/adopt",
                      json={"name": "My Home", "owner_name": "Alex"}).json()
        assert body["adopted_devices"] == 2
    owner = space.list_people()[0]
    central_id = [d.device_id for d in devices.list() if d.role == "central"][0]
    user_id = [d.device_id for d in devices.list() if d.role == "user"][0]
    # The central device becomes the Owner's; the other is left unclaimed
    # rather than guessed at.
    assert space.person_of_device(central_id) == (owner.person_id, "confirmed")
    assert space.person_of_device(user_id) is None


def test_a_failure_midway_through_setup_rolls_the_space_back(monkeypatch):
    """Regression (HIGH): setup was not atomic despite its docstring.

    One "database is locked" after the Space row was written left a Space with
    an Owner, no Core, `needs_setup: false` forever, and every retry answering
    400 "already belongs to a Space". There is no reset route, so the only
    recovery was deleting the database by hand."""
    import sqlite3

    from fastapi import Depends, FastAPI

    from wavr.api_space import build_setup_router

    space = SpaceStore(":memory:")
    cores = CoreRegistry(":memory:")

    def _explode(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cores, "register", _explode)

    app = FastAPI()
    app.include_router(build_setup_router(
        space, SettingsStore(":memory:"), cores, deps=[Depends(lambda: None)]))
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/api/setup/create-space",
                      json={"name": "My Home"}).status_code == 500

        # Nothing was left behind...
        assert space.get_space() is None
        assert space.list_people() == []
        assert c.get("/api/setup/status").json()["needs_setup"] is True

    # ...and the operator can simply try again.
    monkeypatch.undo()
    app2 = FastAPI()
    app2.include_router(build_setup_router(
        space, SettingsStore(":memory:"), CoreRegistry(":memory:"),
        deps=[Depends(lambda: None)]))
    with TestClient(app2) as c:
        assert c.post("/api/setup/create-space",
                      json={"name": "My Home"}).status_code == 200


# -- People ------------------------------------------------------------------

@pytest.fixture
def home():
    c = build()
    c.__enter__()
    c.post("/api/setup/create-space", headers=LOCAL,
           json={"name": "My Home", "owner_name": "Alex"})
    yield c
    c.__exit__(None, None, None)


def test_add_a_person_and_see_the_credential_they_will_get(home):
    r = home.post("/api/space/people", headers=LOCAL,
                  json={"display_name": "Sam", "role": "admin"})
    assert r.status_code == 200
    # The consequence of the role is shown at the moment it is chosen.
    assert r.json()["device_role"] == "central"
    assert home.post("/api/space/people", headers=LOCAL,
                     json={"display_name": "Kid", "role": "user"}
                     ).json()["device_role"] == "user"


def test_a_second_owner_is_refused(home):
    r = home.post("/api/space/people", headers=LOCAL,
                  json={"display_name": "Impostor", "role": "owner"})
    assert r.status_code == 422


def test_role_change_discloses_that_live_tokens_keep_their_reach(home):
    ana = home.post("/api/space/people", headers=LOCAL,
                    json={"display_name": "Sam", "role": "admin"}).json()
    r = home.post(f"/api/space/people/{ana['person_id']}/role", headers=LOCAL,
                  json={"role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    # Honesty: nothing silently revokes, and the caller is told so.
    assert "note" in r.json()


def test_transfer_moves_ownership_and_demotes_the_previous_owner(home):
    ana = home.post("/api/space/people", headers=LOCAL,
                    json={"display_name": "Sam", "role": "admin"}).json()
    r = home.post("/api/space/transfer", headers=LOCAL,
                  json={"to_person_id": ana["person_id"]})
    assert r.status_code == 200
    assert r.json()["owner"]["display_name"] == "Sam"
    assert r.json()["previous_owner"]["role"] == "admin"


def test_profile_edits_cannot_widen_capabilities(home):
    ana = home.post("/api/space/people", headers=LOCAL,
                    json={"display_name": "Sam", "role": "user"}).json()
    r = home.post(f"/api/space/people/{ana['person_id']}/profile", headers=LOCAL,
                  json={"language": "pt-BR", "role": "owner",
                        "capabilities": ["space:transfer"]})
    assert r.status_code == 200
    assert r.json()["role"] == "user"
    assert "space:transfer" not in r.json()["capabilities"]


# -- Device functions --------------------------------------------------------

def test_a_device_can_hold_all_three_functions(home):
    r = home.put("/api/space/devices/laptop-1/functions", headers=LOCAL,
                 json={"functions": ["core", "node", "client"], "room": "Office"})
    assert r.status_code == 200
    assert set(r.json()["functions"]) == {"core", "node", "client"}
    assert r.json()["room"] == "Office"


def test_a_self_reported_manifest_grants_nothing(home):
    r = home.put("/api/space/devices/liar-1/manifest", headers=LOCAL, json={
        "platform": "linux", "functions_supported": ["core"],
        "capabilities": {"gpu": True}, "compute_tier": "high"})
    assert r.status_code == 200
    # It is stored and it informs a recommendation...
    assert r.json()["recommendation"]["label"]
    # ...but it did not make the device a Core.
    assert home.get("/api/space/cores", headers=LOCAL).json()["cores"][0]["is_self"]
    assert all(c["core_id"] != "liar-1"
               for c in home.get("/api/space/cores", headers=LOCAL).json()["cores"])


def test_an_off_spec_manifest_is_normalised_not_stored_verbatim(home):
    r = home.put("/api/space/devices/d/manifest", headers=LOCAL,
                 json={"platform": "solaris", "capabilities": {"wifi": "yes"}})
    m = r.json()["manifest"]
    assert m["platform"] == "unknown"
    assert m["capabilities"]["wifi"] is None


# -- Cores -------------------------------------------------------------------

def test_promoting_a_second_core_fences_the_epoch(home):
    before = home.get("/api/space/cores", headers=LOCAL).json()
    first = before["cores"][0]
    r = home.get("/api/space", headers=LOCAL).json()
    assert r["topology"]["primary_core_id"] == first["core_id"]
    # Demote and confirm the Space honestly reports having no leader.
    home.post(f"/api/space/cores/{first['core_id']}/demote", headers=LOCAL)
    after = home.get("/api/space/cores", headers=LOCAL).json()
    assert after["leaderless"] is True and after["primary_core_id"] is None


def test_the_primary_core_cannot_be_deleted(home):
    cid = home.get("/api/space/cores", headers=LOCAL).json()["cores"][0]["core_id"]
    r = home.delete(f"/api/space/cores/{cid}", headers=LOCAL)
    assert r.status_code == 422
    assert "promote another Core first" in r.json()["detail"]


def test_a_portable_core_can_be_rehomed(home):
    cid = home.get("/api/space/cores", headers=LOCAL).json()["cores"][0]["core_id"]
    r = home.post(f"/api/space/cores/{cid}/room", headers=LOCAL,
                  json={"room": "Kitchen"})
    assert r.status_code == 200 and r.json()["room"] == "Kitchen"


# -- Settings ----------------------------------------------------------------

def test_settings_list_shows_provenance(home):
    rows = {r["key"]: r for r in home.get("/api/settings", headers=LOCAL).json()["settings"]}
    assert rows["instance_name"]["source"] == "default"
    assert rows["lan_access"]["sensitive"] is True


def test_a_sensitive_setting_needs_the_consent_phrase_over_http(home):
    r = home.put("/api/settings/lan_access", headers=LOCAL, json={"value": True})
    assert r.status_code == 422 and "acknowledgement" in r.json()["detail"]

    ok = home.put("/api/settings/lan_access", headers=LOCAL,
                  json={"value": True, "consent": CONSENT_PHRASE})
    assert ok.status_code == 200 and ok.json()["value"] == "1"


def test_an_unknown_setting_key_is_refused(home):
    assert home.put("/api/settings/PATH", headers=LOCAL,
                    json={"value": "/evil"}).status_code == 422


def test_out_of_range_values_are_refused(home):
    assert home.put("/api/settings/port", headers=LOCAL,
                    json={"value": 80}).status_code == 422


# -- Discoveries -------------------------------------------------------------

def test_the_inbox_starts_empty_and_reports_counts(home):
    body = home.get("/api/discoveries", headers=LOCAL).json()
    assert body["discoveries"] == []
    assert body["counts"]["pending"] == 0


def test_accept_and_dismiss_round_trip(home):
    from wavr.discovery_inbox import KIND_CAMERA_FOUND

    inbox = home.app.state.discovery_inbox
    d = inbox.observe(KIND_CAMERA_FOUND, "aa:bb", "Tapo C210", confidence=0.8)

    listed = home.get("/api/discoveries", headers=LOCAL).json()["discoveries"]
    assert len(listed) == 1 and listed[0]["confidence"] == 0.8
    assert any(a["id"] == "add_camera" for a in listed[0]["actions"])

    assert home.post(f"/api/discoveries/{d.discovery_id}/dismiss",
                     headers=LOCAL).json()["status"] == "dismissed"
    assert home.get("/api/discoveries", headers=LOCAL).json()["discoveries"] == []


def test_deciding_an_unknown_discovery_is_a_404(home):
    assert home.post("/api/discoveries/nope/accept", headers=LOCAL).status_code == 404

# -- The switches that take Wavr off its own machine --------------------------
# `central` holds `admin` by default, and every central device sits on the very
# network these settings would expose. So scope is the wrong fence for them.

def _settings_app(settings, *, local: bool):
    from fastapi import Depends, FastAPI

    from wavr.api_space import build_settings_router
    app = FastAPI()
    app.include_router(build_settings_router(
        settings, deps=[Depends(lambda: None)],
        is_local_fn=lambda request: local))
    return TestClient(app)


def test_lan_access_cannot_be_switched_on_from_the_lan(tmp_path):
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        with _settings_app(s, local=False) as c:
            r = c.put("/api/settings/lan_access",
                      json={"value": True, "consent": "i-understand"})
            assert r.status_code == 403
            assert "machine running" in r.json()["detail"]
        assert s.get("lan_access") is None, "nothing was stored"
    finally:
        s.close()


def test_the_same_switch_works_at_the_core_itself(tmp_path):
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        with _settings_app(s, local=True) as c:
            r = c.put("/api/settings/lan_access",
                      json={"value": True, "consent": "i-understand"})
            assert r.status_code == 200
    finally:
        s.close()


def test_ordinary_settings_are_still_reachable_from_a_paired_admin(tmp_path):
    """The fence is narrow on purpose: an admin's phone is still an admin."""
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        with _settings_app(s, local=False) as c:
            assert c.put("/api/settings/instance_name",
                         json={"value": "Kitchen Core"}).status_code == 200
            assert c.get("/api/settings").status_code == 200, "reading stays open"
    finally:
        s.close()


def test_clearing_an_exposure_switch_is_also_a_write(tmp_path):
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        with _settings_app(s, local=False) as c:
            assert c.delete("/api/settings/bind_host").status_code == 403
    finally:
        s.close()


def test_with_no_local_check_wired_the_answer_is_no(tmp_path):
    """Fails closed. A router assembled without its gate must not be the
    permissive one — the same rule every other router here follows."""
    from fastapi import Depends, FastAPI

    from wavr.api_space import build_settings_router
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        app = FastAPI()
        app.include_router(build_settings_router(s, deps=[Depends(lambda: None)]))
        with TestClient(app) as c:
            assert c.put("/api/settings/peers_enabled",
                         json={"value": True,
                               "consent": "i-understand"}).status_code == 403
    finally:
        s.close()


def test_the_ui_is_told_which_keys_are_local_only(tmp_path):
    """So a companion disables them with a reason instead of offering a click
    that comes back 403."""
    from wavr.settings_store import SettingsStore
    s = SettingsStore(str(tmp_path / "s.db"))
    try:
        rows = {r["key"]: r for r in s.describe()}
        assert rows["lan_access"]["local_only"] is True
        assert rows["bind_host"]["local_only"] is True
        assert rows["nodes_enabled"]["local_only"] is True
        assert rows["peers_enabled"]["local_only"] is True
        assert rows["instance_name"]["local_only"] is False
        assert rows["fusion_threshold"]["local_only"] is False
    finally:
        s.close()


# -- the Space is not always a home --------------------------------------------

def test_a_second_core_records_the_kind_it_is_told(tmp_path):
    """`kind` was hardcoded `"home"` on the join path.

    A clinic running two Cores ended up with an office Space on Core A and a
    home Space on Core B — the same Space describing itself two different ways
    depending on which one your phone was paired to. The joiner is already
    asked for the NAME for exactly this reason (joining does not synchronise
    state, so the Core cannot discover it), and the kind is the same class of
    fact.
    """
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    os.environ["WAVR_DB"] = str(tmp_path / "b.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})

    r = c.post("/api/setup/join-space",
               json={"space_id": "spsharedclinic1", "name": "North Clinic",
                     "kind": "office", "owner_name": "Tester"})
    assert r.status_code == 200, r.text
    assert r.json()["space"]["kind"] == "office", r.json()["space"]

    got = c.get("/api/space").json()
    assert got["kind"] == "office", (
        "the second Core recorded a different kind for the same Space")


def test_joining_without_a_kind_still_works(tmp_path):
    """The parameter is optional, so an older client that does not send one
    behaves exactly as before."""
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.space_store import DEFAULT_SPACE_KIND

    os.environ["WAVR_DB"] = str(tmp_path / "c.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    r = c.post("/api/setup/join-space",
               json={"space_id": "spsharedhome2", "name": "Home"})
    assert r.status_code == 200, r.text
    assert r.json()["space"]["kind"] == DEFAULT_SPACE_KIND


def test_a_fresh_install_has_no_invented_rooms(tmp_path):
    """`DEFAULT_MAP` was a three-room Portuguese house — `sala`, `quarto`,
    `quintal`, on a floor called `Térreo` — and it shipped as the fallback for
    every install. Somebody putting Wavr in a workshop landed on a map of three
    rooms that do not exist, with nothing marking them as placeholders, and
    coverage then reported them as real."""
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    os.environ["WAVR_DB"] = str(tmp_path / "d.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    house = c.get("/api/house").json()
    assert house["floors"], "a plan with no floors at all is not the fix"
    assert house["floors"][0]["rooms"] == [], (
        f"a fresh install still ships invented rooms: "
        f"{[r.get('name') for r in house['floors'][0]['rooms']]}")
    assert "Térreo" not in house["floors"][0]["name"], (
        "the floor name is still Portuguese in an English product")


def test_setup_puts_the_room_the_operator_named_on_the_map(tmp_path):
    """The wizard asks which room the Core is in and used that answer only to
    label the Core, so the first screen after setup showed a map of nothing
    beside a Core that knew perfectly well it was in the kitchen."""
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    os.environ["WAVR_DB"] = str(tmp_path / "e.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    r = c.post("/api/setup/create-space",
               json={"name": "Workshop", "kind": "workshop",
                     "owner_name": "Tester", "room": "Bench room"})
    assert r.status_code == 200, r.text
    assert r.json()["room_seeded"] is True
    rooms = [x["name"] for x in
             c.get("/api/house").json()["floors"][0]["rooms"]]
    assert rooms == ["Bench room"], rooms


def test_seeding_never_overwrites_a_plan_somebody_drew(tmp_path):
    """The narrow part of the rule, and the one worth a test: a Core adopted
    into an existing install must not have its floor plan replaced by one room
    named in a wizard."""
    import json
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    plan = {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "Ground floor", "level": 0, "walls": [],
         "features": [], "zones": [], "backdrop": None,
         "rooms": [{"id": "r_a", "name": "Studio",
                    "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]}]}]}
    path = tmp_path / "house.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    os.environ["WAVR_DB"] = str(tmp_path / "f.db")
    os.environ["WAVR_HOUSE_MAP"] = str(path)
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    r = c.post("/api/setup/create-space",
               json={"name": "Studio Space", "owner_name": "T",
                     "room": "Bench room"})
    assert r.status_code == 200, r.text
    assert r.json()["room_seeded"] is False
    rooms = [x["name"] for x in
             c.get("/api/house").json()["floors"][0]["rooms"]]
    assert rooms == ["Studio"], rooms


def test_the_policy_field_can_actually_be_written(tmp_path):
    """`GET /api/space` returned `"policy": {}` on every install forever.

    `SpaceStore.set_policy` existed, with its own bounded-JSON validation, and
    had no route and no caller — so an integrator reading the API took `policy`
    for a real per-Space configuration surface and built against a field the
    product could not populate.
    """
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    os.environ["WAVR_DB"] = str(tmp_path / "p.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    c.post("/api/setup/create-space", json={"name": "H", "owner_name": "T"})

    assert c.get("/api/space").json()["policy"] == {}
    r = c.put("/api/space/policy",
              json={"policy": {"quiet_hours": {"from": "22:00", "to": "07:00"}}})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["quiet_hours"]["from"] == "22:00"
    assert c.get("/api/space").json()["policy"]["quiet_hours"]["to"] == "07:00"


def test_an_oversized_policy_is_refused_rather_than_stored(tmp_path):
    """The size cap `set_policy` already applied is the reason exposing it was
    the small change. It has to still apply through the route."""
    import os
    from fastapi.testclient import TestClient
    from wavr.app import create_app

    os.environ["WAVR_DB"] = str(tmp_path / "q.db")
    os.environ["WAVR_HOUSE_MAP"] = str(tmp_path / "house.json")
    os.environ["WAVR_LOCAL_TOKEN"] = ""
    c = TestClient(create_app(), headers={"X-Wavr-Local": "1"})
    c.post("/api/setup/create-space", json={"name": "H", "owner_name": "T"})

    r = c.put("/api/space/policy", json={"policy": {"x": "y" * 200_000}})
    assert r.status_code in (400, 413, 422), r.status_code
    assert c.get("/api/space").json()["policy"] == {}
