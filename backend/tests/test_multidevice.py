"""Multi-device client-auth backend tests (ADR-0006, Phase 1).

Covers the device/token store, pairing-code + WS-ticket lifecycle, the pure
`authorize` decision, and the FastAPI router happy/deny paths. All deterministic:
time is injected (no real waiting), and there is no real network.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.auth import authorize, can_change_state, can_view, in_subnet, parse_bearer
from wavr.api_devices import build_devices_router, build_pair_router, build_ws_ticket_router
from wavr.devices import VALID_ROLES, DeviceStore
from wavr.pairing import PairingManager


# --------------------------------------------------------------------------- #
# Injectable clock: a mutable UTC clock advanced by hand in TTL tests.
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _store(tmp_path, now_fn=None):
    return DeviceStore(str(tmp_path / "devices.db"), **({"now_fn": now_fn} if now_fn else {}))


# --------------------------------------------------------------------------- #
# DeviceStore: token issuance, hashing, verify, revoke, list.
# --------------------------------------------------------------------------- #
def test_add_returns_id_and_token(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("phone", "user")
    assert device_id and token
    assert len(token) >= 32  # 256-bit secret, URL-safe base64


def test_token_stored_hashed_not_plaintext(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    row = store._conn.execute("SELECT token_hash FROM devices").fetchone()
    assert row["token_hash"] != token           # never stored in the clear
    assert token not in row["token_hash"]


def test_verify_returns_device_with_role(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("laptop", "central")
    dev = store.verify(token)
    assert dev is not None
    assert dev.device_id == device_id and dev.role == "central"


def test_verify_updates_last_seen(tmp_path):
    clock_a = "2026-07-03T12:00:00+00:00"
    clock_b = "2026-07-03T13:00:00+00:00"
    stamps = iter([clock_a, clock_a, clock_b])  # add(created), verify1, verify2
    store = _store(tmp_path, now_fn=lambda: next(stamps))
    _id, token = store.add("phone", "user")
    first = store.verify(token)
    second = store.verify(token)
    assert first.last_seen_ts == clock_a
    assert second.last_seen_ts == clock_b       # last_seen advances on each verify


def test_verify_unknown_token_is_none(tmp_path):
    store = _store(tmp_path)
    assert store.verify("not-a-real-token") is None
    assert store.verify("") is None


def test_revoke_then_verify_is_none(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("phone", "user")
    assert store.verify(token) is not None
    assert store.revoke(device_id) is True
    assert store.verify(token) is None          # revoked -> rejected next call


def test_revoke_unknown_device_is_false(tmp_path):
    store = _store(tmp_path)
    assert store.revoke("nope") is False


def test_revoke_is_idempotent(tmp_path):
    store = _store(tmp_path)
    device_id, _token = store.add("phone", "user")
    assert store.revoke(device_id) is True
    assert store.revoke(device_id) is True      # still True: device exists


def test_list_includes_revoked_without_tokens(tmp_path):
    store = _store(tmp_path)
    id1, _ = store.add("a", "user")
    _id2, _ = store.add("b", "central")
    store.revoke(id1)
    listed = store.list()
    assert {d.name for d in listed} == {"a", "b"}
    assert any(d.revoked for d in listed)
    for d in listed:                            # no token material ever leaks
        assert not hasattr(d, "token") and not hasattr(d, "token_hash")


def test_add_rejects_invalid_role(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.add("x", "admin")
    # 'sensor' added for phone telemetry (mobile unification, blueprint step 1); it is
    # confined to POST /api/telemetry by app.py middleware (see test_telemetry_sensor.py).
    assert VALID_ROLES == {"central", "user", "sensor"}


# --------------------------------------------------------------------------- #
# Pairing codes: one-time redeem, TTL via injected clock, role assignment.
# --------------------------------------------------------------------------- #
def test_mint_and_redeem_code_issues_token_once(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    code = pairing.mint_code("user")
    result = pairing.redeem(code, "phone")
    assert result is not None
    device_id, token = result
    assert store.verify(token).device_id == device_id


def test_code_is_one_time(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    code = pairing.mint_code("user")
    assert pairing.redeem(code, "phone") is not None
    assert pairing.redeem(code, "phone2") is None   # consumed


def test_code_expires_via_injected_clock(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    pairing = PairingManager(store, now_fn=clock, code_ttl=120)
    code = pairing.mint_code("user")
    clock.advance(121)                              # past the 2-min TTL
    assert pairing.redeem(code, "phone") is None


def test_code_valid_just_before_expiry(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    pairing = PairingManager(store, now_fn=clock, code_ttl=120)
    code = pairing.mint_code("user")
    clock.advance(119)
    assert pairing.redeem(code, "phone") is not None


def test_code_role_assignment(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    code = pairing.mint_code("central")
    device_id, token = pairing.redeem(code, "peer-pc")
    assert store.verify(token).role == "central"


def test_mint_code_rejects_invalid_role(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    with pytest.raises(ValueError):
        pairing.mint_code("root")


def test_redeem_unknown_code_is_none(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    assert pairing.redeem("000000", "phone") is None


# --------------------------------------------------------------------------- #
# WS tickets: single-use, TTL, wrong ticket rejected.
# --------------------------------------------------------------------------- #
def test_ws_ticket_single_use(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    ticket = pairing.mint_ticket("dev-123")
    assert pairing.redeem_ticket(ticket) == "dev-123"
    assert pairing.redeem_ticket(ticket) is None    # consumed


def test_ws_ticket_expires_via_injected_clock(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    pairing = PairingManager(store, now_fn=clock, ticket_ttl=30)
    ticket = pairing.mint_ticket("dev-123")
    clock.advance(31)
    assert pairing.redeem_ticket(ticket) is None


def test_ws_ticket_wrong_ticket_rejected(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    pairing.mint_ticket("dev-123")
    assert pairing.redeem_ticket("bogus") is None


# --------------------------------------------------------------------------- #
# auth.authorize: the pure access decision.
# --------------------------------------------------------------------------- #
LOCAL_IP = "192.168.1.10"


def test_authorize_loopback_is_root(tmp_path):
    store = _store(tmp_path)
    for host in ("127.0.0.1", "::1", "testclient"):
        assert authorize(host, LOCAL_IP, None, store) == "root"


def test_authorize_valid_in_subnet_token_returns_role(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    assert authorize("192.168.1.55", LOCAL_IP, token, store) == "user"


def test_authorize_central_token_returns_central(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("peer-pc", "central")
    assert authorize("192.168.1.55", LOCAL_IP, token, store) == "central"


def test_authorize_unknown_token_is_none(tmp_path):
    store = _store(tmp_path)
    assert authorize("192.168.1.55", LOCAL_IP, "garbage", store) is None


def test_authorize_no_token_is_none(tmp_path):
    store = _store(tmp_path)
    assert authorize("192.168.1.55", LOCAL_IP, None, store) is None


def test_authorize_revoked_token_is_none(tmp_path):
    store = _store(tmp_path)
    device_id, token = store.add("phone", "user")
    store.revoke(device_id)
    assert authorize("192.168.1.55", LOCAL_IP, token, store) is None


def test_authorize_out_of_subnet_token_is_none(tmp_path):
    store = _store(tmp_path)
    _id, token = store.add("phone", "user")
    # Valid token but the peer is on a different /24 -> denied (and never verified).
    assert authorize("10.0.0.5", LOCAL_IP, token, store) is None


def test_in_subnet_same_and_different_24():
    assert in_subnet("192.168.1.55", "192.168.1.10") is True
    assert in_subnet("192.168.2.55", "192.168.1.10") is False
    assert in_subnet("10.0.0.5", "192.168.1.10") is False
    assert in_subnet("not-an-ip", "192.168.1.10") is False
    assert in_subnet("192.168.1.55", None) is False


def test_parse_bearer():
    assert parse_bearer("Bearer abc123") == "abc123"
    assert parse_bearer("bearer abc123") == "abc123"   # case-insensitive scheme
    assert parse_bearer("Basic abc123") is None
    assert parse_bearer("abc123") is None
    assert parse_bearer(None) is None
    assert parse_bearer("Bearer ") is None


def test_role_helpers():
    assert can_change_state("root") and can_change_state("central")
    assert not can_change_state("user") and not can_change_state(None)
    assert can_view("user") and can_view("central") and can_view("root")
    assert not can_view(None)


# --------------------------------------------------------------------------- #
# Router happy/deny paths on a tiny app that only includes the auth routers.
# (No loopback middleware here -> isolates the routers' own behaviour.)
# --------------------------------------------------------------------------- #
def _router_client(tmp_path):
    store = DeviceStore(str(tmp_path / "r.db"))
    pairing = PairingManager(store)
    app = FastAPI()
    app.include_router(build_pair_router(store, pairing))
    app.include_router(build_ws_ticket_router(store, pairing))
    app.include_router(build_devices_router(store))
    return TestClient(app), store, pairing


def test_pair_endpoint_happy_path(tmp_path):
    client, _store_, pairing = _router_client(tmp_path)
    code = pairing.mint_code("user")
    r = client.post("/api/pair", json={"code": code, "device_name": "phone"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["device_id"]


def test_pair_endpoint_bad_code_denied(tmp_path):
    client, _store_, _pairing = _router_client(tmp_path)
    r = client.post("/api/pair", json={"code": "000000", "device_name": "phone"})
    assert r.status_code == 403


def test_pair_endpoint_missing_fields_400(tmp_path):
    client, _store_, pairing = _router_client(tmp_path)
    code = pairing.mint_code("user")
    r = client.post("/api/pair", json={"code": code, "device_name": "  "})
    assert r.status_code == 400


def test_ws_ticket_endpoint_happy_path(tmp_path):
    client, store, _pairing = _router_client(tmp_path)
    _id, token = store.add("phone", "user")
    r = client.post("/api/ws-ticket", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["ticket"]


def test_ws_ticket_endpoint_no_token_401(tmp_path):
    client, _store_, _pairing = _router_client(tmp_path)
    r = client.post("/api/ws-ticket")
    assert r.status_code == 401


def test_ws_ticket_endpoint_bad_token_403(tmp_path):
    client, _store_, _pairing = _router_client(tmp_path)
    r = client.post("/api/ws-ticket", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_devices_list_and_revoke(tmp_path):
    client, store, _pairing = _router_client(tmp_path)
    device_id, _token = store.add("phone", "user")
    listed = client.get("/api/devices").json()["devices"]
    assert any(d["device_id"] == device_id for d in listed)
    assert all("token" not in d and "token_hash" not in d for d in listed)
    assert client.delete(f"/api/devices/{device_id}").status_code == 200
    assert client.delete("/api/devices/unknown-id").status_code == 404


def test_revoked_device_ws_ticket_denied(tmp_path):
    client, store, _pairing = _router_client(tmp_path)
    device_id, token = store.add("phone", "user")
    client.delete(f"/api/devices/{device_id}")
    r = client.post("/api/ws-ticket", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403       # revoked token can't mint a WS ticket
