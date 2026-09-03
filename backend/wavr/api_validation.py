"""Guided validation and reliability over HTTP.

Transport only. Every judgement about a sensor lives in `validation.py` and
`reliability.py`; if this file starts deciding what "good" means, the judgement
is in the wrong place and two surfaces will disagree about the same sensor.

Gated like administration: a validation walk writes what Wavr will trust from
then on, which is a configuration act, not a read.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.validation import ValidationError


async def _deps_not_wired():
    """Fail CLOSED when the caller forgot the gate — the house rule for every
    router here. A walk rewrites the weights fusion applies; it may not be
    reachable by default."""
    raise HTTPException(status_code=403,
                        detail="validation routes have no auth gate wired")


def _bad(exc: ValidationError, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


def build_validation_router(session=None, reliability=None, store=None,
                            deps=None) -> APIRouter:
    """The guided walk, plus what it has taught Wavr so far."""
    router = APIRouter(dependencies=deps if deps is not None else [Depends(_deps_not_wired)])

    def _need_session():
        if session is None:
            raise HTTPException(
                status_code=503,
                detail="Guided validation is not available on this Core.")
        return session

    @router.post("/api/validation/start")
    async def start(room: str = Body(..., embed=True)):
        """Begin, or resume, a walk in one room.

        Resuming rather than starting a second session is deliberate: two live
        sessions for one room would each grade half the walk and neither would
        be right.
        """
        try:
            return _need_session().start(room)
        except ValidationError as exc:
            raise _bad(exc, 422) from None

    @router.post("/api/validation/{session_id}/declare")
    async def declare(session_id: str, occupied: bool = Body(...),
                      count: int | None = Body(None)):
        """The operator states what is true right now."""
        try:
            return _need_session().declare(session_id, occupied, count)
        except ValidationError as exc:
            raise _bad(exc, 409) from None

    @router.post("/api/validation/{session_id}/sample")
    async def sample(session_id: str):
        """One check of every sensor against the declared truth.

        Called on a loop by the UI while the operator holds a position — that
        repetition is what makes response time measurable, not just accuracy.
        """
        try:
            return _need_session().sample(session_id)
        except ValidationError as exc:
            raise _bad(exc, 409) from None

    @router.post("/api/validation/{session_id}/finish")
    async def finish(session_id: str):
        """Close the walk, write what was learned, return it in sentences."""
        try:
            return _need_session().finish(session_id)
        except ValidationError as exc:
            raise _bad(exc, 409) from None

    @router.post("/api/validation/{session_id}/abandon")
    async def abandon(session_id: str):
        """Walk away. Nothing is recorded — a session somebody gave up on is
        not evidence and must not be able to demote a sensor."""
        try:
            return _need_session().abandon(session_id)
        except ValidationError as exc:
            raise _bad(exc, 409) from None

    @router.get("/api/validation/history")
    async def history(room: str = "", limit: int = 20):
        if store is None:
            return {"sessions": [], "available": False}
        return {"sessions": store.history(room, max(1, min(int(limit), 100))),
                "available": True}

    @router.get("/api/reliability")
    async def profiles(sensor_id: str = ""):
        """What each sensor has earned, with the counts behind it.

        `available: false` rather than an empty list when reliability is not
        wired: "no sensor has a record" and "this Core cannot tell you" are
        different answers, and the second must never render as the first.
        """
        if reliability is None:
            return {"profiles": [], "available": False,
                    "note": "This Wavr build does not track sensor reliability."}
        rows = [p.to_dict() for p in reliability.list_profiles(sensor_id)]
        return {"profiles": rows, "available": True,
                "note": ("A sensor with no measurements is treated at full "
                         "trust, never penalised for being new.")}

    @router.delete("/api/reliability/{sensor_id}")
    async def forget(sensor_id: str):
        """Clear a sensor's record — for when it is physically moved.

        A camera that earned its reputation in the hall has earned nothing in
        the kitchen, and carrying the numbers over would be a confident claim
        about a place it has never seen.
        """
        if reliability is None:
            raise HTTPException(status_code=503,
                                detail="reliability is not available on this Core")
        return {"sensor_id": sensor_id, "forgotten": reliability.forget(sensor_id)}

    return router
