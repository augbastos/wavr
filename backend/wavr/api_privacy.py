"""The privacy posture, and what Wavr actually stores.

Transport only; `data_inventory.py` holds the answer.

Read-only by construction. This surface exists to let somebody check what Wavr
keeps, and a route that could also CHANGE it would turn an audit screen into an
attack surface — the controls that change privacy behaviour already live in
Settings, gated separately.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from wavr.data_inventory import inventory
from wavr.provider_catalog import build_registry


async def _deps_not_wired():
    raise HTTPException(status_code=403,
                        detail="privacy routes have no auth gate wired")


def build_privacy_router(db_path: str = "", posture_fn=None, registry=None,
                         deps=None) -> APIRouter:
    """`posture_fn() -> dict` reports the live switches (LAN access, cameras,
    external connectors). Injected so this module reads no config.

    `registry` is the provider catalog — what this Wavr COULD talk to, which is
    a different question from what is switched on and belongs on a privacy
    screen for a different reason: somebody deciding whether to enable something
    needs to know where it reaches before they enable it, not after.
    """
    router = APIRouter(dependencies=deps if deps is not None else [Depends(_deps_not_wired)])
    _registry = registry if registry is not None else build_registry()

    @router.get("/api/privacy/data")
    async def stored_data():
        """Every category of thing Wavr keeps, in plain language, with counts.

        Including — deliberately, and first — the things it never keeps at all.
        "Camera frames: never written anywhere" is the strongest statement this
        product can make about itself, and it is worth nothing if a person
        cannot find it.
        """
        return inventory(db_path)

    @router.get("/api/privacy/posture")
    async def posture():
        """The one-glance answer to "what is this thing doing right now".

        `available: false` rather than a reassuring default when the live state
        cannot be read: a privacy screen that guesses in the comfortable
        direction is worse than one that admits it does not know.
        """
        if posture_fn is None:
            return {"available": False,
                    "note": "This Wavr build cannot report its live posture."}
        try:
            body = posture_fn()
        except Exception:      # noqa: BLE001
            return {"available": False,
                    "note": "Wavr could not read its own settings just now. "
                            "This is not a statement that nothing is enabled."}
        return {**body, "available": True}

    @router.get("/api/privacy/providers")
    async def providers():
        """Everything Wavr can take spatial evidence from, grouped by how far
        each one reaches.

        Grouped by reach rather than by kind because that is the axis somebody
        worried about privacy actually sorts on. Capabilities, never state — a
        provider appearing here says nothing about whether it is enabled.
        """
        return _registry.catalog()

    return router
