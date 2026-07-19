"""FastAPI router for user-authored routines (the "when THIS -> do THAT" spine).

A small injectable factory around RoutineStore so it stays testable, mirroring
api_identity.py. All routes are gated in app.py: router-level require_scope("control")
(household posture -- an 'agent' never manages routines) plus per-write `write_deps`
(require_local CSRF). Nothing here actuates on its own: a routine is inert until it is
BOTH created and enabled, and its actions still pass every call_ha_service gate.

  * GET    /api/routines                 -> list all routines
  * POST   /api/routines                 -> create (starts DISABLED unless enabled=true)
  * PUT    /api/routines/{id}            -> update trigger/actions/name
  * POST   /api/routines/{id}/enable     -> {on: bool} enable/disable
  * DELETE /api/routines/{id}            -> remove
  * POST   /api/routines/{id}/test       -> run this routine's actions NOW (real actuation),
                                            return the executor status -- the "test it" button
  * GET    /api/routines/ha-entities     -> the actuatable Home Assistant entities (light/
                                            switch/...) for the action picker
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException


def build_routines_router(store, run_test=None, ha_entities_fn=None,
                          write_deps=None) -> APIRouter:
    """`store` = RoutineStore. `run_test(actions) -> awaitable[str]` runs a routine's
    actions once for the test button (injected by app.py so it uses the real gated
    executor off the event loop). `ha_entities_fn() -> list[dict]` lists actuatable HA
    entities for the picker (injected; [] when HA isn't configured)."""
    router = APIRouter()
    wdeps = write_deps or []

    @router.get("/api/routines")
    async def list_routines():
        return {"routines": store.list()}

    @router.get("/api/routines/ha-entities")
    async def ha_entities():
        # Best-effort: the picker's list of lights/switches/... to build an action on.
        # Empty when Home Assistant isn't configured -- the UI then lets the user type an
        # entity id. Never raises: an HA outage must not break the routines screen.
        return {"entities": ha_entities_fn() if ha_entities_fn else []}

    @router.post("/api/routines", dependencies=wdeps)
    async def create(name: str = Body(...), trigger_kind: str = Body(...),
                     trigger_params: dict = Body(default={}),
                     actions: list = Body(...),
                     condition: dict | None = Body(default=None),
                     enabled: bool = Body(default=False)):
        try:
            return store.add(name, trigger_kind, trigger_params=trigger_params,
                             actions=actions, condition=condition, enabled=enabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.put("/api/routines/{rid}", dependencies=wdeps)
    async def update(rid: str, name: str = Body(...), trigger_kind: str = Body(...),
                     trigger_params: dict = Body(default={}),
                     actions: list = Body(...),
                     condition: dict | None = Body(default=None)):
        try:
            r = store.update(rid, name, trigger_kind, trigger_params, actions,
                             condition=condition)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if r is None:
            raise HTTPException(status_code=404, detail="routine not found")
        return r

    @router.post("/api/routines/{rid}/enable", dependencies=wdeps)
    async def set_enabled(rid: str, on: bool = Body(..., embed=True)):
        if not store.set_enabled(rid, on):
            raise HTTPException(status_code=404, detail="routine not found")
        return {"id": rid, "enabled": on}

    @router.delete("/api/routines/{rid}", dependencies=wdeps)
    async def delete(rid: str):
        if not store.delete(rid):
            raise HTTPException(status_code=404, detail="routine not found")
        return {"deleted": rid}

    @router.post("/api/routines/{rid}/test", dependencies=wdeps)
    async def test(rid: str):
        # Run this routine's actions once, NOW, and report what happened -- the "see it
        # work" button. Real actuation (a light really turns on), so it is a write and
        # goes through the same gated executor a fired routine uses. The trigger is NOT
        # evaluated; only the actions run.
        r = store.get(rid)
        if r is None:
            raise HTTPException(status_code=404, detail="routine not found")
        if run_test is None:
            raise HTTPException(status_code=503, detail="routine execution not available")
        status = await run_test(r["actions"])
        return {"id": rid, "status": status}

    return router
