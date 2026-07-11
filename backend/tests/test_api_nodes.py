"""Router tests for the node onboarding + telemetry surface (wavr.api_nodes),
mounted on a MINIMAL FastAPI app (no create_app -- app.py is intentionally not
imported here). A fake async `on_event` captures what would reach fusion."""
from __future__ import annotations

import struct

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from wavr.api_nodes import (
    build_nodes_admin_router, build_nodes_ingest_router, build_nodes_public_router,
)
from wavr.nodes import REACTIVATE_MAX_ATTEMPTS, NodeEnroller, NodeStore


def _ld2450_frame() -> str:
    slot = struct.pack("<HHHH", 0x8000 | 1000, 0x8000 | 500, 0x8000 | 25, 0)
    return (b"\xaa\xff\x03\x00" + slot + b"\x00" * 16 + b"\x55\xcc").hex()


def _allow_admin() -> None:
    """Test stand-in for the real `[Depends(require_local), Depends(require_root)]`
    app.py wires. Proves the router behaves correctly once admin_deps IS wired --
    the fail-closed DEFAULT (admin_deps omitted) is proven separately, unwired, by
    test_admin_router_fails_closed_without_deps below."""
    return None


class _Harness:
    def __init__(self, tmp_path):
        self.store = NodeStore(str(tmp_path / "nodes.db"))
        self.enroller = NodeEnroller(self.store)
        self.events = []

        async def on_event(ev):
            self.events.append(ev)

        app = FastAPI()
        app.include_router(build_nodes_public_router(self.store, self.enroller))
        app.include_router(build_nodes_ingest_router(self.store, on_event))
        app.include_router(build_nodes_admin_router(
            self.store, self.enroller, admin_deps=[Depends(_allow_admin)]))
        self.client = TestClient(app)

    def enroll(self, sensor_type="ld2450", room="kitchen"):
        r = self.client.post("/api/nodes/enroll-code",
                             json={"name": "n", "sensor_type": sensor_type, "room": room})
        code = r.json()["code"]
        r = self.client.post("/api/nodes/enroll", json={"code": code})
        body = r.json()
        return body["node_id"], body["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# -- enrollment ---------------------------------------------------------------

def test_enroll_happy_path(tmp_path):
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    assert node_id and token
    nodes = h.client.get("/api/nodes").json()["nodes"]
    assert nodes[0]["node_id"] == node_id and nodes[0]["modality"] == "mmwave"


def test_enroll_bad_code_rejected(tmp_path):
    h = _Harness(tmp_path)
    assert h.client.post("/api/nodes/enroll", json={"code": "00000000"}).status_code == 403


def test_enroll_code_bad_sensor_type_422(tmp_path):
    h = _Harness(tmp_path)
    r = h.client.post("/api/nodes/enroll-code",
                      json={"name": "n", "sensor_type": "nope", "room": "kitchen"})
    assert r.status_code == 422


# -- telemetry ----------------------------------------------------------------

def test_telemetry_valid_token_feeds_fusion(tmp_path):
    h = _Harness(tmp_path)
    _, token = h.enroll()
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert len(h.events) == 1
    assert h.events[0].modality == "mmwave" and h.events[0].room == "kitchen"
    assert h.events[0].count == 1


def test_telemetry_requires_token(tmp_path):
    h = _Harness(tmp_path)
    h.enroll()
    r = h.client.post("/api/nodes/telemetry", json={"seq": 1})
    assert r.status_code == 401
    r = h.client.post("/api/nodes/telemetry", headers=_auth("bogus"), json={"seq": 1})
    assert r.status_code == 403
    assert h.events == []


def test_telemetry_replay_rejected(tmp_path):
    h = _Harness(tmp_path)
    _, token = h.enroll()
    body = {"seq": 5, "ld2450_frames": [_ld2450_frame()]}
    assert h.client.post("/api/nodes/telemetry", headers=_auth(token), json=body).status_code == 200
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token), json=body)
    assert r.status_code == 409                 # replayed seq
    assert len(h.events) == 1


def test_telemetry_requires_integer_seq(tmp_path):
    h = _Harness(tmp_path)
    _, token = h.enroll()
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"ld2450_frames": [_ld2450_frame()]})
    assert r.status_code == 400


def test_telemetry_malformed_payload_shape_never_500s(tmp_path):
    # Appsec finding #2 (MEDIUM, reproduced live): a wrong-shaped `ld2450_frames`/
    # `targets` used to raise an unhandled TypeError/ValueError straight into this
    # handler. It must now be accepted-but-dropped (still 200, no fusion event),
    # never a 500.
    h = _Harness(tmp_path)
    _, token = h.enroll()
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": 123})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert h.events == []


