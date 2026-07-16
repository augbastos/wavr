"""Wavr Pass Phase 1 -- local sovereign authorization framework (design spec
2026-07-07-wavr-pass-design.md). Covers the three files the design touches:

  * wavr.devices  -- the nullable `scopes` column, its PRAGMA-guarded additive
    migration (idempotent across repeated `DeviceStore(path)` inits on the SAME
    file), and add()/verify()/list()/get() round-tripping it.
  * wavr.auth     -- SCOPES, DEFAULT_SCOPES, effective_scopes(), has_scope(),
    access_for() -- the pure backward-compat proof: a NULL `Device.scopes`
    resolves to EXACTLY the role's existing tier (can_view/can_change_state/
    require_central), for both grantable roles.
  * wavr.app      -- require_scope() (additive, root-bypassing, fail-closed on
    a missing scope) wired onto the real app, including the one negative-space
    invariant from the design's Verdict: /api/block carries NO scope mapping at
    all, so even a device granted EVERY scope in the taxonomy still can't
    reach it (require_root is the only gate).

`authorize()` itself is untouched by this feature (still covered by its own
tests in test_multidevice.py) -- only `access_for` (the new one-verify ->
(role, scopes) sibling) is exercised here.

Phase 2A / B4 (below, its own section) adds the AGENT principal type + per-tool
MCP scopes: `wavr.devices`'s nullable `tool_scopes` column (same idiom as
`scopes`), `wavr.auth`'s `effective_tool_scopes()`/`tool_call_allowed()`/
`access_for_scoped()` (a NEW three-tuple sibling of `access_for` -- `access_for`
itself stays untouched, same precedent as `authorize()` above), and the app.py
route-level bound: 'agent' is absent from both `can_view`/`can_change_state`
role tuples, so it gets NOTHING from the ordinary HTTP API -- only `/mcp`,
further bounded there by its tool-name allow-list (see test_mcp_http.py for the
`/mcp` gate-4.5 enforcement itself).
"""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.auth import (
    SCOPES, DEFAULT_SCOPES, access_for, effective_scopes, has_scope,
    AGENT_READ_TOOL_SCOPE, AGENT_ACTUATOR_TOOL_SCOPE, AGENT_DEFAULT_TOOL_SCOPE,
    MCP_TOOL_NAMES, DEFAULT_AGENT_TOOL_SCOPES, access_for_scoped,
    effective_tool_scopes, tool_call_allowed,
)
from wavr.devices import DeviceStore
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}
LOCAL_IP = "192.168.1.10"


def _store(tmp_path, name="d.db"):
    return DeviceStore(str(tmp_path / name))


# --------------------------------------------------------------------------- #
# devices.py: nullable `scopes` column -- add()/verify()/list()/get() round trip.
# --------------------------------------------------------------------------- #
def test_add_default_scopes_is_null(tmp_path):
    # Every EXISTING caller of add(name, role) (pairing.py, every test fixture)
    # keeps calling it with exactly two positional args -- the backward-compat
    # lever is that this still works and yields scopes=None.
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    dev = store.verify(token)
    assert dev.scopes is None


