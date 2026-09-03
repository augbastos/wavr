"""Space, people and device functions — the three-axis separation, and the
invariants that keep it from becoming a privilege bug."""
import pytest

from wavr.capabilities import CapabilityManifest
from wavr.space_store import (
    CAP_CONTEXT_VIEW, CAP_PEOPLE_MANAGE, CAP_SPACE_TRANSFER, PERSON_CAPABILITIES,
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER, SpaceError, SpaceStore,
    capabilities_for, device_role_for_person, has_capability,
)


@pytest.fixture
def store():
    s = SpaceStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def space(store):
    store.create_space("My Home", "home")
    return store


# -- Space -------------------------------------------------------------------

def test_create_and_read_back(store):
    sp = store.create_space("My Home", "home")
    assert sp.name == "My Home" and sp.kind == "home" and sp.epoch == 1
    assert store.get_space().space_id == sp.space_id


def test_a_core_serves_exactly_one_space(space):
    with pytest.raises(SpaceError, match="already belongs"):
        space.create_space("Second Home")


def test_unknown_kind_degrades_to_other_rather_than_failing(store):
    assert store.create_space("Lab", "submarine").kind == "other"


def test_blank_name_is_rejected(store):
    with pytest.raises(SpaceError):
        store.create_space("   ")


def test_join_reuses_the_other_cores_space_id(store):
    sp = store.create_space("My Home", "home", space_id="abcdef0123456789")
    assert sp.space_id == "abcdef0123456789"


def test_malformed_space_id_is_rejected(store):
    with pytest.raises(SpaceError):
        store.create_space("My Home", space_id="../../etc/passwd")


def test_epoch_is_monotonic(space):
    assert [space.bump_epoch() for _ in range(3)] == [2, 3, 4]
    assert space.get_space().epoch == 4


def test_policy_is_bounded(space):
    with pytest.raises(SpaceError):
        space.set_policy({"blob": "x" * 20000})
    with pytest.raises(SpaceError):
        space.set_policy(["not", "an", "object"])


# -- Capability resolution ---------------------------------------------------

def test_role_capability_defaults():
    assert CAP_SPACE_TRANSFER in capabilities_for(ROLE_OWNER)
    # The single thing that separates Owner from Admin.
    assert CAP_SPACE_TRANSFER not in capabilities_for(ROLE_ADMIN)
    assert CAP_PEOPLE_MANAGE in capabilities_for(ROLE_ADMIN)
    assert capabilities_for(ROLE_USER) == frozenset({CAP_CONTEXT_VIEW, "control:use"})
    assert capabilities_for(ROLE_GUEST) == frozenset()


def test_unknown_role_fails_closed():
    assert capabilities_for("wizard") == frozenset()
    assert not has_capability(capabilities_for("wizard"), CAP_CONTEXT_VIEW)


def test_explicit_empty_grant_beats_the_role_default():
    # Mirrors auth.effective_scopes: an EXPLICIT empty set is a real deny, not
    # "unset". Getting this backwards would silently re-widen a narrowed person.
    assert capabilities_for(ROLE_OWNER, frozenset()) == frozenset()
    assert capabilities_for(ROLE_OWNER, None) == capabilities_for(ROLE_OWNER)


def test_has_capability_on_none_is_false():
    assert has_capability(None, CAP_CONTEXT_VIEW) is False


# -- The bridge to the existing auth model -----------------------------------

def test_person_role_maps_onto_existing_device_roles():
    from wavr.devices import VALID_ROLES

    for role in (ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST):
        assert device_role_for_person(role) in VALID_ROLES


def test_owner_and_admin_pair_as_central_user_as_user():
    assert device_role_for_person(ROLE_OWNER) == "central"
    assert device_role_for_person(ROLE_ADMIN) == "central"
    assert device_role_for_person(ROLE_USER) == "user"
    assert device_role_for_person(ROLE_GUEST) == "guest"


def test_unknown_person_role_pairs_as_the_weakest_credential():
    assert device_role_for_person("facilities") == "guest"


# -- People ------------------------------------------------------------------

def test_add_and_list(space):
    space.add_person("Augusto", ROLE_OWNER)
    space.add_person("Ana", ROLE_ADMIN)
    assert [p.display_name for p in space.list_people()] == ["Augusto", "Ana"]


def test_exactly_one_owner(space):
    space.add_person("Augusto", ROLE_OWNER)
    with pytest.raises(SpaceError, match="already has an Owner"):
        space.add_person("Impostor", ROLE_OWNER)


