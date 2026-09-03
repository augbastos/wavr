"""Recording, sharing and replaying sensor traces.

Transport plus the recorder's lifecycle. Everything about what a trace IS lives
in `trace.py`.

Two things here are deliberate and would be wrong the other way:

**Recording is OFF by default and has to be started explicitly.** A trace is a
minute-by-minute record of where people were in a home. It is the most sensitive
artefact Wavr can produce, and it must never accumulate because somebody left a
switch on months ago.

**Download is sanitised by default.** The raw form is available, and asking for
it is a separate, deliberate act with the word `raw` in it — so a trace attached
to a bug report is safe unless somebody went out of their way.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.trace import TraceError, TraceRecorder, replay, sanitize, summarize


async def _deps_not_wired():
    raise HTTPException(status_code=403,
                        detail="trace routes have no auth gate wired")


def build_trace_router(state, engine_factory=None, deps=None,
                       max_events: int = 20_000) -> APIRouter:
    """`state` is a mutable dict the app also reads from, so the ingest path can
    check "are we recording" without importing this module."""
    router = APIRouter(dependencies=deps if deps is not None else [Depends(_deps_not_wired)])

    @router.post("/api/trace/start")
    async def start(label: str = Body("", embed=True)):
        """Begin recording. Replaces any recording already running.

        Replacing rather than refusing: an operator who forgot they were
        recording wants the new one, and the old one was accumulating a movement
        log they did not intend.
        """
        state["recorder"] = TraceRecorder(label=label[:120],
                                          max_events=max_events)
        return {"recording": True, "label": label[:120],
                "max_events": max_events,
                "note": ("Recording captures every sensor reading, including "
                         "where people are. Stop it when you are done.")}

    @router.post("/api/trace/stop")
    async def stop():
        rec = state.get("recorder")
        if rec is None:
            return {"recording": False, "captured": 0}
        state["recorder"] = None
        state["last"] = rec.to_dict()
        return {"recording": False, **summarize(state["last"])}

    @router.get("/api/trace/status")
    async def status():
        rec = state.get("recorder")
        return {
            "recording": rec is not None,
            "captured": len(rec) if rec is not None else 0,
            "label": rec.label if rec is not None else "",
            "has_saved_trace": state.get("last") is not None,
        }

    @router.get("/api/trace/download")
    async def download(raw: bool = False, drop_positions: bool = False):
        """The last stopped recording.

        Sanitised unless `raw=true` is asked for explicitly — so a trace attached
        to a bug report carries no person unless somebody deliberately made it.
        """
        trace = state.get("last")
        if trace is None:
            raise HTTPException(status_code=404,
                                detail="no recording has been stopped yet")
        if raw:
            return trace
        return sanitize(trace, drop_positions=drop_positions)

    @router.post("/api/trace/replay")
    async def replay_trace(trace: dict = Body(...)):
        """Run a trace through a fresh engine and return what it produced.

        The fresh engine is the point: replaying into the LIVE one would inject
        a recording of the past into the present and make the dashboard report a
        house that no longer exists.
        """
        if engine_factory is None:
            raise HTTPException(status_code=503,
                                detail="replay is not available on this Core")
        try:
            states = replay(trace, engine_factory)
        except TraceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:      # noqa: BLE001 -- an uploaded trace is untrusted input
            raise HTTPException(
                status_code=422,
                detail=f"this trace could not be replayed: {type(exc).__name__}"
            ) from None
        return {"states": [s.to_dict() for s in states],
                "summary": summarize(trace)}

    return router
