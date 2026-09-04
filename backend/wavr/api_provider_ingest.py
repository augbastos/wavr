"""Registering an external positioning system, and letting it post.

Two different authorizations, deliberately far apart:

  * **Registering** one is local + admin. It decides what a system is allowed to
    claim about the house, which is Space configuration of the highest kind.
  * **Posting** to one needs `presence:write` — the same scope a paired phone
    uses to report its own presence. An adapter is a device that reports
    evidence; it is not an administrator, and it must never be able to widen
    what it is allowed to claim by posting.

That separation is the design. An adapter holding a credential that could also
re-register its own provider would be able to declare itself `position`-capable
and start asserting coordinates, which is exactly the escalation the declaration
exists to prevent.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from wavr.auth import parse_bearer
from wavr.provider_ingest import IngestError, translate


def build_router(*, store, devices, rooms_fn, ingest_fn, at_fn, require_local,
                 require_scope) -> APIRouter:
    router = APIRouter()

    @router.get("/api/providers/external")
    async def list_providers(_=Depends(require_scope("admin"))):
        rows = [p.to_dict() for p in store.list()]
        return {
            "providers": rows,
            # Says exactly which half Wavr enforces. This response goes to
            # integrators, and it used to claim both — reach included, which
            # describes where the vendor's own copy of the data goes and is not
            # something an inbound POST can be checked against.
            "note": ("Each one declares how far it reaches and the finest "
                     "answer it may give. The precision ceiling is ENFORCED on "
                     "every observation. Reach is a disclosure Wavr publishes "
                     "and cannot verify — it describes what the provider does "
                     "with its own copy of the data."),
        }

    @router.put("/api/providers/external/{provider_id}")
    async def register(provider_id: str, label: str = Body(...),
                       reach: str = Body(...), kind: str = Body("spatial"),
                       modality: str = Body("node"), ceiling: str = Body("room"),
                       confidence: str = Body("none"), notes: str = Body(""),
                       device_id: str = Body(""),
                       _=Depends(require_local),
                       __=Depends(require_scope("admin"))):
        """Declare an external system.

        `reach` has no default here either — see `providers.describe`. A
        household deciding whether to switch this on is asking one question, and
        it is this one.
        """
        try:
            provider = store.register(provider_id, label, kind=kind, reach=reach,
                                      modality=modality, ceiling=ceiling,
                                      confidence=confidence, notes=notes,
                                      device_id=device_id)
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        body = provider.to_dict()
        if body["ceiling"] != ceiling:
            # Said out loud rather than applied quietly: an integrator who asked
            # for `count` and got `room` needs to know why their counts will be
            # dropped, at the moment they register rather than a week later.
            body["note"] = (
                f"Capped to '{body['ceiling']}': a provider fusing as "
                f"'{modality}' cannot support '{ceiling}', and a declaration may "
                f"lower a ceiling but never raise one.")
        return body

    @router.post("/api/providers/external/{provider_id}/enabled")
    async def set_enabled(provider_id: str, enabled: bool = Body(..., embed=True),
                          _=Depends(require_local),
                          __=Depends(require_scope("admin"))):
        if not store.set_enabled(provider_id, enabled):
            raise HTTPException(status_code=404, detail="not registered")
        return {"provider_id": provider_id, "enabled": enabled}

    @router.delete("/api/providers/external/{provider_id}")
    async def remove(provider_id: str, _=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        if not store.remove(provider_id):
            raise HTTPException(status_code=404, detail="not registered")
        return {"provider_id": provider_id, "removed": True}

    @router.post("/api/providers/{provider_id}/observations")
    async def observations(request: Request, provider_id: str,
                           observations: list = Body(..., embed=True),
                           authorization: str | None = Header(default=None),
                           _=Depends(require_scope("presence:write"))):
        """Post evidence.

        `presence:write`, NOT admin. An adapter reports; it does not administer.
        A credential that could do both could re-declare its own provider as
        `position`-capable and start asserting coordinates.

        But `presence:write` ALONE was not enough, and that was a real hole.
        The scope is held by every `user` device and by `guest`, whose entire
        documented purpose is registering its own presence. Without the check
        below, a guest -- or any device paired for something unrelated -- could
        post fabricated occupancy for ANY room in the house as soon as one
        external provider existed. That is a much larger blast radius under the
        same scope name, and `register_companion` had already had to special-case
        the identical class ("Finding B, guest-mode review").

        So a provider names the ONE device that may speak for it. A provider
        with none bound accepts loopback only.
        """
        provider = store.get(provider_id)
        if provider is None:
            # Refused, not accepted-with-defaults: a default reach is a privacy
            # failure and a default ceiling would let anything claim `position`.
            raise HTTPException(
                status_code=404,
                detail=(f"{provider_id!r} is not registered. An external "
                        f"provider must declare its reach and its precision "
                        f"ceiling before it may post."))
        if not provider.enabled:
            raise HTTPException(status_code=409,
                                detail=f"{provider_id!r} is switched off")

        # Resolved from the TOKEN, the way `/api/devices/me/manifest` and the
        # UWB claim route do. The auth middleware publishes `role` and `scopes`
        # on `request.state` but not a device id, so reading one from there
        # would silently be `None` for every caller -- fail-closed, but for the
        # wrong reason, and a later reader would take it for a working check.
        token = parse_bearer(authorization)
        device = devices.verify(token) if (devices is not None and token) else None
        caller = device.device_id if device is not None else None
        role = getattr(request.state, "role", None)
        if provider.device_id:
            if caller != provider.device_id:
                raise HTTPException(
                    status_code=403,
                    detail=(f"{provider_id!r} accepts observations from one "
                            f"device only, and this is not it. Bind a different "
                            f"device by re-registering the provider."))
        else:
            # Nobody bound: loopback only. `role == "root"` is how this codebase
            # spells the Core's own loopback caller, and a paired device -- of
            # any tier -- is refused rather than trusted because it happens to
            # hold a scope every phone in the house also holds.
            if role != "root":
                raise HTTPException(
                    status_code=403,
                    detail=(f"{provider_id!r} has no device bound to it, so it "
                            f"accepts observations from this Core only. "
                            f"Re-register it naming the adapter's device."))
        try:
            events, problems = translate(provider, observations,
                                         rooms=rooms_fn(), at_fn=at_fn)
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        for event in events:
            await ingest_fn(event)
        return {
            "provider_id": provider_id,
            "accepted": len(events),
            "problems": problems,
            "note": ("Problems do not fail the batch — everything else was "
                     "accepted. They are reported so an integrator finds out "
                     "from Wavr rather than from months of missing data."),
        }

    return router