def test_owner_cannot_be_demoted_or_removed(space):
    owner = space.add_person("Augusto", ROLE_OWNER)
    with pytest.raises(SpaceError, match="transfer"):
        space.set_person_role(owner.person_id, ROLE_USER)
    with pytest.raises(SpaceError, match="transfer"):
        space.remove_person(owner.person_id)


def test_cannot_promote_to_owner_by_role_change(space):
    space.add_person("Augusto", ROLE_OWNER)
    ana = space.add_person("Ana", ROLE_USER)
    with pytest.raises(SpaceError, match="transfer_ownership"):
        space.set_person_role(ana.person_id, ROLE_OWNER)


def test_transfer_is_atomic_and_leaves_exactly_one_owner(space):
    aug = space.add_person("Augusto", ROLE_OWNER)
    ana = space.add_person("Ana", ROLE_ADMIN)
    new, prev = space.transfer_ownership(ana.person_id)
    assert new.person_id == ana.person_id and new.role == ROLE_OWNER
    assert prev.person_id == aug.person_id and prev.role == ROLE_ADMIN
    owners = [p for p in space.list_people() if p.role == ROLE_OWNER]
    assert len(owners) == 1


def test_transfer_to_unknown_person_fails(space):
    space.add_person("Augusto", ROLE_OWNER)
    with pytest.raises(SpaceError):
        space.transfer_ownership("nope")


def test_unknown_capability_in_explicit_grant_is_rejected(space):
    with pytest.raises(SpaceError, match="unknown capabilities"):
        space.add_person("Ana", ROLE_USER, capabilities=frozenset({"do:anything"}))


def test_person_dict_resolves_capabilities_for_the_caller(space):
    p = space.add_person("Ana", ROLE_ADMIN)
    assert set(p.to_dict()["capabilities"]) == set(capabilities_for(ROLE_ADMIN))
    assert set(p.to_dict()["capabilities"]) <= PERSON_CAPABILITIES


def test_guest_expiry_is_reported(space):
    p = space.add_person("Visitor", ROLE_GUEST, expires_at="2000-01-01T00:00:00+00:00")
    assert p.to_dict()["expired"] is True


def test_malformed_expiry_fails_closed_as_expired(space):
    p = space.add_person("Visitor", ROLE_GUEST, expires_at="not-a-date")
    assert p.to_dict()["expired"] is True


def test_people_are_capped(space):
    space.add_person("Owner", ROLE_OWNER)
    for i in range(199):
        space.add_person(f"P{i}", ROLE_USER)
    with pytest.raises(SpaceError, match="limit"):
        space.add_person("One too many", ROLE_USER)


# -- Personalization must never touch authorization --------------------------

def test_profile_cannot_widen_capabilities(space):
    ana = space.add_person("Ana", ROLE_USER)
    space.set_person_profile(ana.person_id, {
        "language": "pt-BR", "role": "owner",
        "capabilities": ["space:transfer"], "admin": True})
    after = space.get_person(ana.person_id)
    assert after.role == ROLE_USER
    assert CAP_SPACE_TRANSFER not in capabilities_for(after.role, after.capabilities)


def test_profile_is_bounded(space):
    ana = space.add_person("Ana", ROLE_USER)
    with pytest.raises(SpaceError):
        space.set_person_profile(ana.person_id, {"x": "y" * 8000})


# -- Person <-> device -------------------------------------------------------

def test_associate_and_look_up_both_ways(space):
    ana = space.add_person("Ana", ROLE_USER)
    space.associate_device("dev1", ana.person_id)
    space.associate_device("dev2", ana.person_id)
    assert space.person_of_device("dev1") == (ana.person_id, "confirmed")
    assert space.devices_of(ana.person_id) == ["dev1", "dev2"]


def test_inferred_origin_is_kept_distinct_from_confirmed(space):
    ana = space.add_person("Ana", ROLE_USER)
    space.associate_device("maybe", ana.person_id, origin="inferred")
    assert space.person_of_device("maybe")[1] == "inferred"


def test_bad_origin_is_rejected(space):
    ana = space.add_person("Ana", ROLE_USER)
    with pytest.raises(SpaceError):
        space.associate_device("d", ana.person_id, origin="probably")


def test_cannot_associate_with_an_unknown_person(space):
    with pytest.raises(SpaceError):
        space.associate_device("d", "ghost")


def test_reassociating_moves_the_device(space):
    a = space.add_person("A", ROLE_USER)
    b = space.add_person("B", ROLE_USER)
    space.associate_device("d", a.person_id)
    space.associate_device("d", b.person_id)
    assert space.devices_of(a.person_id) == []
    assert space.devices_of(b.person_id) == ["d"]


