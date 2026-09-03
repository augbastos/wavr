"""Room adjacency over HTTP.

Transport only. Every judgement lives in `topology.py`.

The graph is re-derived from the current house map on every request rather than
cached: an operator who redraws a room expects adjacency to follow, and a cached
graph would quietly describe a floor plan that no longer exists.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.topology import build_graph, describe, transition


async def _deps_not_wired():
    raise HTTPException(status_code=403,
                        detail="topology routes have no auth gate wired")


def build_topology_router(house_fn=None, store=None, deps=None) -> APIRouter:
    """`house_fn() -> dict` returns the current house map."""
    router = APIRouter(dependencies=deps if deps is not None else [Depends(_deps_not_wired)])

    def _graph():
        house = house_fn() if house_fn is not None else {}
        overrides = store.overrides() if store is not None else []
        return build_graph(house or {}, overrides)

    @router.get("/api/topology")
    async def get_topology():
        return describe(_graph())

    @router.post("/api/topology/link")
    async def set_link(room_a: str = Body(...), room_b: str = Body(...),
                       connected: bool = Body(...), note: str = Body("")):
        """Correct the graph. An operator's statement always beats the geometry."""
        if store is None:
            raise HTTPException(status_code=503,
                                detail="topology cannot be edited on this Core")
        try:
            return store.declare(room_a, room_b, connected, note)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.delete("/api/topology/link")
    async def clear_link(room_a: str, room_b: str):
        """Drop a correction and fall back to whatever the drawing says."""
        if store is None:
            raise HTTPException(status_code=503,
                                detail="topology cannot be edited on this Core")
        return {"room_a": room_a, "room_b": room_b,
                "cleared": store.clear(room_a, room_b)}

    @router.get("/api/topology/transition")
    async def check_transition(from_room: str, to_room: str):
        """Is a movement between these rooms physically plausible, and why.

        Read-only and advisory. Nothing here changes what Wavr believes about
        who is where — topology explains evidence, it never creates any.
        """
        return transition(_graph(), from_room, to_room)

    return router
