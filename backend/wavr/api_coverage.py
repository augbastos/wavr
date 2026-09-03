"""Sensor coverage over HTTP.

Its own module rather than another block in `app.py`, per the structural rule
this pass adopted: a new domain gets a router file, `app.py` only wires it. The
domain logic lives in `sensor_coverage.py` and none of it is duplicated here —
this file is transport, and if it starts making judgements about health or
precision, the judgement is in the wrong place.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from wavr.sensor_coverage import summarize


async def _deps_not_wired():
    """Fail CLOSED when the caller forgot the auth gate.

    Same shape as the other routers here (`_admin_deps_not_wired` in
    api_nodes.py). Coverage names every room and every sensor in the house, so a
    default of "open" would be a disclosure, not a convenience.
    """
    raise HTTPException(status_code=403,
                        detail="coverage route has no auth gate wired")


def build_coverage_router(coverage_fn=None, rooms_fn=None, deps=None) -> APIRouter:
    """`GET /api/coverage` — what this Core can see, and where it cannot.

    `coverage_fn` returns the list of `SensorCoverage`; `rooms_fn` the room names
    fusion knows about. Both are injected so this is testable without an app and
    so `app.py` stays a wiring file.
    """
    router = APIRouter()
    deps = list(deps) if deps else [Depends(_deps_not_wired)]

    @router.get("/api/coverage", dependencies=deps)
    async def coverage():
        if coverage_fn is None:
            # Honest unavailability, not an empty house. An empty `rooms` list
            # with no flag reads as "nothing is installed".
            return {"rooms": [], "uncovered": [], "house_wide": [],
                    "available": False,
                    "note": "This Wavr build cannot enumerate its sensors."}
        rooms = []
        if rooms_fn is not None:
            try:
                rooms = list(rooms_fn() or [])
            except Exception:      # noqa: BLE001 — an unreadable room list must
                rooms = []         # not hide the sensors that ARE readable
        body = summarize(rooms, coverage_fn())
        body["available"] = True
        return body

    return router