def test_removing_a_person_returns_their_devices_for_revocation(space):
    space.add_person("Owner", ROLE_OWNER)
    ana = space.add_person("Ana", ROLE_USER)
    space.associate_device("phone", ana.person_id)
    assert space.remove_person(ana.person_id) == ["phone"]
    assert space.person_of_device("phone") is None
    assert [p.display_name for p in space.list_people()] == ["Owner"]


# -- Device functions --------------------------------------------------------

def test_a_device_can_be_core_node_and_client_at_once(space):
    f = space.set_functions("laptop", ["core", "node", "client"], platform="windows")
    assert f.functions == frozenset({"core", "node", "client"})
    assert space.get_functions("laptop").platform == "windows"


def test_unknown_function_is_rejected(space):
    with pytest.raises(SpaceError, match="unknown device functions"):
        space.set_functions("x", ["core", "toaster"])


def test_functions_are_replaced_not_merged(space):
    space.set_functions("d", ["core", "node"])
    assert space.set_functions("d", ["client"]).functions == frozenset({"client"})


def test_portable_and_room_round_trip(space):
    space.set_functions("moto", ["core"], room="Kitchen", portable=True)
    got = space.get_functions("moto")
    assert got.portable is True and got.room == "Kitchen"


# -- Manifests ---------------------------------------------------------------

def test_manifest_is_parsed_before_storage(space):
    # An off-spec claim from a LAN device must never land in the db verbatim.
    space.set_manifest("d", {"platform": "solaris", "capabilities": {"wifi": "yes"},
                             "functions_supported": ["core", "toaster"]})
    m = space.get_manifest("d")
    assert m.platform == "unknown"
    assert m.capability("wifi") is None
    assert m.functions_supported == ("core",)


def test_manifest_accepts_a_dataclass_too(space):
    space.set_manifest("d", CapabilityManifest(platform="linux", ram_mb=2048))
    assert space.get_manifest("d").ram_mb == 2048


def test_missing_manifest_is_none(space):
    assert space.get_manifest("never-seen") is None


def test_forget_device_clears_every_axis(space):
    ana = space.add_person("Ana", ROLE_USER)
    space.associate_device("d", ana.person_id)
    space.set_functions("d", ["client"])
    space.set_manifest("d", {"platform": "linux"})
    space.forget_device("d")
    assert space.person_of_device("d") is None
    assert space.get_functions("d") is None
    assert space.get_manifest("d") is None


# -- Rollback of a half-created Space ----------------------------------------

def test_destroy_space_unwinds_a_setup_that_never_finished(space):
    space.add_person("Augusto", ROLE_OWNER)
    assert space.destroy_space() is True
    assert space.get_space() is None
    assert space.list_people() == []
    # And it is idempotent: nothing left to unwind.
    assert space.destroy_space() is False


def test_destroy_space_refuses_once_the_space_is_real(space):
    space.add_person("Augusto", ROLE_OWNER)
    space.set_functions("laptop", ["core"])
    with pytest.raises(SpaceError, match="refusing to delete"):
        space.destroy_space()
    assert space.get_space() is not None


def test_destroy_space_refuses_with_a_second_person(space):
    space.add_person("Augusto", ROLE_OWNER)
    space.add_person("Ana", ROLE_ADMIN)
    with pytest.raises(SpaceError, match="refusing to delete"):
        space.destroy_space()


# -- Legacy adoption ---------------------------------------------------------

def test_adopt_legacy_creates_a_space_and_maps_central_devices(store):
    rows = [{"device_id": "d-central", "role": "central"},
            {"device_id": "d-user", "role": "user"},
            {"device_id": "d-agent", "role": "agent"}]
    sp = store.adopt_legacy(rows, owner_name="Augusto", space_name="My Home")
    assert sp.name == "My Home"
    owner = store.list_people()[0]
    assert owner.role == ROLE_OWNER and owner.display_name == "Augusto"
    # The central device becomes the Owner's...
    assert store.person_of_device("d-central") == (owner.person_id, "confirmed")
    # ...and the others are deliberately left unclaimed rather than guessed at.
    assert store.person_of_device("d-user") is None
    assert store.person_of_device("d-agent") is None


def test_adopt_legacy_is_idempotent(store):
    first = store.adopt_legacy([{"device_id": "a", "role": "central"}])
    second = store.adopt_legacy([{"device_id": "b", "role": "central"}])
    assert first.space_id == second.space_id
    assert len(store.list_people()) == 1


def test_adopt_legacy_tolerates_junk_rows(store):
    store.adopt_legacy([{}, {"role": "central"}, None, {"device_id": "ok", "role": "central"}])
    assert store.person_of_device("ok") is not None
