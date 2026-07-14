"""FastAPI routers for sensor-node onboarding + telemetry (design 2026-07-11).

Three routers, each with a DIFFERENT auth boundary (app.py wires the loopback-admin
gate; the ingest routes self-authenticate on the NODE bearer token, and enroll is
code-bounded) -- the exact split pattern api_peers.py uses:

  * build_nodes_public_router  -- POST /api/nodes/enroll. Deliberately UNAUTH,
    in-subnet-bounded (app.py exempts it like /api/pair): a headless node must
    reach it before it holds any token. Bounded by the one-time, per-IP
    rate-limited enrollment code minted on the trusted loopback screen.

  * build_nodes_ingest_router  -- the node-reachable data plane. Every route
    verifies the NODE bearer token IN-HANDLER via `node_store.get_by_token` (these
    are NOT DeviceStore tokens, so app.py must exempt these paths from the device
    middleware -- see the wiring spec). Three routes:
      - POST /api/nodes/telemetry   push a sensor frame -> fusion (via `on_event`).
      - POST /api/nodes/heartbeat   poll for the kill command (ok|sleep|revoked).
      - POST /api/nodes/reactivate  the node-initiated physical re-enable.

  * build_nodes_admin_router   -- loopback-root control plane (admin_deps in
    app.py = require_local + require_root). Mint codes, list, DISABLE (remote-OFF),
    REVOKE. There is deliberately NO enable route: remote-OFF-never-ON is enforced
    by the ABSENCE of a remote enable, with reactivation living only on the
    node-authed /api/nodes/reactivate. FAIL-CLOSED: omitting `admin_deps` does NOT
    open these routes -- see `_admin_deps_not_wired`.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from wavr.auth import parse_bearer
from wavr.nodes import (
    STATE_ACTIVE, STATE_DISABLED, STATE_REVOKED, NodeReactivateRateLimited,
    node_event,
)

# Heartbeat command a node acts on: ok = sense normally; sleep = go dark (remote
# kill reached the hardware); revoked = factory-reset, this token is dead. `ok` and
# `sleep` are both reachable (a disabled node still authenticates, see
# NodeStore.get_by_token). `revoked` is listed here for symmetry/future-proofing,
# but in the CURRENT design it is never actually returned in-body: NodeStore.revoke()
# clears the token hash (anti-resurrection), so a revoked node can never
# authenticate again and this route always 401/403s for it instead -- see
# firmware/wavr_client.cpp's sendHeartbeat() and NODE_PROTOCOL.md for how the
# firmware is expected to treat a persistent 401/403 on this authenticated route
# as equivalent to an in-body "revoked" (never a bare/ambiguous signal: it is a
# definitive rejection from the server, not a network error, and it carries a JSON
# `detail` body).
_HEARTBEAT_COMMAND = {
    STATE_ACTIVE: "ok", STATE_DISABLED: "sleep", STATE_REVOKED: "revoked",
}


def build_nodes_public_router(node_store, enroller) -> APIRouter:
    """The ONE deliberately-UNAUTH, in-subnet-bounded entry point a node calls.
    `node_store` is accepted for symmetry; the redeem goes through `enroller`."""
    router = APIRouter()

    @router.post("/api/nodes/enroll")
    async def enroll(request: Request, code: str = Body(...),
                     cert_fingerprint: str = Body("")):
        # Safe UNAUTH for the same reason POST /api/pair is: it only ever consumes a
        # code minted on the trusted loopback screen, and per-IP rate-limiting (keyed
        # on the caller's IP, mirroring build_pair_router) means one host's junk
        # guesses can't lock out others. The node self-reports only its cert
        # fingerprint (pinned TOFU) -- never its room/modality/trust.
        source_ip = request.client.host if request.client else None
        result = enroller.redeem(code.strip(), cert_fingerprint.strip(), source_ip=source_ip)
        if result is None:
            raise HTTPException(status_code=403, detail="invalid or expired enrollment code")
        node_id, token = result
        # The token is returned exactly once -- the node stores it and presents it on
        # every telemetry/heartbeat/reactivate call thereafter.
        return {"node_id": node_id, "token": token}

    return router


def _auth_node(node_store, authorization: str | None):
    """Resolve the node behind a Bearer token or raise 401. Shared by every ingest
    route so a rogue device with no/invalid token never reaches the data plane."""
    token = parse_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing node bearer token")
    node = node_store.get_by_token(token)
    if node is None:
        raise HTTPException(status_code=403, detail="invalid or revoked node token")
    return node


def build_nodes_ingest_router(node_store, on_event) -> APIRouter:
    """Node-reachable data plane. `on_event` is the app's async fusion ingest
    callback (`_ingest`) -- the SAME seam SourceManager feeds, so a node frame
    enters fusion by exactly the path a local source does."""
    router = APIRouter()

    @router.post("/api/nodes/telemetry")
    async def telemetry(authorization: str | None = Header(default=None),
                        payload: dict = Body(...)):
        node = _auth_node(node_store, authorization)
        # Kill-switch enforced at ingest: a DISABLED node's data is dropped here even
        # if its firmware keeps sending (defense in depth vs. the heartbeat sleep).
        if node.state != STATE_ACTIVE:
            raise HTTPException(status_code=423, detail="node disabled")
        # Telemetry anti-replay: a strictly-increasing per-node seq. A missing/
        # non-int seq is rejected outright (a genuine node always sends one).
        # Bounded to a signed 64-bit range at THIS boundary, before it ever reaches
        # `record_seq`'s SQLite UPDATE: SQLite's INTEGER column is a C sqlite3_int64,
        # and binding a Python int outside that range raises an unwrapped
        # OverflowError deep in NodeStore -- a 500, not a clean 400, for what is
        # just more attacker-shaped JSON from an untrusted node.
        seq = payload.get("seq")
        if (not isinstance(seq, int) or isinstance(seq, bool)
                or not (0 <= seq <= 2**63 - 1)):
            raise HTTPException(status_code=400, detail="telemetry requires an integer seq")
        if not node_store.record_seq(node.node_id, seq):
            raise HTTPException(status_code=409, detail="stale or replayed telemetry seq")
        event = node_event(node, payload)
        if event is not None:                 # None for a non-presence sensor -> accept, no fuse
            await on_event(event)
        return {"accepted": True}

    @router.post("/api/nodes/heartbeat")
    async def heartbeat(authorization: str | None = Header(default=None)):
        node = _auth_node(node_store, authorization)
        # The channel by which remote-OFF reaches the hardware: a disabled node is
        # told to `sleep`; a revoked node would already 401/403 at _auth_node (see
        # _HEARTBEAT_COMMAND's comment above), so this only ever returns ok|sleep in
        # practice.
        return {"command": _HEARTBEAT_COMMAND.get(node.state, "sleep"),
                "state": node.state}

    @router.post("/api/nodes/reactivate")
    async def reactivate(authorization: str | None = Header(default=None),
                         press_count: int = Body(..., embed=True)):
        # The ONLY disabled -> active edge, and it is NODE-INITIATED (this route is
        # reached with the node's OWN token, triggered by a physical button press
        # that bumps press_count). There is no admin-reachable enable anywhere.
        node = _auth_node(node_store, authorization)
        # Bound to signed-int64 (press_count is a firmware uint32_t; this is generous
        # headroom) BEFORE it binds into the SQLite INTEGER column -- an out-of-range
        # int would raise OverflowError -> unwrapped 500 (sibling of the seq bound in
        # telemetry). Reject malformed as a clean 400.
        if (not isinstance(press_count, int) or isinstance(press_count, bool)
                or not (0 <= press_count <= 2**63 - 1)):
            raise HTTPException(status_code=400, detail="press_count must be an integer")
        try:
            new_state = node_store.reactivate(node.node_id, press_count)
        except NodeReactivateRateLimited:
            # Abuse brake (appsec finding #3), not a security boundary -- see
            # NodeStore.reactivate()'s docstring. A node hitting this needs to slow
            # down; the operator's real recourse for a node they no longer trust is
            # DELETE /api/nodes/{id} (revoke), which no press_count can undo.
            raise HTTPException(status_code=429,
                                detail="reactivate attempted too many times; slow down")
        if new_state is None:
            raise HTTPException(status_code=404, detail="unknown node")
        return {"state": new_state}

    return router


def _admin_deps_not_wired() -> None:
    """FAIL-CLOSED default for `admin_deps` (appsec finding #1, HIGH). Previously
    `admin_deps=None` -> `[]`, meaning if app.py's wiring ever forgot to pass
    `[Depends(require_local), Depends(require_root)]`, every admin route (mint
    code, list, disable, revoke) would run completely UNAUTHENTICATED. A forgotten
    argument must never silently open the loopback-root control plane, so the
    default now DENIES instead: every admin route 403s until the real gate is
    explicitly wired. Real callers (app.py) always pass admin_deps and never hit
    this; only an intentional override (e.g. a test standing up its own
    allow-dependency) can make these routes reachable."""
    raise HTTPException(status_code=403,
                        detail="node admin routes have no auth gate wired")


def build_nodes_admin_router(node_store, enroller, admin_deps=None) -> APIRouter:
    """Loopback-root control plane. `admin_deps` MUST be
    `[Depends(require_local), Depends(require_root)]` (app.py wires this, mirroring
    build_peers_admin_router). FAIL CLOSED if omitted/empty -- see
    `_admin_deps_not_wired` -- rather than the old `None -> []` (open) default.
    There is NO enable route by design (remote-OFF-never-ON)."""
    router = APIRouter()
    admin_deps = list(admin_deps) if admin_deps else [Depends(_admin_deps_not_wired)]

    @router.post("/api/nodes/enroll-code", dependencies=admin_deps)
    async def mint_code(name: str = Body(...), sensor_type: str = Body(...),
                        room: str = Body(...), transport: str = Body("native")):
        # The operator declares WHAT + WHERE on the trusted loopback screen; the node
        # never gets to choose these. Bad sensor_type/transport/room -> 422.
        try:
            code = enroller.mint_code(name, sensor_type, room, transport)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"code": code}

    @router.get("/api/nodes", dependencies=admin_deps)
    async def list_nodes():
        return {"nodes": [n.to_dict() for n in node_store.list()]}

    @router.post("/api/nodes/{node_id}/disable", dependencies=admin_deps)
    async def disable(node_id: str):
        # Remote-OFF (allowed). The node stops feeding fusion immediately (ingest
        # rejects it) and is told to sleep on its next heartbeat.
        if not node_store.disable(node_id):
            raise HTTPException(status_code=404, detail="unknown or revoked node")
        return {"node_id": node_id, "state": STATE_DISABLED}

    @router.delete("/api/nodes/{node_id}", dependencies=admin_deps)
    async def revoke(node_id: str):
        # Terminal: kills the token. Re-flash + re-enroll to bring it back.
        if not node_store.revoke(node_id):
            raise HTTPException(status_code=404, detail="unknown node")
        return {"node_id": node_id, "state": STATE_REVOKED}

    return router
