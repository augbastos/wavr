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

import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from wavr.auth import parse_bearer


def _deps_not_wired() -> None:
    """FAIL-CLOSED default for `delete_deps` (sweep #13, mirrors
    api_nodes._admin_deps_not_wired). Previously `delete_deps=None` -> `[]`, so if
    app.py's wiring ever forgot to pass `[Depends(require_csrf_root)]`, DELETE
    /api/devices/{id} and POST /api/devices/{id}/role would run completely
    UNAUTHENTICATED. A forgotten argument must never silently open device
    revoke/role-change, so the default now DENIES instead: both routes 403 until
    the real gate is explicitly wired."""
    raise HTTPException(status_code=403,
                        detail="device management routes have no auth gate wired")


def build_pair_router(store, pairing, on_redeem=None) -> APIRouter:
    """POST /api/pair {code, device_name} -> {device_id, token}. The token is
    returned exactly once. `store` is accepted for symmetry/future use; the redeem
    goes through `pairing`, which owns the store.

    `on_redeem(code, device_id)` is an optional post-mint hook. app.py uses it to
    stamp the PERSON the code was minted for onto the device that redeemed it --
    which is what lets that person's role later cap this credential. It runs
    AFTER the mint and its failure never fails the pairing: a device that paired
    successfully but could not be associated is recoverable from the admin
    screen, whereas a pairing that 500s after minting a token leaves the operator
    with a credential they were never shown."""
    router = APIRouter()

    @router.post("/api/pair")
    async def pair(request: Request, code: str = Body(...), device_name: str = Body(...),
                   device_key: str | None = Body(None)):
        code = code.strip()
        device_name = device_name.strip()
        if not code or not device_name:
            raise HTTPException(status_code=400, detail="code and device_name are required")
        # `device_key` is OPTIONAL and hashed by the client: the same physical
        # device sends the same value every time it pairs. When one arrives, the
        # redeem retires the credentials that device held before, so re-installing
        # the app stops leaving another live key to the home behind. A client that
        # doesn't send one (an older app, a browser) pairs exactly as it always did.
        chave = (device_key or "").strip()[:200] or None
        # Pass the caller's IP so the failed-attempt rate-limiter is keyed per host
        # (sweep [4]/[13]): a junk-flooding host throttles only itself, not everyone.
        source_ip = request.client.host if request.client else None
        result = pairing.redeem(code, device_name, source_ip=source_ip, device_key=chave)
        if result is None:
            # Two different refusals, and they used to read the same.
            #
            # After ten failed guesses this host is blocked for a minute, and
            # `redeem` then refuses the CORRECT code without looking at it --
            # right, and indistinguishable from "you typed it wrong". Somebody
            # who mistyped a few times gets told their good code is invalid,
            # generates a new one, and is told the same, because the block is on
            # the host and not the code. 429 with a sentence that says to wait
            # is the difference between a minute and giving up.
            #
            # Asked AFTER the attempt, so the attempt that crossed the line is
            # already counted and reports honestly. `is_throttled` records
            # nothing, so asking cannot cause what it reports.
            if getattr(pairing, "is_throttled", None) and pairing.is_throttled(source_ip):
                raise HTTPException(
                    status_code=429,
                    detail="Too many attempts from this device. Wait a minute, "
                           "then try the same code again.")
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")
        device_id, token = result
        if on_redeem is not None:
            try:
                on_redeem(code, device_id)
            except Exception:      # noqa: BLE001 -- see the docstring
                logging.warning("pairing: could not associate the device with its "
                                "person; associate it from the admin screen",
                                exc_info=True)
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


def build_devices_router(store, delete_deps=None, on_revoke=None) -> APIRouter:
    """GET /api/devices -> list (no token material); DELETE /api/devices/{id} ->
    revoke. Revocation takes effect on the device's next request.

    `delete_deps` (optional) are extra FastAPI dependencies applied ONLY to the
    state-changing DELETE (e.g. a CSRF-header guard) -- the GET list is a read and
    needs no CSRF, so it must stay reachable without the header."""
    router = APIRouter()
    ddeps = list(delete_deps) if delete_deps else [Depends(_deps_not_wired)]

    @router.get("/api/devices")
    async def devices():
        return {"devices": [d.to_dict() for d in store.list()]}

    @router.delete("/api/devices/{device_id}", dependencies=ddeps)
    async def revoke(device_id: str):
        if not store.revoke(device_id):
            raise HTTPException(status_code=404, detail=f"unknown device: {device_id}")
        # Its spatial grants go with it. Without this, pairing a replacement
        # under the same id would inherit the revoked device's experience
        # scopes -- the kind of inheritance nobody remembers granting.
        # Guarded: an unpair must not fail because a grant store is unreadable.
        dropped = 0
        if on_revoke is not None:
            try:
                dropped = int(on_revoke(device_id) or 0)
            except Exception:      # noqa: BLE001
                dropped = 0
        return {"revoked": device_id, "grants_dropped": dropped}

    @router.post("/api/devices/{device_id}/role", dependencies=ddeps)
    async def set_role(device_id: str, role: str = Body(..., embed=True)):
        """Promote/demote an already-paired device between the two grantable roles.
        State-changing, so it carries the SAME delete_deps (CSRF X-Wavr-Local guard)
        as DELETE, and the router-level dep already limits it to central/root. Never
        returns or alters token material — only the role column moves."""
        try:
            changed = store.set_role(device_id, role)
        except ValueError as exc:
            # Surface the store's reason (invalid role, or the guest-not-by-role-change
            # rule) rather than a fixed message, so the caller learns WHY.
            raise HTTPException(status_code=422, detail=str(exc) or f"invalid role: {role!r}")
        if not changed:
            raise HTTPException(status_code=404, detail=f"unknown device: {device_id}")
        return {"device_id": device_id, "role": role}

    return router
