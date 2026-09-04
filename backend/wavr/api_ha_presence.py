"""Mapping Home Assistant sensors to Wavr rooms.

Four routes, and the shape of them is the point: Wavr can SUGGEST which HA
entities look like presence sensors and which room each probably watches, but
only a person can turn a suggestion into a mapping. Presence in the wrong room is
worse than no presence at all — a light that comes on in an empty hallway is
merely odd, while "somebody is in the bedroom" pointing at the wrong bedroom is
the kind of wrong that makes a household stop believing anything the product
says.

Everything here is admin-only and local-only. A mapping decides where evidence
about people lands, so it belongs with the other Space-configuration powers
rather than with the read-only context ones.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.ha_presence import HAPresenceError, suggest


def build_router(*, store, ha_client_fn, rooms_fn, require_local,
                 require_scope, on_change=None) -> APIRouter:
    """Routes over an HA presence mapping store.

    `ha_client_fn` is a callable rather than a client so the token is read at
    call time from config — the same lifecycle `ha_import` uses, and the reason
    an operator can fix a wrong token without restarting Wavr.

    `on_change` lets the app re-register the source when the mapping set becomes
    empty or non-empty, which is what keeps "an install with no mappings makes no
    requests to HA" true at runtime rather than only at boot.
    """
    router = APIRouter()

    @router.get("/api/ha/presence")
    async def list_mappings(_=Depends(require_local),
                            __=Depends(require_scope("admin"))):
        rows = [m.to_dict() for m in store.list()]
        return {
            "mappings": rows,
            "active": sum(1 for m in rows if m["enabled"]),
            "note": ("Each mapping makes one Home Assistant sensor a source of "
                     "presence evidence for one room. Nothing is polled until "
                     "something is mapped."),
        }

    @router.get("/api/ha/presence/suggestions")
    async def suggestions(_=Depends(require_local),
                          __=Depends(require_scope("admin"))):
        """Entities that LOOK like presence sensors, with a room guess.

        Reaches HA, so it is a deliberate action rather than something the
        dashboard polls: a household with no Home Assistant should never see
        Wavr making requests to one.
        """
        client = ha_client_fn()
        if client is None:
            raise HTTPException(
                status_code=409,
                detail="Home Assistant is not configured on this Core")
        try:
            entities = client.get_entities()
        except Exception as exc:      # noqa: BLE001 -- reaching off-box
            # 502, not 500: the failure is in the thing Wavr called, and saying
            # so is the difference between "check your Home Assistant" and "file
            # a Wavr bug".
            raise HTTPException(status_code=502,
                                detail=f"Home Assistant unreachable: {exc}")
        mapped = {m.entity_id for m in store.list()}
        rows = [r for r in suggest(entities, rooms_fn())
                if r["entity_id"] not in mapped]
        return {"suggestions": rows,
                "note": ("A guess at which room each sensor watches, from its "
                         "name. Check it — a sensor mapped to the wrong room "
                         "puts people in the wrong room.")}

    @router.put("/api/ha/presence/{entity_id}")
    async def upsert(entity_id: str, room: str = Body(...),
                     modality: str = Body("pir"), label: str = Body(""),
                     _=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        try:
            m = store.map(entity_id, room, modality, label)
        except HAPresenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await _changed()
        return m.to_dict()

    @router.post("/api/ha/presence/{entity_id}/enabled")
    async def set_enabled(entity_id: str, enabled: bool = Body(..., embed=True),
                          _=Depends(require_local),
                          __=Depends(require_scope("admin"))):
        if not store.set_enabled(entity_id, enabled):
            raise HTTPException(status_code=404, detail="not mapped")
        await _changed()
        return {"entity_id": entity_id, "enabled": enabled}

    @router.delete("/api/ha/presence/{entity_id}")
    async def unmap(entity_id: str, _=Depends(require_local),
                    __=Depends(require_scope("admin"))):
        if not store.unmap(entity_id):
            raise HTTPException(status_code=404, detail="not mapped")
        await _changed()
        return {"entity_id": entity_id, "removed": True}

    async def _changed() -> None:
        if on_change is not None:
            await on_change()

    return router
