"""FastAPI router factories for the multi-device auth surface (ADR-0006, Phase 1).

Three small routers, each built around the DeviceStore / PairingManager so they
stay injectable and testable:

  * build_pair_router       -> POST /api/pair       (redeem code -> token, once)
  * build_ws_ticket_router  -> POST /api/ws-ticket  (Bearer token -> WS ticket)
  * build_devices_router    -> GET/DELETE /api/devices (list + revoke)

These routers carry no access control of their own beyond what each endpoint needs
functionally (a code, a bearer token). The load-bearing gates — loopback-or-authed
and the per-role route gate — live in app.py's middleware/dependencies, which wrap
these routes when `WAVR_MULTIDEVICE` is on. `/api/pair` is deliberately reachable
by an unauthenticated in-subnet peer (that is the whole point of pairing); it is
still bounded by the pairing code's ~2-min one-time window.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Header, HTTPException

from wavr.auth import parse_bearer


def build_pair_router(store, pairing) -> APIRouter:
    """POST /api/pair {code, device_name} -> {device_id, token}. The token is
    returned exactly once. `store` is accepted for symmetry/future use; the redeem
    goes through `pairing`, which owns the store."""
    router = APIRouter()

    @router.post("/api/pair")
    async def pair(code: str = Body(...), device_name: str = Body(...)):
        code = code.strip()
        device_name = device_name.strip()
        if not code or not device_name:
            raise HTTPException(status_code=400, detail="code and device_name are required")
        result = pairing.redeem(code, device_name)
        if result is None:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")
        device_id, token = result
        return {"device_id": device_id, "token": token}

    return router


def build_ws_ticket_router(store, pairing) -> APIRouter:
    """POST /api/ws-ticket (Authorization: Bearer <token>) -> {ticket}. The ticket
    is short-lived + single-use; the companion then opens /ws/live?ticket=..."""
    router = APIRouter()

    @router.post("/api/ws-ticket")
    async def ws_ticket(authorization: str | None = Header(default=None)):
        token = parse_bearer(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="missing bearer token")
        device = store.verify(token)
        if device is None:
            raise HTTPException(status_code=403, detail="invalid or revoked token")
        return {"ticket": pairing.mint_ticket(device.device_id)}

    return router


def build_devices_router(store, delete_deps=None) -> APIRouter:
    """GET /api/devices -> list (no token material); DELETE /api/devices/{id} ->
    revoke. Revocation takes effect on the device's next request.

    `delete_deps` (optional) are extra FastAPI dependencies applied ONLY to the
    state-changing DELETE (e.g. a CSRF-header guard) -- the GET list is a read and
    needs no CSRF, so it must stay reachable without the header."""
    router = APIRouter()

    @router.get("/api/devices")
    async def devices():
        return {"devices": [d.to_dict() for d in store.list()]}

    @router.delete("/api/devices/{device_id}", dependencies=list(delete_deps or []))
    async def revoke(device_id: str):
        if not store.revoke(device_id):
            raise HTTPException(status_code=404, detail=f"unknown device: {device_id}")
        return {"revoked": device_id}

    return router
