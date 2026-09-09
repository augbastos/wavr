"""A person's role controls access — and only ever narrows it.

The property under test: demoting somebody takes effect on their device's very
next request, and promoting somebody never widens a credential already in the
wild. Everything else here defends the "never widens" half, because that is the
half an attacker would want.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.auth import _apply_person_cap, access_for_scoped, narrower_role
from wavr.devices import DeviceStore
from wavr.space_store import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER, SpaceStore,
    device_role_for_person,
)

LOCAL = {"X-Wavr-Local": "1"}


# -- The ladder ---------------------------------------------------------------

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


@pytest.mark.parametrize("issued,person,expect", [
    ("central", "central", "central"),
    ("central", "user", "user"),         # demotion narrows
    ("central", "guest", "guest"),
    ("user", "central", "user"),         # promotion does NOT widen
    ("guest", "central", "guest"),
    ("user", "user", "user"),
])
def test_the_narrower_of_the_two_always_wins(issued, person, expect):
    assert narrower_role(issued, person) == expect


def test_a_role_outside_the_ladder_is_returned_untouched():
    # `agent` is a bounded machine credential, not a human's device. `root` is
    # synthesised for loopback and never persisted. Neither has a rung, and
    # silently rewriting a role this function does not understand would be worse
    # than leaving it alone.
    assert narrower_role("agent", "user") == "agent"
    assert narrower_role("root", "guest") == "root"
    assert narrower_role("central", "wizard") == "central"
    assert narrower_role("central", None) == "central"


# -- The cap ------------------------------------------------------------------

class _Dev:
    """The parts of `devices.Device` the auth path actually reads."""

    def __init__(self, role, person_id=None):
        self.role = role
        self.person_id = person_id
        self.scopes = None            # None = derive from role
        self.tool_scopes = None


def test_a_device_with_no_person_is_completely_unaffected():
    # Every device paired before this existed, every node, every agent.
    assert _apply_person_cap(_Dev("central"), lambda pid: "guest") == "central"


def test_no_resolver_injected_changes_nothing():
    assert _apply_person_cap(_Dev("central", "p1"), None) == "central"


def test_a_demoted_person_narrows_their_device():
    assert _apply_person_cap(_Dev("central", "p1"), lambda pid: "user") == "user"


def test_a_promoted_person_does_not_widen_their_device():
    # The important direction. A token issued as `user` stays `user` however
    # senior its owner becomes; widening requires a deliberate re-pair, so a
    # mis-click on the people screen cannot hand authority to credentials
    # already in the wild.
    assert _apply_person_cap(_Dev("user", "p1"), lambda pid: "central") == "user"


def test_a_dangling_association_denies_outright():
    # The person was removed. Falling back to the issued role would keep their
    # phone working; denying is the only fail-closed answer.
    assert _apply_person_cap(_Dev("central", "ghost"), lambda pid: None) is None


def test_an_agent_is_exempt_from_the_person_axis():
    assert _apply_person_cap(_Dev("agent", "p1"), lambda pid: "guest") == "agent"


# -- Through the real resolution path -----------------------------------------

class _Store:
    def __init__(self, device):
        self._device = device

    def verify(self, token):
        return self._device if token == "good" else None


def test_access_for_scoped_applies_the_cap():
    store = _Store(_Dev("central", "p1"))
    role, scopes, _ = access_for_scoped(
        "192.168.1.5", "192.168.1.1", "good", store,
        person_role_fn=lambda pid: "user")
    assert role == "user"
    # And the SCOPES follow the narrowed role, not the issued one.
    assert "admin" not in (scopes or frozenset())


def test_access_for_scoped_denies_a_dangling_association():
    store = _Store(_Dev("central", "ghost"))
    assert access_for_scoped("192.168.1.5", "192.168.1.1", "good", store,
                             person_role_fn=lambda pid: None) == (None, None, None)


def test_access_for_scoped_is_byte_identical_without_a_resolver():
    # The additive-only proof: every existing caller passes no resolver.
    store = _Store(_Dev("central"))
    assert access_for_scoped("192.168.1.5", "192.168.1.1", "good", store)[0] == "central"
    assert access_for_scoped("192.168.1.5", "192.168.1.1", "bad", store) == (None, None, None)


def test_loopback_still_short_circuits_before_any_person_lookup():
    def _explode(pid):
        raise AssertionError("loopback must never consult a person")

    assert access_for_scoped("127.0.0.1", "127.0.0.1", None, None,
                             person_role_fn=_explode) == ("root", None, None)


# -- Storage ------------------------------------------------------------------

@pytest.fixture
def devices():
    d = DeviceStore(":memory:")
    yield d
    d.close()


def test_person_id_round_trips_and_defaults_to_none(devices):
    did, token = devices.add("Sam's phone", "user")
    assert devices.verify(token).person_id is None
    assert devices.set_person(did, "p1") is True
    assert devices.verify(token).person_id == "p1"
    assert devices.get(did).person_id == "p1"
    assert devices.list()[0].person_id == "p1"


def test_setting_a_person_never_touches_the_credential(devices):
    did, token = devices.add("phone", "user", scopes=frozenset({"presence:read"}))
    before = devices.verify(token)
    devices.set_person(did, "p1")
    after = devices.verify(token)
    assert after.role == before.role
    assert after.scopes == before.scopes
    assert after.revoked == before.revoked


def test_clearing_the_person_restores_unnarrowed_behaviour(devices):
    did, token = devices.add("phone", "central")
    devices.set_person(did, "p1")
    devices.set_person(did, None)
    assert devices.verify(token).person_id is None


def test_the_migration_leaves_pre_existing_rows_alone(tmp_path):
    """A db written before `person_id` existed must gain the column with every
    row reading None — not a guessed owner."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE devices (
        device_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, created_ts TEXT NOT NULL,
        last_seen_ts TEXT, revoked INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT INTO devices VALUES ('d1','Old laptop','central',"
                 "'deadbeef','2026-01-01T00:00:00+00:00',NULL,0)")
    conn.commit()
    conn.close()

    store = DeviceStore(path)
    try:
        dev = store.get("d1")
        assert dev is not None and dev.role == "central"
        assert dev.person_id is None, "a pre-existing device's owner is unknown"
    finally:
        store.close()


