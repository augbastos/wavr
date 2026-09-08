"""Downloading a configuration or a diagnostic bundle.

Local + admin on every route. These files describe the whole Space, and the
import replaces a floor plan — hours of somebody's work, and not easily undone.

## The audit is a GATE, not a report

`config_export.audit` runs on the finished document, and a non-empty result
REFUSES the download with a 500. That is deliberate and it is the opposite of
the usual arrangement, where a scanner logs a warning nobody reads.

The reasoning: the allowlists are only as good as the last person who added a
field, and the failure they guard against is a credential sitting in a file that
somebody has already emailed. A refused download is an obvious bug that gets
fixed on the spot. A leaked one is discovered by whoever received it.

## Network reachability rides along, off the event loop

Two questions, both from `wavr.lan_reachability`, neither answerable by
anything else in this bundle:

  * **Is the socket even on the LAN?** `serves_the_lan()` / `bound_host()`
    read what the launcher actually told uvicorn to bind to — in-memory,
    instant, no I/O. A Core "Let other devices connect" turned on but still
    listening on loopback is a real, already-found failure mode, and it looks
    identical to a firewall block from a phone's point of view: silence.
  * **If it is, would the firewall let a packet through?** `check()` shells
    out to `netsh` (~1.5s warm), so it is dispatched through
    `asyncio.to_thread` rather than awaited on the event loop directly — the
    same "on demand, off the hot path" rule its own docstring states.

`program`/`port` are resolved from `lan_reachability` itself at call time
(`sys.executable`, `bound_port()`) rather than threaded through from the
caller: the launcher (`serve.py`) already calls `note_bound_host` before this
Core accepts a request, so nothing here needs app.py to hand over a port it
would otherwise have to go and find. `reachability_fn` stays injectable so a
test never shells out to `netsh`.
"""
from __future__ import annotations

import asyncio
import sys

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr import lan_reachability
from wavr.config_export import (
    TransferError, audit, check_import, diagnostic_bundle, export_config,
    plan_import, preview_import,
)


def build_router(*, gather_config, gather_bundle, existing_rooms_fn,
                 existing_anchors_fn, require_local, require_scope,
                 apply_fn=None, validate_house=None,
                 reachability_fn=None) -> APIRouter:
    router = APIRouter()
    reachability_fn = reachability_fn or lan_reachability.check

    async def _network_reachability() -> dict:
        """Never raises, never blocks the loop. A failed check degrades to
        the same honest UNKNOWN a Linux Core or a query timeout already
        produces — see lan_reachability.Reachability's own default."""
        served = False
        host = ""
        try:
            served = lan_reachability.serves_the_lan()
            host = lan_reachability.bound_host()
        except Exception:                     # noqa: BLE001
            pass
        try:
            program = sys.executable or ""
            port = lan_reachability.bound_port()
            result = await asyncio.to_thread(reachability_fn, program, port)
            out = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        except Exception:                     # noqa: BLE001
            out = {"state": "unknown", "reason": "the reachability check "
                   "itself failed", "rules": [], "checked": False}
        out["serves_the_lan"] = served
        out["bound_host"] = host
        return out

    def _checked(document: dict, what: str) -> dict:
        leaks = audit(document)
        if leaks:
            # Refused, loudly. See the module docstring: this is a gate.
            raise HTTPException(
                status_code=500,
                detail=(f"Wavr refused to build this {what}: something that "
                        f"looks like a credential reached it at {leaks}. This "
                        f"is a bug in the exporter, not in your setup — please "
                        f"report it rather than working around it."))
        return document

    @router.get("/api/config/export")
    async def export(_=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        """Everything needed to rebuild this Space somewhere else.

        No password, token or camera URL, and nobody's name. It describes a
        building.
        """
        return _checked(export_config(**gather_config()), "export")

    @router.get("/api/diagnostics/bundle")
    async def bundle(_=Depends(require_local),
                     __=Depends(require_scope("admin"))):
        """What Wavr can see, for somebody trying to help.

        Sensor health, coverage and the semantic event stream. Not the room
        state history: "the kitchen was occupied at 23:40" is a fact about
        somebody's evening, and a support bundle ends up in a ticket system.
        """
        # Read once, used twice: `export_config` gets the whole gathered dict
        # as before, and `space` (the RAW row, carrying `space_id`) goes to
        # `diagnostic_bundle` separately so it can be fingerprinted without
        # ever being exported itself — see config_export._space_fingerprint.
        gathered = gather_config()
        body = diagnostic_bundle(config=export_config(**gathered),
                                 space=gathered.get("space"),
                                 **gather_bundle())
        # Real I/O, added AFTER the pure bundle is built and BEFORE the audit
        # gate below — so a firewall rule NAME (the one thing this check could
        # ever surface that looks like it came from the machine rather than
        # the Space) still passes through the same credential check as
        # everything else here, rather than riding in on a side door.
        body["network_reachability"] = await _network_reachability()
        return _checked(body, "bundle")

    @router.post("/api/config/preview")
    async def preview(config: dict = Body(..., embed=True),
                      _=Depends(require_local),
                      __=Depends(require_scope("admin"))):
        """What importing would change. Nothing is written.

        A separate step because an import replaces the floor plan, and "it
        replaced my rooms" is the complaint this exists to prevent.
        """
        try:
            return preview_import(config, existing_rooms=existing_rooms_fn(),
                                  existing_anchors=existing_anchors_fn(),
                                  validate_house=validate_house)
        except TransferError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/api/config/validate")
    async def validate(config: dict = Body(..., embed=True),
                       _=Depends(require_local),
                       __=Depends(require_scope("admin"))):
        """Whether Wavr can read this file at all, without a Space to compare to.

        Separate from the preview because a file that will not parse and a file
        that would replace three rooms are different problems, and one answer
        for both would bury the second.
        """
        try:
            body = check_import(config)
        except TransferError as exc:
            return {"readable": False, "error": str(exc)}
        return {"readable": True,
                "format_version": body.get("wavr_config_version"),
                "exported_at": body.get("exported_at", ""),
                "secrets_needed": body.get("secrets_you_must_supply_again", [])}

    @router.post("/api/config/import")
    async def apply(config: dict = Body(..., embed=True),
                    confirm_digest: str = Body("", embed=True),
                    _=Depends(require_local),
                    __=Depends(require_scope("admin"))):
        """Actually write it. The other half of an export that had only halves.

        Export, preview and validate all existed; nothing applied anything, so
        the feature whose whole purpose is "the Pi died and I want my Space
        back" stopped one step short of giving it back. An API with no workflow
        is not a feature.

        **`confirm_digest` is required and must match the preview.** An import
        replaces a floor plan that took somebody an evening to draw, and preview
        and apply are two requests: without this an operator can be shown the
        consequences of one file and apply another. The error names the mismatch
        rather than applying anything.
        """
        if apply_fn is None:
            raise HTTPException(
                status_code=501,
                detail="This Core was built without an import writer.")
        try:
            plan = plan_import(config, existing_rooms=existing_rooms_fn(),
                               existing_anchors=existing_anchors_fn(),
                               validate_house=validate_house)
        except TransferError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not confirm_digest:
            raise HTTPException(
                status_code=400,
                detail=("Preview this file first and send back its `digest` as "
                        "`confirm_digest`. Importing replaces your floor plan."))
        if confirm_digest != plan["digest"]:
            raise HTTPException(
                status_code=409,
                detail=("This is not the file you previewed. Preview it again "
                        "and confirm with the digest that preview returns."))
        return apply_fn(plan)

    return router