def test_telemetry_dict_shaped_arrays_never_500(tmp_path):
    # M4 (appsec re-audit, 2026-07, MEDIUM): a JSON OBJECT for `ld2450_frames`/
    # `targets` (instead of an array) used to reach the raw `payload["..."][:64]`
    # slice -- on this Python, slicing a dict raises KeyError, which was NOT
    # caught by the (TypeError, ValueError, OverflowError) tuple and 500'd this
    # handler for any caller holding a valid node token. Must now be
    # accepted-but-dropped (200, no fusion event), never a 500.
    h = _Harness(tmp_path)
    _, token = h.enroll()
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": {"0": "aa"}})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert h.events == []
    _, token2 = h.enroll(sensor_type="generic", room="hall")
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token2),
                      json={"seq": 1, "targets": {"0": {"id": 1, "x": 1.0, "y": 2.0}}})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert h.events == []


def test_telemetry_overflow_values_never_500(tmp_path):
    # Appsec finding #2, part 2 (M1): a huge-magnitude JSON int in x/motion/
    # velocity/confidence, or a target `id` that decoded from the non-standard
    # `1e400` literal to +inf, used to raise an unhandled OverflowError straight
    # into this handler (500) -- `_num()`'s `math.isfinite()` can't widen either
    # to a C double. Must now be accepted-but-dropped (200, no fusion event),
    # exactly like the other malformed-shape cases above.
    h = _Harness(tmp_path)
    _, token = h.enroll(sensor_type="generic")
    huge = 10 ** 400
    for body in (
        {"seq": 1, "targets": [{"id": 1, "x": huge, "y": 1.0}]},
        {"seq": 2, "targets": [{"id": 1, "x": 1.0, "y": 2.0, "velocity": huge}]},
        {"seq": 3, "targets": [{"id": 1, "x": 1.0, "y": 2.0, "confidence": huge}]},
        {"seq": 4, "presence": True, "motion": huge},
    ):
        r = h.client.post("/api/nodes/telemetry", headers=_auth(token), json=body)
        assert r.status_code == 200 and r.json()["accepted"] is True
    # `id: 1e400` is valid JSON *syntax* that overflows to +inf on decode -- but
    # httpx's own outbound encoder refuses to SERIALIZE a Python `float("inf")`
    # (strict RFC 8259), so this one case is sent as a raw body to exercise the
    # server's json.loads (which, like Python's, decodes the literal to +inf,
    # allow_nan=True by default) rather than the test client's encoder.
    r = h.client.post(
        "/api/nodes/telemetry",
        headers={**_auth(token), "Content-Type": "application/json"},
        content=b'{"seq": 5, "targets": [{"id": 1e400, "x": 1.0, "y": 2.0}]}',
    )
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert h.events == []


def test_telemetry_seq_out_of_int64_range_is_a_clean_400(tmp_path):
    # Appsec finding #2, part 2 (M2): the anti-replay `seq` was accepted as ANY
    # Python int and bound straight into SQLite's `UPDATE ... SET last_seq = ?`,
    # whose INTEGER column is a signed 64-bit sqlite3_int64 -- a seq outside that
    # range raised an unwrapped OverflowError deep in NodeStore.record_seq (500).
    # Must now be rejected at the API boundary as a clean 400, same status as any
    # other malformed seq (see test_telemetry_requires_integer_seq above).
    h = _Harness(tmp_path)
    _, token = h.enroll()
    for bad_seq in (10 ** 19, 2 ** 63, -1):
        r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                          json={"seq": bad_seq, "ld2450_frames": [_ld2450_frame()]})
        assert r.status_code == 400
    assert h.events == []


def test_telemetry_on_disabled_node_rejected(tmp_path):
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    assert h.client.post(f"/api/nodes/{node_id}/disable").status_code == 200
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert r.status_code == 423                 # kill-switch enforced at ingest
    assert h.events == []


# -- heartbeat + kill-switch reachability ------------------------------------

def test_heartbeat_reflects_kill_switch(tmp_path):
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    assert h.client.post("/api/nodes/heartbeat", headers=_auth(token)).json()["command"] == "ok"
    h.client.post(f"/api/nodes/{node_id}/disable")
    assert h.client.post("/api/nodes/heartbeat", headers=_auth(token)).json()["command"] == "sleep"


def test_heartbeat_on_revoked_token_is_403_not_bare(tmp_path):
    # A revoked node's token can never re-authenticate (NodeStore.revoke() clears
    # the hash), so it never sees an in-body "revoked" -- it gets a definitive
    # 401/403 with a JSON detail body instead of a network-error-indistinguishable
    # bare failure. See _HEARTBEAT_COMMAND's comment in api_nodes.py.
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    h.client.delete(f"/api/nodes/{node_id}")
    r = h.client.post("/api/nodes/heartbeat", headers=_auth(token))
    assert r.status_code == 403
    assert r.json().get("detail")


