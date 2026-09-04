"""Anchors over HTTP: create, place, bind, resolve.

Two authorization tiers, and the split is deliberate.

**Writing** an anchor is Space configuration — it changes the vocabulary every
application in the house will speak — so it needs local admin, like rooms and
topology.

**Reading** them is one tier down, at `presence:read`. An application asking
"which anchors are in the room I am in" is doing the ordinary thing this feature
exists for, and forcing it through an admin credential would mean every
experience runs with the power to reconfigure the Space. That is the shape of
permission failure worth avoiding: it is not that reading anchors is dangerous,
it is that the only credential able to read them must not also be able to delete
a room.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.anchors import AnchorError, KIND_LOGICAL, summarize


def build_router(*, store, house_fn, room_polygon_fn, require_local,
                 require_scope) -> APIRouter:
    """Routes over an anchor store.

    `room_polygon_fn(room) -> polygon | None` is injected rather than reached for
    so the units check has real geometry to test against, and so this module
    never imports the house map — the anchor store does not know what a floor
    plan is and should not start to.
    """
    router = APIRouter()

    def _rooms() -> list[str]:
        from wavr.housemap import room_names
        return room_names(house_fn())

    @router.get("/api/anchors")
    async def list_anchors(room: str = "", _=Depends(require_scope("presence:read"))):
        anchors = store.list(room=room or None)
        return summarize(anchors, _rooms())

    @router.get("/api/anchors/{anchor_id}")
    async def get_anchor(anchor_id: str, _=Depends(require_scope("presence:read"))):
        a = store.get(anchor_id)
        if a is None:
            raise HTTPException(status_code=404, detail="no such anchor")
        return a.to_dict(known_rooms=_rooms())

    @router.get("/api/anchors/resolve/{provider_id}/{external_id}")
    async def resolve(provider_id: str, external_id: str,
                      _=Depends(require_scope("presence:read"))):
        """Which Wavr anchors an external system's id refers to.

        The route a headset calls after recognising one of its own anchors, to
        find out what Wavr calls the place. A list, because two runtimes can bind
        the same id and returning the first would hide the collision.
        """
        found = store.resolve(provider_id, external_id)
        return {"provider_id": provider_id, "external_id": external_id,
                "anchors": [a.to_dict(known_rooms=_rooms()) for a in found],
                "note": ("An external id can map to more than one anchor. Wavr "
                         "returns all of them rather than picking.")}

    @router.post("/api/anchors")
    async def create(name: str = Body(...), room: str = Body(...),
                     kind: str = Body(KIND_LOGICAL), level: int = Body(0),
                     x: float | None = Body(None), y: float | None = Body(None),
                     z: float | None = Body(None),
                     polygon: list | None = Body(None), note: str = Body(""),
                     _=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        try:
            a = store.create(name, room, level=level, kind=kind, x=x, y=y, z=z,
                             polygon=polygon, note=note,
                             room_polygon=room_polygon_fn(room))
        except AnchorError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return a.to_dict(known_rooms=_rooms())

    @router.post("/api/anchors/{anchor_id}/place")
    async def place(anchor_id: str, x: float = Body(...), y: float = Body(...),
                    z: float | None = Body(None), _=Depends(require_local),
                    __=Depends(require_scope("admin"))):
        existing = store.get(anchor_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="no such anchor")
        try:
            a = store.place(anchor_id, x, y, z=z,
                            room_polygon=room_polygon_fn(existing.room))
        except AnchorError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return a.to_dict(known_rooms=_rooms())

    @router.post("/api/anchors/{anchor_id}/room")
    async def move(anchor_id: str, room: str = Body(..., embed=True),
                   _=Depends(require_local), __=Depends(require_scope("admin"))):
        try:
            a = store.move(anchor_id, room)
        except AnchorError as exc:
            raise HTTPException(status_code=404 if "no such" in str(exc) else 400,
                                detail=str(exc))
        return {**a.to_dict(known_rooms=_rooms()),
                "note": ("Coordinates were cleared: they were metres from the "
                         "old room's corner, and keeping them would place this "
                         "anchor somewhere arbitrary in the new one.")}

    @router.post("/api/anchors/{anchor_id}/name")
    async def rename(anchor_id: str, name: str = Body(..., embed=True),
                     _=Depends(require_local), __=Depends(require_scope("admin"))):
        try:
            a = store.rename(anchor_id, name)
        except AnchorError as exc:
            raise HTTPException(status_code=404 if "no such" in str(exc) else 400,
                                detail=str(exc))
        return a.to_dict(known_rooms=_rooms())

    @router.post("/api/anchors/{anchor_id}/bind")
    async def bind(anchor_id: str, provider_id: str = Body(...),
                   external_id: str = Body(...), _=Depends(require_local),
                   __=Depends(require_scope("admin"))):
        try:
            return store.bind(anchor_id, provider_id, external_id)
        except AnchorError as exc:
            raise HTTPException(status_code=404 if "no such" in str(exc) else 400,
                                detail=str(exc))

    @router.delete("/api/anchors/{anchor_id}/bind/{provider_id}/{external_id}")
    async def unbind(anchor_id: str, provider_id: str, external_id: str,
                     _=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        if not store.unbind(anchor_id, provider_id, external_id):
            raise HTTPException(status_code=404, detail="no such mapping")
        return {"removed": True}

    @router.delete("/api/anchors/{anchor_id}")
    async def delete(anchor_id: str, _=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        if not store.delete(anchor_id):
            raise HTTPException(status_code=404, detail="no such anchor")
        return {"anchor_id": anchor_id, "removed": True}

    return router
