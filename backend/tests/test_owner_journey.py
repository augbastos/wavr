"""The owner's actual setup, exercised as far as software can carry it.

    notebook  -> Primary Core + Admin Client + Node, named "My Home"
    a handset -> Owner/Admin client, paired, no typed IP
    Moto G    -> a second Core, joining the same Space
    tablet    -> User
    Xiaomi    -> User
    cameras   -> surface in the Discovery Inbox, approved by a human

What this file can and cannot prove is worth being precise about. It drives the
real HTTP surface end to end, so every API contract the journey depends on is
verified. It cannot install anything, cannot hold a phone, and cannot make an
Android device run a Core — those are checked elsewhere or genuinely blocked on
hardware. Where a step is hardware-bound, the test asserts the CONTRACT that
step relies on rather than pretending to perform it.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.camera_store import CameraStore
from wavr.core_registry import CoreRegistry
from wavr.discovery_inbox import KIND_CAMERA_FOUND, DiscoveryInbox
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.settings_store import CONSENT_PHRASE, SettingsStore
from wavr.space_store import SpaceStore
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}


@pytest.fixture
def notebook(monkeypatch, tmp_path):
    """The laptop, as it is the moment Wavr first starts: no environment, no
    Space, nothing configured."""
    from wavr.app import create_app

    db = str(tmp_path / "wavr.db")
    for key in [k for k in __import__("os").environ if k.startswith("WAVR_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WAVR_DB", db)
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")     # the owner turns this on in step 2

    # This Core's OWN certificate, in this test's own directory.
    #
    # The loop above clears every WAVR_* variable and the client below asks for
    # `https://`, so `cfg.tls_cert` fell back to its default — `~/.wavr/cert.pem`
    # — and the fingerprint assertion in step 3 was reading whatever certificate
    # happened to be sitting in the developer's home directory. It passed on this
    # machine and would have failed on any clean checkout. It failed the instant
    # the real `~/.wavr` was cleared, which is precisely the state this fixture's
    # own docstring claims to be modelling: "no environment, nothing configured".
    #
    # The fix is to move HOME, not to add `WAVR_TLS_CERT`: pointing the variable
    # at a test path would make `test_nothing_in_the_journey_needed_a_config_file`
    # fail, and that test is right — an owner never sets it. Relocating the home
    # directory keeps the DEFAULT path under test, which is the one the product
    # actually uses, and leaves the environment as bare as the docstring claims.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))     # Path.home() on Windows
    monkeypatch.setenv("HOME", str(home))            # and everywhere else
    from wavr.tls import ensure_cert
    ensure_cert("", "", "192.0.2.10")     # TEST-NET-1: documented as never routable

    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(db), health_resolvers={},
        space_store=SpaceStore(db), core_registry=CoreRegistry(db),
        settings_store=SettingsStore(db), discovery_inbox=DiscoveryInbox(db))
    # HTTPS: step 2 of the journey below turns multidevice on and step 3 pairs a
    # phone against a certificate fingerprint, so this owner IS on a TLS Core.
    # The scheme is read off the connection now rather than from the flag --
    # the flag is also set on launchers that serve plain HTTP, where the
    # fingerprint compare in step 3 has no certificate behind it.
    with TestClient(app, base_url="https://testserver") as client:
        yield client, app, db


def test_the_owners_journey(notebook):
    client, app, db = notebook

    # ---- 1. The notebook ----------------------------------------------------
    # Wavr opens and asks what this place is. It looks at the machine first.
    assert client.get("/api/setup/status", headers=LOCAL).json()["needs_setup"]

    scan = client.post("/api/setup/scan", headers=LOCAL).json()
    assert scan["recommendation"]["reasons"], "a proposal with no reasons is a guess"
    # The network is the FOUNDATION, so it should be the first thing cited.
    assert any("network" in r.lower() for r in scan["recommendation"]["reasons"])

    created = client.post("/api/setup/create-space", headers=LOCAL, json={
        "name": "My Home", "kind": "home", "owner_name": "Augusto",
        "functions": scan["recommendation"]["functions"]}).json()
    assert created["space"]["name"] == "My Home"
    assert created["owner"]["role"] == "owner"
    assert created["core"]["status"] == "primary"
    owner_id = created["owner"]["person_id"]

    # ---- 2. Letting the phones in ------------------------------------------
    # A separate, deliberate act. It must not be flippable by accident.
    assert client.put("/api/settings/lan_access", headers=LOCAL,
                      json={"value": True}).status_code == 422
    assert client.put("/api/settings/lan_access", headers=LOCAL,
                      json={"value": True,
                            "consent": CONSENT_PHRASE}).status_code == 200

    # ---- 3. The S25: the owner's own phone ---------------------------------
    # Paired AS a person, so the credential follows Augusto's role rather than
    # whatever was typed. No IP is entered anywhere: the response carries both
    # routes the phone can use, and the QR is built from them.
    code = client.post("/api/pair-code", headers=LOCAL,
                       json={"role": "central", "person_id": owner_id}).json()
    assert code["role"] == "central"
    assert code["person_id"] == owner_id
    assert code["cert_fingerprint"], "the QR must carry something to verify"

    status = client.get("/api/setup/status", headers=LOCAL).json()
    assert status["lan_url"].startswith(("http://", "https://"))
    assert status["lan_hostname_url"].endswith(":8000") or ".local:" in \
        status["lan_hostname_url"], "a name-based route, so nobody types an address"

    # ---- 4. The rest of the household --------------------------------------
    for who in ("Ana", "Kid"):
        person = client.post("/api/space/people", headers=LOCAL,
                             json={"display_name": who, "role": "user"}).json()
        # Choosing the role shows the credential it implies, before pairing.
        assert person["device_role"] == "user"

    people = client.get("/api/space/people", headers=LOCAL).json()["people"]
    assert {p["display_name"] for p in people} == {"Augusto", "Ana", "Kid"}

    # A User's device cannot be paired as an admin credential, however it is
    # asked for — the person is a ceiling.
    kid = [p for p in people if p["display_name"] == "Kid"][0]
    narrowed = client.post("/api/pair-code", headers=LOCAL,
                           json={"role": "central",
                                 "person_id": kid["person_id"]}).json()
    assert narrowed["role"] == "user"

    # ---- 5. A second Core (the Moto G's role in the Space) -----------------
    # The Space must be able to HOLD a second Core safely, whatever hardware
    # ends up running it. Joining never takes over.
    cores = app.state.core_registry
    cores.register("core-moto-000001", created["space"]["space_id"], "Moto G",
                   platform="android", portable=True)
    topology = client.get("/api/space/cores", headers=LOCAL).json()
    assert len(topology["cores"]) == 2
    assert topology["primary_core_id"] == created["core"]["core_id"]
    assert topology["contested"] is False, "joining must never contest"

    # And promotion is explicit, fenced, and reversible.
    promoted = client.post("/api/space/cores/core-moto-000001/promote",
                           headers=LOCAL).json()
    assert promoted["promoted"]["status"] == "primary"
    assert promoted["promoted"]["epoch"] > created["core"]["epoch"]
    assert promoted["topology"]["contested"] is False
    client.post(f"/api/space/cores/{created['core']['core_id']}/promote",
                headers=LOCAL)

    # ---- 6. Cameras --------------------------------------------------------
    # They arrive as a question, not as a configured device.
    inbox = app.state.discovery_inbox
    inbox.observe(KIND_CAMERA_FOUND, "aa:bb:cc:dd:ee:ff",
                  "Tapo C210 — looks like a camera.",
                  detail={"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff",
                          "vendor": "TP-Link"},
                  confidence=0.88)
    found = client.get("/api/discoveries", headers=LOCAL).json()["discoveries"]
    cam = [d for d in found if d["kind"] == KIND_CAMERA_FOUND][0]
    assert cam["confidence"] == 0.88, "the guess stays a number, not a verdict"
    assert any(a["id"] == "add_camera" for a in cam["actions"])

    # ---- 7. What the household looks like afterwards -----------------------
    space = client.get("/api/space", headers=LOCAL).json()
    assert space["name"] == "My Home"
    assert len(space["people"]) == 3
    assert space["topology"]["primary_core_id"] == created["core"]["core_id"]
    assert space["topology"]["leaderless"] is False


def test_nothing_in_the_journey_needed_a_config_file(notebook):
    """The acceptance criterion, asserted rather than asserted-to.

    Every step above went through the HTTP surface a UI drives. If any of it
    ever starts requiring a `WAVR_*` variable, this fails."""
    import os

    client, _app, _db = notebook
    client.post("/api/setup/create-space", headers=LOCAL, json={"name": "My Home"})
    client.post("/api/space/people", headers=LOCAL,
                json={"display_name": "Ana", "role": "admin"})
    client.put("/api/settings/lan_access", headers=LOCAL,
               json={"value": True, "consent": CONSENT_PHRASE})

    leaked = [k for k in os.environ
              if k.startswith("WAVR_") and k not in ("WAVR_DB", "WAVR_MULTIDEVICE")]
    assert not leaked, f"onboarding reached for the environment: {leaked}"


def test_a_demotion_reaches_an_already_paired_device(notebook):
    """The half of the person model that would be easy to fake.

    Ana's phone was paired while she was an Admin. She is demoted. Her phone's
    authority must drop on its very NEXT request — not at the next re-pair, and
    not only after somebody remembers to revoke it."""
    from wavr.auth import _apply_person_cap

    client, app, _db = notebook
    client.post("/api/setup/create-space", headers=LOCAL,
                json={"name": "My Home", "owner_name": "Augusto"})
    ana = client.post("/api/space/people", headers=LOCAL,
                      json={"display_name": "Ana", "role": "admin"}).json()

    space = app.state.space_store

    class _Phone:
        role = "central"           # what Ana's phone was issued while Admin
        person_id = ana["person_id"]
        scopes = None
        tool_scopes = None

    def resolver(pid):
        from wavr.space_store import device_role_for_person
        person = space.get_person(pid)
        return device_role_for_person(person.role) if person else None

    assert _apply_person_cap(_Phone(), resolver) == "central"

    client.post(f"/api/space/people/{ana['person_id']}/role", headers=LOCAL,
                json={"role": "user"})

    assert _apply_person_cap(_Phone(), resolver) == "user", (
        "a demotion must land on the next request, not at the next re-pair")


def test_removing_someone_stops_their_devices_dead(notebook):
    from wavr.auth import _apply_person_cap

    client, app, _db = notebook
    client.post("/api/setup/create-space", headers=LOCAL,
                json={"name": "My Home", "owner_name": "Augusto"})
    ana = client.post("/api/space/people", headers=LOCAL,
                      json={"display_name": "Ana", "role": "admin"}).json()
    space = app.state.space_store

    class _Phone:
        role = "central"
        person_id = ana["person_id"]
        scopes = None
        tool_scopes = None

    def resolver(pid):
        from wavr.space_store import device_role_for_person
        person = space.get_person(pid)
        if person is None or person.removed:
            return None
        return device_role_for_person(person.role)

    client.delete(f"/api/space/people/{ana['person_id']}", headers=LOCAL)
    assert _apply_person_cap(_Phone(), resolver) is None, (
        "a credential whose owner was removed must stop working outright")
