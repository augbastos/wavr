"""Router tests for the "approve on the Core" pairing-request surface
(wavr.api_pair_requests), mounted on a MINIMAL FastAPI app (no create_app --
app.py is intentionally not imported here, same pattern as test_api_nodes.py).

Two DIFFERENT auth boundaries, exercised separately:
  * build_pair_request_router (create/poll) -- deliberately UNAUTH here too;
    the real in-subnet bound is app.py's exemption tuple, not this router.
  * build_pending_pairings_router (list/approve/deny) -- admin-gated. The
    happy-path tests wire a stand-in `_allow_admin` dependency (mirrors
    test_api_nodes.py's `_allow_admin` / api_peers's convention: "deps
    default to []" means the FAIL-CLOSED default is proven unwired,
    separately, by test_admin_router_fails_closed_without_deps).

Covers: happy path (create -> compare_code goes ONLY to its own caller,
approve mints a real device token, poll then returns it), the TTL/expiry
lifecycle (injected clock, no real waiting), the per-IP + global rate/flood
caps, and the stated security invariants (compare_code never leaks via
poll(), unknown ids resolve cleanly rather than 500ing, malformed/untrusted
JSON bodies 422 rather than crash the handler).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from wavr.api_pair_requests import build_pair_request_router, build_pending_pairings_router
from wavr.devices import DeviceStore
from wavr.pair_requests import PairApprovalManager


# --------------------------------------------------------------------------- #
# Injectable clock: a mutable UTC clock advanced by hand in TTL tests (same
# idiom as test_multidevice.py's Clock / test_pin_ratelimit.py's Clock).
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _allow_admin() -> None:
    """Test stand-in for the real `[Depends(require_local), Depends(require_root)]`
    app.py wires. Proves the router behaves correctly once admin_deps IS wired --
    the fail-closed DEFAULT (admin_deps omitted) is proven separately, unwired, by
    test_admin_router_fails_closed_without_deps below."""
    return None


class _Harness:
    def __init__(self, tmp_path, clock: Clock | None = None, **manager_kwargs):
        self.store = DeviceStore(str(tmp_path / "devices.db"))
        self.clock = clock or Clock()
        self.approvals = PairApprovalManager(self.store, now_fn=self.clock, **manager_kwargs)

        app = FastAPI()
        app.include_router(build_pair_request_router(
            self.approvals, cert_fingerprint_fn=lambda: "AA:BB:CC"))
        app.include_router(build_pending_pairings_router(
            self.approvals, cert_fingerprint_fn=lambda: "AA:BB:CC",
            admin_deps=[Depends(_allow_admin)]))
        self.client = TestClient(app)

    def create(self, name="phone", **kw):
        return self.client.post("/api/pair-request", json={"requester_name": name, **kw})

    def poll(self, request_id):
        return self.client.post("/api/pair-request/status", json={"request_id": request_id})

    def approve(self, request_id, code, role="user"):
        return self.client.post(
            f"/api/pending-pairings/{request_id}/approve",
            json={"role": role, "confirm_code": code})

    def deny(self, request_id):
        return self.client.post(f"/api/pending-pairings/{request_id}/deny")

    def list_pending(self):
        return self.client.get("/api/pending-pairings")


# --------------------------------------------------------------------------- #
# create() -- happy path + the compare_code isolation invariant.
# --------------------------------------------------------------------------- #
def test_create_returns_request_id_and_compare_code_to_caller(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] and body["compare_code"]
    assert len(body["compare_code"]) == 6 and body["compare_code"].isdigit()
    assert body["cert_fingerprint"] == "AA:BB:CC"
    assert body["poll_after_ms"] == 1500


def test_create_bad_body_is_422_not_500(tmp_path):
    h = _Harness(tmp_path)
    # Missing the required requester_name field entirely.
    r = h.client.post("/api/pair-request", json={})
    assert r.status_code == 422
    # Wrong type -- pydantic-typed param, not a raw dict.
    r = h.client.post("/api/pair-request", json={"requester_name": 12345})
    assert r.status_code == 422
    # Not even a JSON object.
    r = h.client.post("/api/pair-request", json="just a string")
    assert r.status_code == 422


def test_create_empty_or_whitespace_name_is_400_not_500(tmp_path):
    h = _Harness(tmp_path)
    for bad_name in ("", "   "):
        r = h.create(bad_name)
        assert r.status_code == 400


def test_compare_code_not_echoed_to_a_second_requester(tmp_path):
    # THE stated invariant: two independently-created requests must never let
    # one caller learn the other's compare_code via any response it can read.
    h = _Harness(tmp_path)
    r1 = h.create("phone-a")
    r2 = h.create("phone-b")
    code_a = r1.json()["compare_code"]
    code_b = r2.json()["compare_code"]
    assert code_a != code_b
    # Neither poll() response (own or the other's id) ever surfaces a code.
    poll_a = h.poll(r1.json()["request_id"]).json()
    poll_b = h.poll(r2.json()["request_id"]).json()
    assert "compare_code" not in poll_a
    assert "compare_code" not in poll_b


def test_compare_code_unique_among_concurrently_pending(tmp_path):
    h = _Harness(tmp_path, max_pending=20)
    codes = {h.create(f"phone-{i}").json()["compare_code"] for i in range(5)}
    assert len(codes) == 5  # no collisions while all 5 are still pending


# --------------------------------------------------------------------------- #
# poll() -- status semantics, unknown-id handling, no-token-before-approve.
# --------------------------------------------------------------------------- #
def test_poll_unknown_id_is_clean_expired_not_500(tmp_path):
    h = _Harness(tmp_path)
    r = h.poll("does-not-exist")
    assert r.status_code == 200
    assert r.json() == {"status": "expired"}


def test_poll_bad_body_is_400_not_500(tmp_path):
    h = _Harness(tmp_path)
    r = h.client.post("/api/pair-request/status", json={"request_id": "   "})
    assert r.status_code == 400
    r = h.client.post("/api/pair-request/status", json={})
    assert r.status_code == 422  # embed=..., required field missing entirely
    r = h.client.post("/api/pair-request/status", json={"request_id": 123})
    assert r.status_code == 422  # wrong type


def test_poll_pending_has_no_token_before_approve(tmp_path):
    h = _Harness(tmp_path)
    rid = h.create("phone").json()["request_id"]
    body = h.poll(rid).json()
    assert body["status"] == "pending"
    assert "token" not in body
    assert "device_id" not in body


def test_poll_after_approve_returns_token_and_device_id(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    ap = h.approve(rid, code)
    assert ap.status_code == 200
    device_id = ap.json()["device_id"]
    body = h.poll(rid).json()
    assert body == {"status": "approved", "device_id": device_id, "token": body["token"]}
    assert body["token"]
    # The minted token is real -- it verifies against the SAME DeviceStore.
    dev = h.store.verify(body["token"])
    assert dev is not None and dev.device_id == device_id and dev.role == "user"


def test_poll_after_deny_returns_denied_no_leak(tmp_path):
    h = _Harness(tmp_path)
    rid = h.create("phone").json()["request_id"]
    assert h.deny(rid).status_code == 200
    body = h.poll(rid).json()
    assert body == {"status": "denied"}


# --------------------------------------------------------------------------- #
# TTL / expiry -- injected clock, no real waiting.
# --------------------------------------------------------------------------- #
def test_request_expires_after_request_ttl(tmp_path):
    clock = Clock()
    h = _Harness(tmp_path, clock=clock, request_ttl=180)
    rid = h.create("phone").json()["request_id"]
    clock.advance(181)
    assert h.poll(rid).json() == {"status": "expired"}


def test_request_still_pending_just_before_ttl(tmp_path):
    clock = Clock()
    h = _Harness(tmp_path, clock=clock, request_ttl=180)
    rid = h.create("phone").json()["request_id"]
    clock.advance(179)
    assert h.poll(rid).json()["status"] == "pending"


def test_approve_after_request_expired_is_404(tmp_path):
    clock = Clock()
    h = _Harness(tmp_path, clock=clock, request_ttl=180)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    clock.advance(181)
    ap = h.approve(rid, code)
    assert ap.status_code == 404


def test_approved_token_pickup_window_expires(tmp_path):
    clock = Clock()
    # request_ttl < approval_ttl so approval EXTENDS the pickup window to approval_ttl.
    # (The window is max(remaining request TTL, approval_ttl) -- "never shorter than what
    # was left" -- so a LONGER request TTL would govern instead; keep it short here to
    # exercise the approval_ttl expiry specifically.)
    h = _Harness(tmp_path, clock=clock, request_ttl=60, approval_ttl=120)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    h.approve(rid, code)
    clock.advance(121)
    assert h.poll(rid).json() == {"status": "expired"}


# --------------------------------------------------------------------------- #
# approve()/deny() -- unknown id, wrong/missing confirm_code, role validation.
# --------------------------------------------------------------------------- #
def test_approve_unknown_id_is_clean_404(tmp_path):
    h = _Harness(tmp_path)
    r = h.approve("does-not-exist", "123456")
    assert r.status_code == 404


def test_deny_unknown_id_is_clean_404(tmp_path):
    h = _Harness(tmp_path)
    r = h.deny("does-not-exist")
    assert r.status_code == 404


def test_approve_wrong_confirm_code_rejected_no_token_minted(tmp_path):
    h = _Harness(tmp_path)
    rid = h.create("phone").json()["request_id"]
    r = h.approve(rid, "000000")
    assert r.status_code == 404
    # Still pollable/pending -- a wrong guess doesn't burn the request.
    assert h.poll(rid).json()["status"] == "pending"


def test_approve_missing_confirm_code_is_422(tmp_path):
    h = _Harness(tmp_path)
    rid = h.create("phone").json()["request_id"]
    r = h.client.post(f"/api/pending-pairings/{rid}/approve", json={"role": "user"})
    assert r.status_code == 422


def test_approve_rejects_ungrantable_role(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    # 'agent' is a VALID_ROLES member but deliberately NOT grantable here.
    r = h.approve(rid, code, role="agent")
    assert r.status_code == 422
    assert h.poll(rid).json()["status"] == "pending"


def test_approve_bad_role_type_is_422_not_500(tmp_path):
    h = _Harness(tmp_path)
    rid = h.create("phone").json()["request_id"]
    r = h.client.post(f"/api/pending-pairings/{rid}/approve",
                      json={"role": 12345, "confirm_code": "123456"})
    assert r.status_code == 422


def test_double_approve_second_call_is_404(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    assert h.approve(rid, code).status_code == 200
    # Same code, already-decided request -- no second mint.
    assert h.approve(rid, code).status_code == 404


def test_deny_then_approve_is_404(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    assert h.deny(rid).status_code == 200
    assert h.approve(rid, code).status_code == 404


# --------------------------------------------------------------------------- #
# list_pending() -- admin surface, and the compare_code IS shown here
# (loopback-root-only view -- different boundary than poll()).
# --------------------------------------------------------------------------- #
def test_list_pending_shows_compare_code_to_admin_only(tmp_path):
    h = _Harness(tmp_path)
    r = h.create("phone")
    rid, code = r.json()["request_id"], r.json()["compare_code"]
    listing = h.list_pending().json()
    assert listing["cert_fingerprint"] == "AA:BB:CC"
    entries = listing["requests"]
    assert len(entries) == 1 and entries[0]["request_id"] == rid
    assert entries[0]["compare_code"] == code
    assert "token" not in entries[0]


def test_list_pending_excludes_decided_requests(tmp_path):
    h = _Harness(tmp_path)
    r1 = h.create("phone-a")
    r2 = h.create("phone-b")
    h.deny(r1.json()["request_id"])
    listing = h.list_pending().json()["requests"]
    ids = {e["request_id"] for e in listing}
    assert ids == {r2.json()["request_id"]}


# --------------------------------------------------------------------------- #
# admin-gate fail-closed default -- MUST deny when admin_deps is omitted/empty,
# same discipline as test_api_nodes.test_admin_router_fails_closed_without_deps.
# --------------------------------------------------------------------------- #
def test_admin_router_fails_closed_without_deps(tmp_path):
    store = DeviceStore(str(tmp_path / "devices.db"))
    approvals = PairApprovalManager(store)
    app = FastAPI()
    app.include_router(build_pending_pairings_router(
        approvals, cert_fingerprint_fn=lambda: "AA:BB:CC"))  # no admin_deps!
    client = TestClient(app)

    assert client.get("/api/pending-pairings").status_code == 403
    assert client.post("/api/pending-pairings/ghost/approve",
                       json={"role": "user", "confirm_code": "123456"}).status_code == 403
    assert client.post("/api/pending-pairings/ghost/deny").status_code == 403


def test_admin_router_fails_closed_with_empty_deps_list(tmp_path):
    store = DeviceStore(str(tmp_path / "devices.db"))
    approvals = PairApprovalManager(store)
    app = FastAPI()
    app.include_router(build_pending_pairings_router(
        approvals, cert_fingerprint_fn=lambda: "AA:BB:CC", admin_deps=[]))
    client = TestClient(app)
    assert client.get("/api/pending-pairings").status_code == 403


# --------------------------------------------------------------------------- #
# Flood / rate bound -- per-IP and global caps evict oldest, never 500/hang.
# --------------------------------------------------------------------------- #
def test_per_ip_flood_evicts_oldest_never_grows_unbounded(tmp_path):
    h = _Harness(tmp_path, max_pending_per_ip=3, max_pending=20)
    app = h.client.app

    # TestClient doesn't let us spoof request.client.host per-call easily via
    # json-only helper; hit the route directly with a client whose transport
    # reports a fixed peer -- default TestClient peer is "testclient", which
    # is stable across calls, so repeated creates from this client all share
    # one source_ip bucket and exercise the per-IP cap directly.
    ids = [h.create(f"phone-{i}").json()["request_id"] for i in range(5)]
    listing_ids = {e["request_id"] for e in h.list_pending().json()["requests"]}
    # Only the most recent max_pending_per_ip requests survive.
    assert len(listing_ids) == 3
    assert listing_ids == set(ids[-3:])
    # The evicted (oldest) ones resolve cleanly, not 500.
    assert h.poll(ids[0]).json() == {"status": "expired"}
