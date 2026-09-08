"""Developer Mode: how somebody explores Wavr before building on it.

This is not the normal user's interface, and keeping the two apart is a product
requirement rather than a matter of taste. A household setting up a camera should
never encounter the word "manifest"; a developer needs the manifest, the raw
event stream, the provider catalogue and a way to make a house do something
without owning a radar. Serving both from one screen makes the first audience
feel they are using a tool for somebody else, which is how a spatial product
turns into a developer product nobody else installs.

## The gate is a switch, not a secret

Every route here is local + admin, exactly like the other configuration
surfaces — the switch decides whether the UI SHOWS them, not whether they are
protected. A hidden-but-open route is the worst of both: invisible to the person
who owns the box and reachable by anything that guesses the path.

## The simulator is the load-bearing feature

Everything else here is a window onto state that already exists. Scenarios are
the part that changes what is possible: they let somebody develop an application
against a house that becomes occupied, loses its count, and contradicts itself,
without owning any of the hardware that would otherwise be required.

Every event a scenario produces carries a `sim:` sensor id that travels through
fusion into `sources[]`, into coverage, into the event stream and onto the
dashboard — so a simulated room can never be mistaken for a real one, at any
surface, by anybody.
"""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr import simulator
from wavr.contracts import version as contract_version
from wavr.experience_manifest import ManifestError, parse, validate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _restamp(step, ts: datetime):
    """The same step, stamped when it is actually being fed in.

    A scenario's steps carry `simulator.EPOCH + at_s`, a fixed 2026-01-01, so
    two runs of the same story produce identical timestamps — which is what
    makes a scenario assertable, and is right for the SCENARIO.

    It is wrong for the ENGINE. Production's FusionEngine ages evidence against
    the wall clock, so every simulated reading arrived roughly eight months
    stale: `health: "dead"`, mass 0.0, and the room stayed vacant at 0%
    confidence. The whole "run a scenario and watch Wavr react" onboarding
    produced a house where nothing ever happened, and the test that covered it
    passed because its fixture built a FusionEngine with an injected clock that
    production never uses.

    So the timeline is preserved and MOVED: `at_s` still says where a step sits
    in the story, and `ts` says when that moment is arriving here.
    """
    return dataclasses.replace(
        step, event=dataclasses.replace(step.event, ts=ts.isoformat()))

# What a developer is told this Core speaks. Collected in one response rather
# than left to be discovered a 404 at a time.
#
# READ from `contracts`, never written out here. This table held literals until
# a version genuinely moved, and then it reported the OLD number for a shape the
# Core no longer speaks -- to the one endpoint whose entire job is telling a
# developer what to build against. `test_contracts` caught it; it is the reason
# that test refuses a literal anywhere near a producer.
DEVELOPER_PROTOCOL = {
    name: contract_version(name)
    for name in ("experience_context", "spatial_events", "experience_manifest",
                 "anchors", "trace")
}


