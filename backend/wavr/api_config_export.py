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
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.config_export import (
    TransferError, audit, check_import, diagnostic_bundle, export_config,
    preview_import,
)


def build_router(*, gather_config, gather_bundle, existing_rooms_fn,
                 existing_anchors_fn, require_local, require_scope) -> APIRouter:
    router = APIRouter()

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
        body = diagnostic_bundle(config=export_config(**gather_config()),
                                 **gather_bundle())
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
                                  existing_anchors=existing_anchors_fn())
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

    return router