# -- End to end through the HTTP surface --------------------------------------

def _multidevice_client(monkeypatch, tmp_path):
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.core_registry import CoreRegistry
    from wavr.discovery_inbox import DiscoveryInbox
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.settings_store import SettingsStore
    from wavr.storage import Storage

    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    space = SpaceStore(str(tmp_path / "wavr.db"))
    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=space, core_registry=CoreRegistry(":memory:"),
        settings_store=SettingsStore(":memory:"),
        discovery_inbox=DiscoveryInbox(":memory:"))
    return app, space


def test_pairing_derives_the_role_from_the_person(monkeypatch, tmp_path):
    app, space = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        space.create_space("My Home")
        owner = space.add_person("Alex", ROLE_OWNER)
        kid = space.add_person("Kid", ROLE_USER)

        # An Admin's device: asking for `central` on an Owner is allowed.
        r = c.post("/api/pair-code", headers=LOCAL,
                   json={"role": "central", "person_id": owner.person_id})
        assert r.status_code == 200 and r.json()["role"] == "central"

        # A User's device: asking for `central` is NARROWED, not honoured.
        r = c.post("/api/pair-code", headers=LOCAL,
                   json={"role": "central", "person_id": kid.person_id})
        assert r.status_code == 200
        assert r.json()["role"] == "user", "the person is a ceiling, not a floor"


