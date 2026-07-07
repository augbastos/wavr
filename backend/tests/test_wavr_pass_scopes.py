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
"""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.auth import SCOPES, DEFAULT_SCOPES, access_for, effective_scopes, has_scope
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