def test_reactivate_is_node_initiated_enable(tmp_path):
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    h.client.post(f"/api/nodes/{node_id}/disable")
    r = h.client.post("/api/nodes/reactivate", headers=_auth(token), json={"press_count": 1})
    assert r.status_code == 200 and r.json()["state"] == "active"
    # Telemetry flows again after the physical re-enable.
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert r.status_code == 200 and len(h.events) == 1


def test_reactivate_press_count_out_of_int64_range_is_a_clean_400(tmp_path):
    # Same class of bug as test_telemetry_seq_out_of_int64_range_is_a_clean_400 below:
    # press_count binds into the same signed-64-bit SQLite INTEGER column
    # (NodeStore.reactivate's UPDATE), so an out-of-range value must be rejected at
    # the API boundary as a clean 400, never an unwrapped OverflowError (500) deep
    # in the store. Node red-team finding, fixed pre-merge -- regression lock.
    h = _Harness(tmp_path)
    _, token = h.enroll()
    for bad_press_count in (10 ** 19, 2 ** 63, -1):
        r = h.client.post("/api/nodes/reactivate", headers=_auth(token),
                          json={"press_count": bad_press_count})
        assert r.status_code == 400


def test_telemetry_huge_ld2450_frames_array_is_capped_not_500(tmp_path):
    # Node red-team finding, fixed pre-merge: a node batching an absurd number of
    # frames in one payload (an LD2450 emits <=3 targets/frame -- thousands is
    # definitionally malformed) must be capped, not allowed to parse/materialize
    # 50k Targets (~+23MB measured) or crash the handler. See _MAX_ARRAY's comment
    # in wavr.nodes.node_event. Regression lock.
    h = _Harness(tmp_path)
    _, token = h.enroll()
    huge_frames = [_ld2450_frame()] * 50_000
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": huge_frames})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert len(h.events) == 1
    assert h.events[0].count is not None and h.events[0].count <= 64


def test_no_remote_enable_route_exists(tmp_path):
    # Remote-OFF-never-ON: there is no admin enable endpoint at all.
    h = _Harness(tmp_path)
    node_id, _ = h.enroll()
    assert h.client.post(f"/api/nodes/{node_id}/enable").status_code == 404


def test_reactivate_rate_limited_returns_429(tmp_path):
    # Appsec finding #3: an abuse brake against a node hammering reactivate, not a
    # security boundary (the node already holds a valid bearer token throughout).
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    for i in range(1, REACTIVATE_MAX_ATTEMPTS + 1):
        r = h.client.post("/api/nodes/reactivate", headers=_auth(token),
                          json={"press_count": i})
        assert r.status_code == 200
    r = h.client.post("/api/nodes/reactivate", headers=_auth(token),
                      json={"press_count": REACTIVATE_MAX_ATTEMPTS + 1})
    assert r.status_code == 429


# -- admin --------------------------------------------------------------------

def test_disable_unknown_node_404(tmp_path):
    h = _Harness(tmp_path)
    assert h.client.post("/api/nodes/ghost/disable").status_code == 404


def test_revoke_kills_token(tmp_path):
    h = _Harness(tmp_path)
    node_id, token = h.enroll()
    assert h.client.delete(f"/api/nodes/{node_id}").status_code == 200
    r = h.client.post("/api/nodes/telemetry", headers=_auth(token),
                      json={"seq": 1, "ld2450_frames": [_ld2450_frame()]})
    assert r.status_code == 403                 # revoked token is dead


def test_admin_router_fails_closed_without_deps(tmp_path):
    # Appsec finding #1, HIGH: admin_deps=None used to default to [] (NO AUTH).
    # A forgotten wiring in app.py must never expose disable/revoke/enroll-code/
    # list unauthenticated -- the default must DENY instead.
    store = NodeStore(str(tmp_path / "nodes.db"))
    enroller = NodeEnroller(store)
    app = FastAPI()
    app.include_router(build_nodes_admin_router(store, enroller))  # no admin_deps!
    client = TestClient(app)

    assert client.post("/api/nodes/enroll-code",
                       json={"name": "n", "sensor_type": "ld2450",
                             "room": "kitchen"}).status_code == 403
    assert client.get("/api/nodes").status_code == 403
    assert client.post("/api/nodes/ghost/disable").status_code == 403
    assert client.delete("/api/nodes/ghost").status_code == 403


def test_admin_router_fails_closed_with_empty_deps_list(tmp_path):
    # Belt-and-suspenders: an explicitly-empty list is treated the same as
    # "forgot to wire it" -- also denies, rather than silently running open.
    store = NodeStore(str(tmp_path / "nodes.db"))
    enroller = NodeEnroller(store)
    app = FastAPI()
    app.include_router(build_nodes_admin_router(store, enroller, admin_deps=[]))
    client = TestClient(app)
    assert client.get("/api/nodes").status_code == 403