def test_linking_a_person_in_the_ui_actually_caps_the_credential(
        monkeypatch, tmp_path):
    """The workflow an admin performs, end to end.

    Pair a device with no person (what the frontend does — it sends only
    `role`), then link it to someone by clicking their name, then demote them.
    The device must lose access on its very next request.

    This failed before: the associate route wrote the Space's own record and
    never `devices.person_id`, the column `auth._apply_person_cap` reads. So the
    Devices screen showed the phone as belonging to Sam while authorization had
    never heard of her, and demoting Sam changed nothing.
    """
    app, space = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        space.create_space("My Home")
        ana = space.add_person("Sam", ROLE_ADMIN)

        # Exactly what the frontend sends: a role, no person.
        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "central"}).json()["code"]
        peer = TestClient(app, client=("192.168.1.50", 12345))
        dev = peer.post("/api/pair", json={"code": code,
                                           "device_name": "ana-phone"}).json()
        auth = {"Authorization": f"Bearer {dev['token']}"}
        assert peer.get("/api/space/people", headers=auth).status_code == 200

        # The admin clicks Sam's name on the Devices screen.
        r = c.post(f"/api/space/devices/{dev['device_id']}/person",
                   headers=LOCAL, json={"person_id": ana.person_id})
        assert r.status_code == 200

        # Still an Admin, so nothing changes yet.
        assert peer.get("/api/space/people", headers=auth).status_code == 200

        # Demote her. No revoke, no re-pair.
        space.set_person_role(ana.person_id, ROLE_GUEST)
        assert peer.get("/api/space/people", headers=auth).status_code == 403, (
            "a demotion must land on the very next request (ADR-0011)")


def test_unlinking_a_person_releases_the_cap(monkeypatch, tmp_path):
    """Unbinding must clear BOTH records, or the device stays capped by
    somebody the Space no longer says owns it."""
    app, space = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        space.create_space("My Home")
        kid = space.add_person("Kid", ROLE_USER)

        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "central"}).json()["code"]
        peer = TestClient(app, client=("192.168.1.50", 12345))
        dev = peer.post("/api/pair", json={"code": code,
                                           "device_name": "shared-tablet"}).json()
        auth = {"Authorization": f"Bearer {dev['token']}"}

        c.post(f"/api/space/devices/{dev['device_id']}/person",
               headers=LOCAL, json={"person_id": kid.person_id})
        # Capped down to `user`, so admin routes are refused.
        assert peer.get("/api/space/people", headers=auth).status_code == 403

        c.post(f"/api/space/devices/{dev['device_id']}/person",
               headers=LOCAL, json={"person_id": None})
        assert peer.get("/api/space/people", headers=auth).status_code == 200, (
            "with no person, the credential resolves at its issued role again")


def test_pairing_for_an_unknown_person_is_refused(monkeypatch, tmp_path):
    app, _ = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/pair-code", headers=LOCAL,
                      json={"person_id": "ghost"}).status_code == 404


def test_a_guest_cannot_be_paired_into_a_never_expiring_credential(
        monkeypatch, tmp_path):
    app, space = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        space.create_space("My Home")
        guest = space.add_person("Visitor", ROLE_GUEST)
        r = c.post("/api/pair-code", headers=LOCAL,
                   json={"role": "user", "person_id": guest.person_id})
        assert r.status_code == 400
        assert "guest/invite" in r.json()["detail"]


def test_redeeming_a_person_code_associates_the_device(monkeypatch, tmp_path):
    app, space = _multidevice_client(monkeypatch, tmp_path)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        space.create_space("My Home")
        ana = space.add_person("Sam", ROLE_ADMIN)
        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "central",
                            "person_id": ana.person_id}).json()["code"]
        paired = c.post("/api/pair", json={"code": code,
                                           "device_name": "Sam's phone"})
        assert paired.status_code == 200
        device_id = paired.json()["device_id"]

        # Inside the context: the app's lifespan closes these stores on exit.
        # Both sides of the association were written -- the credential knows its
        # person (that is what the auth cap reads) and the Space knows the device.
        assert space.person_of_device(device_id) == (ana.person_id, "confirmed")


def test_device_role_for_person_is_now_actually_load_bearing():
    # It used to feed only a UI preview. If this ever stops being consulted by
    # the auth path, the whole person axis becomes decorative again.
    import inspect

    from wavr import app as app_module

    src = inspect.getsource(app_module.create_app)
    assert "device_role_for_person" in src
    assert "person_role_fn=_person_role" in src
    assert device_role_for_person(ROLE_ADMIN) == "central"