def build_router(*, enabled_fn, providers_fn, source_health_fn, anchors_fn,
                 rooms_fn, fusion_rooms_fn, room_state_fn, ingest_fn,
                 experiences_fn, require_local, require_scope) -> APIRouter:
    """Routes for Developer Mode.

    `enabled_fn` is read per request, not captured at build time: the switch is a
    setting an operator can flip while Wavr is running, and a router that decided
    at startup would need a restart to obey it.
    """
    router = APIRouter()

    def _require_developer_mode():
        if not enabled_fn():
            # 403 with a sentence naming the switch. A bare 404 would send a
            # developer looking for a Core version that has the feature.
            raise HTTPException(
                status_code=403,
                detail=("Developer mode is off. Turn it on in Settings — it is "
                        "a switch, not a build flag."))

    @router.get("/api/dev/status")
    async def status(_=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        """Everything a developer needs to know before writing a line.

        Deliberately one response. Discovering a platform one endpoint at a time
        is how people conclude a capability does not exist.
        """
        _require_developer_mode()
        rooms = list(fusion_rooms_fn())
        simulated = []
        for room in rooms:
            state = room_state_fn(room) or {}
            for src in state.get("sources") or []:
                if simulator.is_simulated(str(src.get("sensor_id") or "")):
                    simulated.append(room)
                    break
        return {
            "protocol": DEVELOPER_PROTOCOL,
            "rooms": rooms_fn(),
            "rooms_with_state": rooms,
            # Live rather than a flag somebody has to remember to clear: a room
            # is "simulated" for exactly as long as simulated evidence is still
            # voting in it, which is a fact about fusion and not about a run.
            "simulated_rooms": sorted(set(simulated)),
            "endpoints": {
                "context": "/api/experience/context",
                "room_context": "/api/experience/context/{room}",
                "compatibility": "/api/experience/compatibility",
                "scopes": "/api/experience/scopes",
                "anchors": "/api/anchors",
                "events_tail": "/api/events/recent",
                "events_stream": "/ws/events",
                "coverage": "/api/coverage",
            },
            "sdks": {
                "javascript": "sdk/javascript/wavr.js",
                "python": "sdk/python/wavr_sdk",
                "kotlin": "dev.wavr.sdk (core-launcher/app)",
            },
            # Reachable only when this Core actually SHIPS them. A packaged
            # build may not, and a list of links that 404 is worse than an
            # honest empty one.
            "experiences": experiences_fn(),
            "note": ("Simulated evidence is labelled at the source with a "
                     "`sim:` sensor id and stays labelled everywhere. If a room "
                     "appears in `simulated_rooms`, nothing it is currently "
                     "saying came from a real sensor."),
        }

    @router.get("/api/dev/providers")
    async def providers(_=Depends(require_local),
                        __=Depends(require_scope("admin"))):
        """What this Core can take spatial evidence from, and how each is doing.

        The catalogue and the health in one response, because "what could I use"
        and "is it working" are the same question when you are debugging.
        """
        _require_developer_mode()
        return {**providers_fn(), "health": source_health_fn()}

    @router.post("/api/dev/manifest/validate")
    # `embed=True` because a single Body parameter would otherwise take the
    # WHOLE request body as the manifest, and `/api/experience/compatibility`
    # (two parameters, so embedded automatically) expects `{"manifest": ...}`.
    # Two routes taking the same document in two different envelopes is the kind
    # of inconsistency a developer discovers as a confusing validation error
    # about a field they did not write.
    async def validate_manifest(manifest: dict = Body(..., embed=True),
                                _=Depends(require_local),
                                __=Depends(require_scope("admin"))):
        """Check an experience manifest without evaluating it against a Space.

        Separate from `/api/experience/compatibility` because they answer
        different questions: this one is "is my document correct", that one is
        "will it work here". A developer with a typo needs the first, and getting
        UNSUPPORTED for a house-shaped reason would send them to the wrong place.
        """
        _require_developer_mode()
        try:
            spec = parse(manifest)
        except ManifestError as exc:
            return {"valid": False, "error": str(exc), "warnings": []}
        return {"valid": True, "manifest": spec.to_dict(),
                "warnings": validate(spec),
                "note": ("Valid means the document parses and its fields are "
                         "known. Whether a Space can support it is a different "
                         "question — ask /api/experience/compatibility.")}

    @router.get("/api/dev/scenarios")
    async def scenarios(_=Depends(require_local),
                        __=Depends(require_scope("admin"))):
        """Scenarios that make a house do something, without the house."""
        _require_developer_mode()
        body = simulator.list_scenarios()
        for row in body["scenarios"]:
            scenario = simulator.get(row["key"])
            row["rooms"] = simulator.rooms_touched(scenario)
            row["sensors"] = simulator.sensors_used(scenario)
        return body

    @router.post("/api/dev/scenarios/{key}/run")
    async def run_scenario(key: str, realtime: bool = Body(False, embed=True),
                           _=Depends(require_local),
                           __=Depends(require_scope("admin"))):
        """Play a scenario into this Core's own fusion engine.

        `realtime: false` (the default) feeds every step at once and returns the
        resulting room states — deterministic, assertable, and the mode a test
        uses. `realtime: true` schedules the steps on their own timeline so a
        developer can watch the dashboard react, which is the mode a person uses.

        Both write into the REAL engine. There is no parallel simulated engine,
        because a simulator running against a different fusion than production
        would eventually disagree with it, and the disagreement would be
        discovered by somebody's application rather than by us.
        """
        _require_developer_mode()
        scenario = simulator.get(key)
        if scenario is None:
            raise HTTPException(status_code=404, detail=f"no scenario {key!r}")

        # Into THIS Space's rooms. A scenario names `kitchen`; a real house might
        # call it `sala`, and running the scenario unchanged grows a room nobody
        # has on their floor plan. The mapping is published rather than applied
        # quietly — a silent rename leaves somebody watching the wrong card.
        scenario, mapping = simulator.remap(scenario, rooms_fn())

        run = simulator.SimulationRun(scenario)
        if realtime:
            asyncio.get_running_loop().create_task(_play(run))
            return {"scenario": key, "realtime": True,
                    "rooms": simulator.rooms_touched(scenario),
                    "room_mapping": mapping,
                    "duration_s": scenario.duration_s,
                    "note": ("Running on its own timeline. Watch the dashboard, "
                             "or /ws/events.")}

        # Instant replay: the whole story is fed at once, so it is stamped so
        # that its LAST moment is now and the earlier ones sit behind it by
        # exactly the gaps the scenario describes. Feeding it stamped in
        # January instead is what made every scenario produce a vacant house.
        #
        # Ending at `now` rather than starting there matters: the state a
        # caller asserts on is the state at the END of the story, and that one
        # has to be FRESH or fusion has already decayed it before the response
        # is written. Earlier steps ageing out is not a bug — a house really
        # would have aged them.
        end = _now()
        steps = []
        while True:
            step = run.next_step()
            if step is None:
                break
            at = end - timedelta(seconds=max(0.0, scenario.duration_s - step.at_s))
            await ingest_fn(_restamp(step, at).event)
            steps.append(step.to_dict())
        return {
            "scenario": key, "realtime": False, "steps": steps,
            "room_mapping": mapping,
            "rooms": {room: room_state_fn(room)
                      for room in simulator.rooms_touched(scenario)},
            "note": ("Every source in these rooms now carries a `sim:` sensor "
                     "id. They decay out of fusion like any other evidence — "
                     "there is nothing to switch off."),
        }

    async def _play(run: simulator.SimulationRun) -> None:
        """Feed a scenario on its own timeline.

        Sleeps the GAP between steps rather than to an absolute schedule, so a
        slow ingest delays the rest instead of causing a burst that arrives all
        at once and produces a story no house could have told.
        """
        previous = 0.0
        while True:
            step = run.next_step()
            if step is None:
                return
            gap = max(0.0, step.at_s - previous)
            previous = step.at_s
            if gap:
                await asyncio.sleep(gap)
            try:
                # Stamped NOW, not at the scenario's fixed epoch. This is the
                # mode a person watches, and evidence stamped eight months ago
                # is dead on arrival — the dashboard sat still through every
                # scenario and looked like the feature did nothing.
                await ingest_fn(_restamp(step, _now()).event)
            except Exception:      # noqa: BLE001 -- a scenario must not be able
                return             # to take the Core down; stop the run instead

    @router.get("/api/dev/anchors")
    async def anchors(_=Depends(require_local),
                      __=Depends(require_scope("admin"))):
        """Anchors with their external mappings, for an integration developer.

        The same data `/api/anchors` serves, kept here as well because the
        developer view is where somebody debugging an ARKit binding will look,
        and sending them to the application-facing route to find it is the kind
        of small friction that makes a platform feel unfinished.
        """
        _require_developer_mode()
        return anchors_fn()

    return router
