"""FastAPI router for the single 'Connectors & Services' egress surface
(project_wavr_connectors_vision). Mirrors api_identity.py: a small injectable
factory built around ConnectorStore so it stays testable.

Routes (all gated in app.py -- router-level central/root, same as the identity +
device-management routes; the state-changing enable additionally carries
require_local CSRF):

  * GET  /api/connectors          -> built-in + generic connectors with live state
  * GET  /api/connectors/catalog  -> the built-in descriptors only ("what CAN plug in")
  * POST /api/connectors/{id}/enable {enabled: bool} -> the revocable toggle

`catalog_fn` is a live callable () -> list[builtin-descriptor] supplied by app.py; it
computes each built-in's available/env_active/active/enforcement from cfg + the store
overlay, so this router carries NO cfg knowledge and cannot fork a gate. The enable
route reads the descriptor's `enforcement` to decide whether the toggle has teeth
(registry-overlay/registry) or is env-only (409 with the exact env var to edit)."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from wavr.connector_store import BUILTIN_IDS


def _generic_descriptor(row: dict) -> dict:
    """Shape a kind='generic' store row as a connector descriptor. `active` is the
    full gate (enabled==1); the registry IS the enforcing gate for generics."""
    return {
        "id": row["id"],
        "kind": "generic",
        "direction": "outbound",
        "label": row["label"],
        "available": True,
        "active": row["enabled"] == 1,
        "suppressed": False,
        "enforcement": "registry",
        "scope": row["scope"],
        "env_flag": None,
    }


def build_connectors_router(store, catalog_fn, write_deps=None) -> APIRouter:
    """`store` -- ConnectorStore. `catalog_fn` -- () -> list of built-in descriptors
    computed live from cfg + store overlay. `write_deps` -- FastAPI deps applied to
    the state-changing enable route only (require_local CSRF); the GET reads carry no
    CSRF (but are still router-level central-gated in app.py)."""
    router = APIRouter()
    wdeps = list(write_deps or [])

    def _all_connectors() -> list[dict]:
        builtins = list(catalog_fn())
        generics = [_generic_descriptor(r) for r in store.list()
                    if r["kind"] == "generic"]
        return builtins + generics

    @router.get("/api/connectors")
    async def list_connectors():
        return {"connectors": _all_connectors()}

    @router.get("/api/connectors/catalog")
    async def catalog():
        # The static "what CAN plug in", decoupled from generics -- pure cfg read.
        return {"catalog": list(catalog_fn())}

    @router.post("/api/connectors/{id}/enable", dependencies=wdeps)
    async def enable(id: str, enabled: bool = Body(..., embed=True)):
        # Built-in? Look it up in the live catalog (never trust a client-supplied
        # kind) and act by its declared enforcement.
        if id in BUILTIN_IDS:
            desc = next((d for d in catalog_fn() if d["id"] == id), None)
            if desc is None:                       # configured out of the catalog
                raise HTTPException(status_code=404, detail=f"unknown connector: {id}")
            if desc["enforcement"] == "env":
                env_flag = desc.get("env_flag") or "its environment flag"
                raise HTTPException(
                    status_code=409,
                    detail=(f"connector '{id}' is controlled by environment flag "
                            f"{env_flag}; edit config and restart"))
            # registry-overlay: a row can only SUPPRESS (kill-switch). Ensure the row
            # exists (label/scope from the catalog, never client input) then set the
            # bit. enabled=True merely clears suppression; the chokepoint still ANDs
            # the env flag, so this can NEVER enable egress beyond env.
            store.upsert(id, "builtin", desc["label"], scope=desc.get("scope"))
            store.set_enabled(id, enabled)
            updated = next((d for d in catalog_fn() if d["id"] == id), None)
            return {"connector": updated}
        # Generic: the registry is the full gate.
        row = store.get(id)
        if row is None or row["kind"] != "generic":
            raise HTTPException(status_code=404, detail=f"unknown connector: {id}")
        store.set_enabled(id, enabled)
        return {"connector": _generic_descriptor(store.get(id))}

    return router
