"""Brokering UWB ranging sessions over HTTP.

Two routes, and the difference between their authorizations is the whole
security of the feature.

**Opening** a session needs `control` — it is an arrangement between devices in
the Space, and deciding which devices may range with each other is an operator
concern.

**Claiming** a session's parameters resolves the device id from the BEARER TOKEN
and never from the path or the body. That rule is what makes the key safe to
hand out at all: with a device id in the request, any paired device could ask for
another's key and receive it, and the session key is the entire secret of a UWB
ranging session.

`/api/devices/me/manifest` established the same rule for the same reason — a
device may only ever act on itself.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException

from wavr.auth import parse_bearer
from wavr.uwb_broker import UwbError


def build_router(*, broker, devices, capable_fn, require_local,
                 require_scope) -> APIRouter:
    """Routes over a `UwbBroker`.

    `capable_fn(device_id) -> True | False | None` reads the device's capability
    manifest. The tristate is carried all the way in: `None` means the device
    never mentioned UWB, and the broker refuses rather than assuming.
    """
    router = APIRouter()

    @router.post("/api/uwb/sessions")
    async def open_session(controller: str = Body(...),
                           controlees: list = Body(...),
                           _=Depends(require_local),
                           __=Depends(require_scope("control"))):
        try:
            session = broker.open(controller, controlees, capable=capable_fn)
        except UwbError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return session.to_dict()

    @router.post("/api/uwb/sessions/{session_id}/claim")
    async def claim(session_id: str,
                    authorization: str | None = Header(default=None),
                    _=Depends(require_scope("presence:write"))):
        """A device collects its own ranging parameters. Once.

        The id comes from the token. A `device_id` parameter here would let any
        paired device ask for another's session key, which is the entire secret
        of the ranging session.
        """
        token = parse_bearer(authorization)
        device = devices.verify(token) if (devices is not None and token) else None
        if device is None:
            # Loopback root is not a paired device and has no radio to range
            # with. Refused with the reason rather than a bare 401, because
            # "your token is wrong" would send somebody to the wrong place.
            raise HTTPException(
                status_code=400,
                detail=("only a paired device can collect ranging parameters — "
                        "the session key is issued to a specific device, and "
                        "the id comes from your credential rather than from "
                        "the request"))
        try:
            return broker.claim(session_id, device.device_id)
        except UwbError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.get("/api/uwb/sessions")
    async def list_sessions(_=Depends(require_local),
                            __=Depends(require_scope("control"))):
        """Live sessions, WITHOUT their keys.

        `to_dict` does not carry the key at all, so this route cannot leak one
        by forgetting to strip it.
        """
        return {"sessions": [s.to_dict() for s in broker.list()],
                "note": ("Keys are never listed. Each device collects its own "
                         "once, and a lost one means a new session.")}

    @router.delete("/api/uwb/sessions/{session_id}")
    async def close_session(session_id: str, _=Depends(require_local),
                            __=Depends(require_scope("control"))):
        if not broker.close(session_id):
            raise HTTPException(status_code=404, detail="no such session")
        return {"session_id": session_id, "closed": True}

    return router