def test_add_explicit_scopes_round_trips_through_verify_list_get(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("phone", "user", scopes=frozenset({"presence:read", "camera:view"}))
    assert store.verify(token).scopes == frozenset({"presence:read", "camera:view"})
    assert store.get(device_id).scopes == frozenset({"presence:read", "camera:view"})
    listed = {d.device_id: d for d in store.list()}
    assert listed[device_id].scopes == frozenset({"presence:read", "camera:view"})


def test_add_explicit_empty_scopes_is_distinct_from_null(tmp_path):
    # An explicit EMPTY grant (deny-all) must NOT be confused with NULL
    # ("derive from role") -- the whole point of the NULL/non-NULL distinction.
    store = _store(tmp_path)
    _id, token = store.add("phone", "central", scopes=frozenset())
    dev = store.verify(token)
    assert dev.scopes == frozenset()
    assert dev.scopes is not None


# --------------------------------------------------------------------------- #
# devices.py: PRAGMA-guarded migration, idempotent across repeated inits.
# --------------------------------------------------------------------------- #
def test_migration_idempotent_across_repeated_init_same_file(tmp_path):
    path = str(tmp_path / "mig.db")
    store1 = DeviceStore(path)
    device_id, token = store1.add("phone", "user")
    store1.close()

    # Re-init on the SAME db file: the scopes column already exists -- the
    # PRAGMA-guarded ALTER must be a no-op (never a duplicate-column error).
    store2 = DeviceStore(path)
    # ...and again (init a THIRD time on the same file) -- still a no-op.
    store3 = DeviceStore(path)
    cols = [r["name"] for r in store3._conn.execute("PRAGMA table_info(devices)")]
    assert cols.count("scopes") == 1                    # column added exactly once

    # The pre-existing row (created before this feature/this second init) still
    # verifies, and its NULL scopes still reads back as None -- the migration
    # never touched existing data.
    dev = store2.verify(token)
    assert dev is not None and dev.device_id == device_id and dev.scopes is None
    store2.close()
    store3.close()


def test_migration_adds_column_to_a_pre_wavr_pass_devices_table(tmp_path):
    # Simulate a devices table that predates this column entirely (hand-rolled
    # schema, no `scopes`), seeded with a row -- as if the app had been running
    # Phase-0 multidevice for a while. DeviceStore must add the column AND keep
    # reading the old row (scopes -> None) without any migration step from the
    # operator.
    import sqlite3
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, created_ts TEXT NOT NULL,
            last_seen_ts TEXT, revoked INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO devices VALUES (?, ?, ?, ?, ?, NULL, 0)",
        ("dev-1", "old-phone", "user", "deadbeef" * 4, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = DeviceStore(path)
    cols = [r["name"] for r in store._conn.execute("PRAGMA table_info(devices)")]
    assert "scopes" in cols
    dev = store.get("dev-1")
    assert dev is not None and dev.scopes is None       # pre-existing row, column backfilled NULL


# --------------------------------------------------------------------------- #
# auth.py: the executable backward-compat proof -- NULL scopes -> role default,
# for BOTH grantable roles, full allow/deny matrix over every named scope.
# --------------------------------------------------------------------------- #
def test_null_scopes_resolve_to_role_default_full_matrix():
    for role in ("central", "user"):
        resolved = effective_scopes(role, None)
        for scope in SCOPES:
            expected = scope in DEFAULT_SCOPES[role]
            assert has_scope(resolved, scope) is expected, (role, scope, expected)


def test_root_default_scopes_contain_every_scope():
    # root -> ALL (sentinel): never scope-limited, documented via DEFAULT_SCOPES
    # even though the runtime path (access_for) short-circuits before reaching
    # effective_scopes for loopback at all.
    resolved = effective_scopes("root", None)
    for scope in SCOPES:
        assert has_scope(resolved, scope) is True


def test_explicit_scopes_override_the_role_default():
    # A non-None (even empty) explicit grant always wins over DEFAULT_SCOPES --
    # this is the P2 consent lever this Phase only wires the plumbing for.
    assert effective_scopes("central", frozenset()) == frozenset()
    assert effective_scopes("user", frozenset({"admin"})) == frozenset({"admin"})


def test_has_scope_fails_closed_on_none():
    assert has_scope(None, "presence:read") is False


# --------------------------------------------------------------------------- #
# auth.access_for: one verify -> (role, scopes). Mirrors authorize()'s own
# decision (authorize is untested here -- see test_multidevice.py) but ALSO
# resolves scopes in the same pass.
# --------------------------------------------------------------------------- #
def test_access_for_loopback_is_root_with_none_scopes(tmp_path):
    store = _store(tmp_path)
    for host in ("127.0.0.1", "::1", "testclient"):
        assert access_for(host, LOCAL_IP, None, store) == ("root", None)


def test_access_for_null_device_scopes_resolves_to_user_default(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    role, scopes = access_for("192.168.1.55", LOCAL_IP, token, store)
    assert role == "user"
    assert scopes == DEFAULT_SCOPES["user"]


def test_access_for_null_device_scopes_resolves_to_central_default(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("peer-pc", "central")
    role, scopes = access_for("192.168.1.55", LOCAL_IP, token, store)
    assert role == "central"
    assert scopes == DEFAULT_SCOPES["central"]


def test_access_for_explicit_device_scopes_override_default(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user", scopes=frozenset({"presence:read"}))
    role, scopes = access_for("192.168.1.55", LOCAL_IP, token, store)
    assert role == "user" and scopes == frozenset({"presence:read"})


def test_access_for_denies_before_any_scope_talk_off_subnet(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    # Off-subnet: denied before device_store.verify is ever called (mirrors
    # authorize's own subnet-first check) -- (None, None), never a scope leak.
    assert access_for("10.0.0.5", LOCAL_IP, token, store) == (None, None)


def test_access_for_unknown_token_denies(tmp_path):
    store = _store(tmp_path)
    assert access_for("192.168.1.55", LOCAL_IP, "garbage", store) == (None, None)


def test_access_for_revoked_token_denies(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("phone", "user")
    store.revoke(device_id)
    assert access_for("192.168.1.55", LOCAL_IP, token, store) == (None, None)


# --------------------------------------------------------------------------- #
# app.py wiring: require_scope on the REAL app -- root bypass, a missing-scope
# 403, and the single most important negative: /api/block carries NO scope.
# --------------------------------------------------------------------------- #
def _md_app(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"), **extra)


def _pair(app, role):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_root_bypasses_require_scope(tmp_path, monkeypatch):
    # Loopback root's request.state.scopes is None (no Device row to read an
    # explicit grant from) -- yet an admin-scoped route (POST /api/core/pin,
    # additive require_scope("admin") on top of require_local) still succeeds:
    # proof require_scope short-circuits on role == "root" before ever
    # consulting `scopes`.
    app = _md_app(tmp_path, monkeypatch)
    root = TestClient(app, headers=CSRF)
    assert root.post("/api/core/pin", json={"pin": "1234"}).status_code == 200


def test_require_scope_denies_an_explicit_missing_scope(tmp_path, monkeypatch):
    # Pairing (Phase 1) never grants explicit scopes -- every paired device is
    # NULL -> role default. To prove require_scope is a REAL, independent gate
    # (not just an echo of require_local/require_central), pre-seed a 'central'
    # device with an EXPLICIT empty grant (bypassing pairing) directly via
    # DeviceStore.add(scopes=...): its ROLE gate (require_local's
    # can_change_state) would ALLOW this route; only require_scope denies it.
    db_path = str(tmp_path / "md.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("scoped-central", "central", scopes=frozenset())
    seed_store.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {token}"}
    r = peer.post("/api/system/toggle", json={"on": True}, headers=auth)
    assert r.status_code == 403
    assert r.json()["detail"] == "missing scope: control"


def test_block_carries_no_scope_mapping_even_with_every_scope_granted(tmp_path, monkeypatch):
    # Verdict condition (3): /api/block must carry NO scope mapping. Prove it
    # in the strongest way available -- grant a 'central' device EVERY scope in
    # the taxonomy (as generous as Wavr Pass can express) and show it STILL
    # can't reach /api/block. Only require_root gates this route; no
    # require_scope exists on it at all for a grant to satisfy.
    db_path = str(tmp_path / "md.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NET_BLOCKING", "1")     # flag ON -- a 403 here can't be a masked 503
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("super-central", "central", scopes=frozenset(SCOPES))
    seed_store.close()
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"),
                     arp_send=lambda *a, **k: None)
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {token}"}
    assert peer.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:01", "confirm": True},
                     headers=auth).status_code == 403
    assert peer.get("/api/block", headers=auth).status_code == 403
    # Sanity: this same over-privileged central genuinely CAN reach a
    # control-scoped route -- so the /api/block denial isn't just "everything
    # 403s for this device".
    assert peer.post("/api/system/toggle", json={"on": False}, headers=auth).status_code == 200


def test_null_scope_user_and_central_route_parity_end_to_end(tmp_path, monkeypatch):
    # The end-to-end mirror of test_null_scopes_resolve_to_role_default_full_matrix,
    # run through the REAL middleware + require_scope wiring on one representative
    # route per scope tier -- the backward-compat merge gate.
    app = _md_app(tmp_path, monkeypatch)
    user_peer, user_auth = _pair(app, "user")
    central_peer, central_auth = _pair(app, "central")

    # presence:read -- in both defaults.
    assert user_peer.get("/api/state", headers=user_auth).status_code == 200
    assert central_peer.get("/api/state", headers=central_auth).status_code == 200

    # network:read -- in both defaults.
    assert user_peer.get("/api/inventory", headers=user_auth).status_code == 200
    assert central_peer.get("/api/inventory", headers=central_auth).status_code == 200

    # camera:view -- in both defaults.
    assert user_peer.get("/api/cameras", headers=user_auth).status_code == 200
    assert central_peer.get("/api/cameras", headers=central_auth).status_code == 200

    # control -- user default lacks it; central default has it.
    assert user_peer.post("/api/system/toggle", json={"on": True},
                          headers=user_auth).status_code == 403
    assert central_peer.post("/api/system/toggle", json={"on": True},
                             headers=central_auth).status_code == 200

    # admin -- user default lacks it; central default has it.
    assert user_peer.get("/api/devices", headers=user_auth).status_code == 403
    assert central_peer.get("/api/devices", headers=central_auth).status_code == 200


# =============================================================================
# Wavr Pass (Phase 2A / B4) -- the AGENT principal type + per-tool MCP scopes.
# Covers wavr.devices's `tool_scopes` column, wavr.auth's tool-scope resolution
# functions, and app.py's route-level bound for the new role. The `/mcp`
# gate-4.5 enforcement itself (the actual tools/call refusal) is covered in
# test_mcp_http.py, which owns the real MCP-session integration harness.
# =============================================================================

# --------------------------------------------------------------------------- #
# devices.py: nullable `tool_scopes` column -- add()/verify()/list()/get() round
# trip, mirroring the `scopes` column's own tests above exactly.
# --------------------------------------------------------------------------- #
def test_add_default_tool_scopes_is_null(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("mcp-agent", "agent")
    assert store.verify(token).tool_scopes is None


def test_add_explicit_tool_scopes_round_trips_through_verify_list_get(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("mcp-agent", "agent",
                                 tool_scopes=frozenset({"list_rooms", "get_alerts"}))
    assert store.verify(token).tool_scopes == frozenset({"list_rooms", "get_alerts"})
    assert store.get(device_id).tool_scopes == frozenset({"list_rooms", "get_alerts"})
    listed = {d.device_id: d for d in store.list()}
    assert listed[device_id].tool_scopes == frozenset({"list_rooms", "get_alerts"})


def test_add_explicit_empty_tool_scopes_is_distinct_from_null(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("mcp-agent", "agent", tool_scopes=frozenset())
    dev = store.verify(token)
    assert dev.tool_scopes == frozenset()
    assert dev.tool_scopes is not None


def test_valid_roles_includes_agent():
    from wavr.devices import VALID_ROLES
    assert "agent" in VALID_ROLES


def test_tool_scopes_migration_idempotent_across_repeated_init_same_file(tmp_path):
    # Mirrors test_migration_idempotent_across_repeated_init_same_file (Phase 1,
    # for `scopes`) exactly, for the NEW `tool_scopes` column: re-init on the
    # SAME db file must be a no-op (never a duplicate-column error), and a
    # pre-existing row's NULL tool_scopes must still read back as None.
    path = str(tmp_path / "mig.db")
    store1 = DeviceStore(path)
    device_id, token = store1.add("mcp-agent", "agent")
    store1.close()

    store2 = DeviceStore(path)
    store3 = DeviceStore(path)
    cols = [r["name"] for r in store3._conn.execute("PRAGMA table_info(devices)")]
    assert cols.count("tool_scopes") == 1

    dev = store2.verify(token)
    assert dev is not None and dev.device_id == device_id and dev.tool_scopes is None
    store2.close()
    store3.close()


# --------------------------------------------------------------------------- #
# auth.py: effective_tool_scopes / tool_call_allowed -- pure logic, no I/O.
# --------------------------------------------------------------------------- #
def test_effective_tool_scopes_non_agent_roles_are_unrestricted():
    # The tool-name axis doesn't apply to root/central/user at all -- None
    # ("not restricted"), even if a device somehow carried an explicit grant.
    for role in ("root", "central", "user", None):
        assert effective_tool_scopes(role, None) is None
        assert effective_tool_scopes(role, frozenset({"list_rooms"})) is None


def test_effective_tool_scopes_agent_null_resolves_to_coarse_default():
    # Phase-2A verify FIX 4 (MEDIUM, least-privilege default): the DEFAULT agent
    # grant is the COARSE, current-state-only set -- NOT the full read set.
    resolved = effective_tool_scopes("agent", None)
    assert resolved == AGENT_DEFAULT_TOOL_SCOPE
    assert resolved == DEFAULT_AGENT_TOOL_SCOPES["agent"]
    assert resolved == frozenset({
        "list_rooms", "get_room_context", "get_house_status"})
    assert "call_ha_service" not in resolved       # actuation opt-in, never default
    # The household PII/tracking crown jewels are opt-in ONLY -- excluded from
    # the default even though get_network_inventory/get_alerts/
    # query_occupancy_history are minimized (FIX 1/2/3) and get_ha_entities
    # never was (HA friendly_name can name a person or a device).
    # Phase-2B re-threat FIX 1 (MEDIUM): get_house_map joins that excluded set
    # too -- its room `id` encodes the room name and it ships polygon geometry
    # (the floor plan itself), which a coarse cloud/default agent doesn't need.
    for excluded in ("get_alerts", "get_network_inventory",
                     "query_occupancy_history", "get_ha_entities", "get_house_map"):
        assert excluded not in resolved


def test_agent_default_tool_scope_is_a_strict_subset_of_read_scope():
    # AGENT_READ_TOOL_SCOPE (every read tool) stays available as the name an
    # admin grants EXPLICITLY to widen an agent -- it is no longer the default.
    assert AGENT_DEFAULT_TOOL_SCOPE < AGENT_READ_TOOL_SCOPE
    assert AGENT_DEFAULT_TOOL_SCOPE != AGENT_READ_TOOL_SCOPE


def test_agent_actuator_scope_is_read_scope_plus_call_ha_service():
    assert AGENT_ACTUATOR_TOOL_SCOPE == AGENT_READ_TOOL_SCOPE | {"call_ha_service"}
    assert AGENT_ACTUATOR_TOOL_SCOPE == MCP_TOOL_NAMES


def test_effective_tool_scopes_agent_explicit_overrides_default():
    assert effective_tool_scopes("agent", frozenset()) == frozenset()
    assert effective_tool_scopes("agent", frozenset({"call_ha_service"})) == \
        frozenset({"call_ha_service"})


def test_tool_call_allowed_none_means_unrestricted():
    assert tool_call_allowed(None, "call_ha_service") is True
    assert tool_call_allowed(None, "anything_at_all") is True


def test_tool_call_allowed_fails_closed_on_an_explicit_set():
    assert tool_call_allowed(frozenset({"list_rooms"}), "list_rooms") is True
    assert tool_call_allowed(frozenset({"list_rooms"}), "call_ha_service") is False
    assert tool_call_allowed(frozenset(), "list_rooms") is False   # empty = deny-all


# --------------------------------------------------------------------------- #
# auth.access_for_scoped: the three-tuple sibling of access_for -- root/central/
# user get byte-identical (role, scopes) plus a new `tool_scopes=None`; 'agent'
# additionally resolves its tool-name allow-list in the SAME one-verify pass.
# --------------------------------------------------------------------------- #
def test_access_for_scoped_loopback_is_root_with_none_everything(tmp_path):
    store = _store(tmp_path)
    assert access_for_scoped("127.0.0.1", LOCAL_IP, None, store) == ("root", None, None)


def test_access_for_scoped_matches_access_for_for_central_and_user(tmp_path):
    store = _store(tmp_path)
    _id_c, tok_c = store.add("central-dev", "central")
    _id_u, tok_u = store.add("user-dev", "user")
    for token in (tok_c, tok_u):
        role2, scopes2 = access_for("192.168.1.55", LOCAL_IP, token, store)
        role3, scopes3, tool_scopes3 = access_for_scoped(
            "192.168.1.55", LOCAL_IP, token, store)
        assert (role3, scopes3) == (role2, scopes2)
        assert tool_scopes3 is None      # unrestricted by the tool-name axis


def test_access_for_scoped_agent_resolves_tool_scopes(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("mcp-agent", "agent")
    role, scopes, tool_scopes = access_for_scoped("192.168.1.55", LOCAL_IP, token, store)
    assert role == "agent"
    assert scopes == DEFAULT_SCOPES["agent"]
    # Phase-2A verify FIX 4: NULL tool_scopes resolves to the COARSE default,
    # not the full read set (that now requires an explicit grant -- see
    # test_agent_explicit_broad_grant_can_still_reach_all_read_tools_live in
    # test_mcp_http.py).
    assert tool_scopes == AGENT_DEFAULT_TOOL_SCOPE


def test_access_for_scoped_agent_explicit_tool_scopes_override_default(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("mcp-agent", "agent", tool_scopes=frozenset({"list_rooms"}))
    _role, _scopes, tool_scopes = access_for_scoped(
        "192.168.1.55", LOCAL_IP, token, store)
    assert tool_scopes == frozenset({"list_rooms"})


def test_access_for_scoped_denies_before_any_scope_talk_off_subnet(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("mcp-agent", "agent")
    assert access_for_scoped("10.0.0.5", LOCAL_IP, token, store) == (None, None, None)


def test_access_for_scoped_revoked_token_denies(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("mcp-agent", "agent")
    store.revoke(device_id)
    assert access_for_scoped("192.168.1.55", LOCAL_IP, token, store) == (None, None, None)


# --------------------------------------------------------------------------- #
# app.py wiring: the route-level bound for 'agent' -- NOTHING but /mcp. Proves
# "a bounded capability set, not the whole API" holds at the ROUTE layer too,
# independent of the /mcp per-tool gate (test_mcp_http.py).
# --------------------------------------------------------------------------- #
def test_agent_role_denied_every_ordinary_api_route(tmp_path, monkeypatch):
    # An agent device (seeded directly -- POST /api/pair-code deliberately still
    # only mints central/user codes; an operator promotes an already-paired
    # device to 'agent' via the EXISTING POST /api/devices/{id}/role instead)
    # gets NOTHING from the ordinary HTTP API: 'agent' is absent from BOTH
    # can_view's and can_change_state's role tuples (auth.py), so
    # require_authenticated/require_local/require_scope(...) all deny it.
    db_path = str(tmp_path / "agent_route.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("mcp-agent", "agent")
    seed_store.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {token}"}
    assert peer.get("/api/state", headers=auth).status_code == 403          # presence:read
    assert peer.get("/api/inventory", headers=auth).status_code == 403      # network:read
    assert peer.get("/api/cameras", headers=auth).status_code == 403        # camera:view
    assert peer.post("/api/system/toggle", json={"on": True},
                     headers=auth).status_code == 403                       # control
    assert peer.get("/api/devices", headers=auth).status_code == 403        # admin
    # 2026-07-16 audit: five GET routes carried NO Depends and leaked to 'agent'
    # (findings #2 MEDIUM + #3 LOW). Each now gates on the scope its sibling
    # write/read path uses. This is the regression that lets the gap resurface.
    assert peer.get("/api/status", headers=auth).status_code == 403          # presence:read (#2: house.people)
    assert peer.get("/api/system", headers=auth).status_code == 403          # control (#3)
    assert peer.get("/api/system/toggles", headers=auth).status_code == 403  # control (#3)
    assert peer.get("/api/speedtest/info", headers=auth).status_code == 403  # control (#3)
    assert peer.get("/api/core/pin/status", headers=auth).status_code == 403  # presence:read (#3)


def test_scoped_reads_stay_open_to_the_roles_that_own_them(tmp_path, monkeypatch):
    # Control for the agent-denied test: the five newly-gated GETs must still be
    # reachable by the roles that legitimately read them, or the fix broke the
    # dashboard. central holds every scope; a 'user' companion holds presence:read
    # (so /api/status + /api/core/pin/status) but NOT control (so the three
    # system/egress-posture routes 403 it -- which is correct: the frontend never
    # calls those for a non-central companion, it early-returns on !companionCentral).
    db_path = str(tmp_path / "scoped.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed = DeviceStore(db_path)
    _cid, ctok = seed.add("panel", "central")
    _uid, utok = seed.add("phone", "user")
    seed.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    c = {"Authorization": f"Bearer {ctok}"}
    u = {"Authorization": f"Bearer {utok}"}
    # central (has control + presence:read): every one of the five stays open.
    for path in ("/api/status", "/api/system", "/api/system/toggles",
                 "/api/speedtest/info", "/api/core/pin/status"):
        assert peer.get(path, headers=c).status_code == 200, f"central lost {path}"
    # user (presence:read, no control): the two presence-gated reads stay open...
    assert peer.get("/api/status", headers=u).status_code == 200
    assert peer.get("/api/core/pin/status", headers=u).status_code == 200
    # ...and the three control-plane reads correctly 403 (user never calls them).
    assert peer.get("/api/system", headers=u).status_code == 403
    assert peer.get("/api/system/toggles", headers=u).status_code == 403
    assert peer.get("/api/speedtest/info", headers=u).status_code == 403


def test_agent_role_denied_the_live_stream_and_its_ticket(tmp_path, monkeypatch):
    # The live x/y position + vitals stream (/ws/live, ADR-0002's most sensitive
    # live-only class) and the ticket that opens it must deny 'agent' for the SAME
    # reason every ordinary route does: an agent's only surface is /mcp, where the
    # per-tool allow-list and audit trail live. The ws-ticket router was wired with
    # no dependencies (app.py) while every sibling route gates on a scope, so an
    # agent got 403 on /api/state yet 200 on /api/ws-ticket and a CONNECTED
    # /ws/live -- streaming the exact data /api/state denied it, outside the MCP
    # audit trail entirely. Both now require presence:read (which 'agent' lacks and
    # 'user'/'central' have), so this is one gate, not a special case. (2026-07-16)
    db_path = str(tmp_path / "agent_ws.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("mcp-agent", "agent")
    seed_store.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {token}"}
    assert peer.get("/api/state", headers=auth).status_code == 403          # the control
    # The ticket that unlocks the stream must deny the agent, not just the stream.
    assert peer.post("/api/ws-ticket", headers=auth).status_code == 403
    # And the socket itself must refuse an agent bearer even if a ticket were had.
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with peer.websocket_connect("/ws/live", headers=auth):
            pass


def test_user_role_still_gets_the_live_stream(tmp_path, monkeypatch):
    # The control for the test above: a 'user' companion HAS presence:read, so the
    # new gate must not break its access to its own house's live view.
    db_path = str(tmp_path / "user_ws.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed_store = DeviceStore(db_path)
    _id, token = seed_store.add("phone", "user")
    seed_store.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {token}"}
    r = peer.post("/api/ws-ticket", headers=auth)
    assert r.status_code == 200 and r.json().get("ticket")


def test_agent_role_promoted_via_existing_devices_role_route(tmp_path, monkeypatch):
    # The admin-gated POST /api/devices/{id}/role route (unchanged by this
    # feature) already accepts 'agent' now that it's in VALID_ROLES -- an
    # operator can promote an already-paired 'user' device without any new
    # route. The promoted device's scopes/tool_scopes stay whatever they were
    # (NULL if never explicitly granted) -- set_role touches ONLY the role
    # column (devices.py's own documented invariant) -- so it falls back to the
    # 'agent' role defaults automatically.
    app = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    central = TestClient(app, headers=CSRF)
    devices = central.get("/api/devices").json()["devices"]
    device_id = devices[0]["device_id"]
    r = central.post(f"/api/devices/{device_id}/role", json={"role": "agent"})
    assert r.status_code == 200
    assert r.json()["role"] == "agent"
    # The SAME token, now role=agent, loses ordinary API access on its very next
    # request (no re-pair, no new token needed).
    assert peer.get("/api/state", headers=auth).status_code == 403
