"""FastAPI routers for "approve on the Core" pairing (design 2026-07-11).

Two routers, two DIFFERENT auth boundaries -- the exact split pattern
api_peers.py / api_nodes.py already use for their public vs. admin surfaces:

  * build_pair_request_router     -- POST /api/pair-request (create) + POST
    /api/pair-request/status (poll). Deliberately UNAUTH, in-subnet-bounded
    (app.py must add BOTH exact paths to the same exemption tuple that already
    covers /api/pair): a companion has to reach these before it holds any
    token. Neither route mints anything -- `create()` only opens a PENDING
    record; `poll()` only ever returns a token AFTER a loopback-root Approve.
    `request_id` travels in the request BODY on both calls, never in the URL/
    query string, so it never lands in an access log.

  * build_pending_pairings_router -- loopback-root control plane. `admin_deps`
    in app.py MUST be `[Depends(require_local), Depends(require_root)]` --
    the SAME tier as the peers-admin / nodes-admin routers (ARP-block-grade):
    `require_root` rejects even an authenticated remote 'central' peer, so a
    stolen/paired central token can never approve its own (or any other)
    pending request. Approve is the ONLY place a token is minted -- it calls
    the SAME `device_store.add(name, role)` `/api/pair` uses.

The 8-digit `/api/pair` + `/api/pair-code` flow is completely untouched and
stays mounted as the fallback -- this module is purely additive.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

# Deliberately narrower than devices.VALID_ROLES ({"central","user","agent"}):
# a Core operator approving a companion can only grant the two device-facing
# roles, exactly like the existing POST /api/pair-code role check
# (app.py's `pair_code` handler) -- 'agent' is a separate, MCP-only
# principal that is never handed out through a pairing flow.
_GRANTABLE_ROLES = ("central", "user")


def _admin_deps_not_wired() -> None:
    """FAIL-CLOSED default for `admin_deps` (mirrors
    api_nodes._admin_deps_not_wired / the equivalent in api_peers.py): a
    forgotten wiring argument must never silently open the loopback-root
    list/approve/deny surface -- every admin route 403s until the real gate
    is explicitly wired in app.py."""
    raise HTTPException(status_code=403,
                        detail="pending-pairing admin routes have no auth gate wired")


def build_pair_request_router(approvals, cert_fingerprint_fn) -> APIRouter:
    """`approvals` is a `PairApprovalManager`. `cert_fingerprint_fn` is a
    zero-arg callable returning the Core's own LIVE cert fingerprint (the same
    source `/api/pair-code` reads, `cert_fingerprint(resolved_cert_path(...))`)
    -- injected so this router never touches TLS config/filesystem itself."""
    router = APIRouter()

    @router.post("/api/pair-request")
    async def create_request(request: Request,
                             requester_name: str = Body(...),
                             platform: str | None = Body(None),
                             reported_fp: str | None = Body(None)):
        # Untrusted in-subnet caller, pre-token: FastAPI/pydantic already
        # rejects a non-string body field with a clean 422 (these are typed
        # params, not a raw dict), so the only thing left to validate here is
        # "is there actually a name" -- delegated to the manager, which also
        # bounds every field's length before it ever sits in memory.
        source_ip = request.client.host if request.client else None
        try:
            request_id, compare_code = approvals.create(
                requester_name, source_ip=source_ip,
                platform=platform, reported_fp=reported_fp)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "request_id": request_id,
            # Returned ONLY here, in the requester's own create() response,
            # over its own pinned channel -- poll() below never echoes it,
            # so no other in-subnet caller can learn it by polling. Show
            # this number to the operator; it is the per-request anchor
            # that makes the approve prompt unambiguous (see
            # pair_requests.py module docstring, factor 3).
            "compare_code": compare_code,
            "cert_fingerprint": cert_fingerprint_fn(),
            "poll_after_ms": 1500,
        }

    @router.post("/api/pair-request/status")
    async def poll_request(request_id: str = Body(..., embed=True)):
        # request_id is the companion's ONLY capability here (192-bit secret);
        # kept in the body (never a path/query param) so it's never logged.
        if not isinstance(request_id, str) or not request_id.strip():
            raise HTTPException(status_code=400, detail="request_id is required")
        return approvals.poll(request_id.strip())

    return router


def build_pending_pairings_router(approvals, cert_fingerprint_fn, admin_deps=None) -> APIRouter:
    """Loopback-root control plane. `admin_deps` MUST be
    `[Depends(require_local), Depends(require_root)]` (app.py wires this,
    mirroring build_peers_admin_router / build_nodes_admin_router). FAIL
    CLOSED if omitted/empty -- see `_admin_deps_not_wired` -- rather than an
    open default. `cert_fingerprint_fn` is the SAME zero-arg live-cert-fp
    callable passed to build_pair_request_router (and the source /api/pair-code
    reads) -- surfaced here too so the Core's own operator banner can show its
    live fingerprint without minting a pairing code as a side effect."""
    router = APIRouter()
    admin_deps = list(admin_deps) if admin_deps else [Depends(_admin_deps_not_wired)]

    @router.get("/api/pending-pairings", dependencies=admin_deps)
    async def list_pending():
        return {"requests": approvals.list_pending(), "cert_fingerprint": cert_fingerprint_fn()}

    @router.post("/api/pending-pairings/{request_id}/approve", dependencies=admin_deps)
    async def approve(request_id: str,
                      role: str = Body("user", embed=True),
                      confirm_code: str = Body(..., embed=True)):
        # confirm_code travels in the BODY (never the URL/query string), same
        # discipline as request_id in the public router below -- it must
        # never land in an access log. Required (not optional): the operator
        # must have actually read the number off the companion's screen and
        # echoed it back before ANY token is minted (see pair_requests.py
        # approve() docstring) -- this is what turns the on-screen numeric
        # comparison from a UI nicety into an enforced gate.
        if not isinstance(role, str) or role not in _GRANTABLE_ROLES:
            raise HTTPException(status_code=422,
                                detail="role must be central or user")
        if not isinstance(confirm_code, str) or not confirm_code.strip():
            raise HTTPException(status_code=422,
                                detail="confirm_code is required")
        device_id = approvals.approve(request_id, role, confirm_code.strip())
        if device_id is None:
            raise HTTPException(status_code=404,
                                detail="unknown/expired request, or the numbers did not match")
        return {"ok": True, "device_id": device_id}

    @router.post("/api/pending-pairings/{request_id}/deny", dependencies=admin_deps)
    async def deny(request_id: str):
        if not approvals.deny(request_id):
            raise HTTPException(status_code=404,
                                detail="unknown or expired pairing request")
        return {"ok": True}

    return router
