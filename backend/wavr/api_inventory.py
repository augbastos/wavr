"""Wavr Net HTTP surface: the running inventory + rogue alerts (read-only),
plus the one state-changing write this module owns (naming a device).

`build_inventory_router(service, device_meta=None, name_deps=None)` returns a
FastAPI APIRouter exposing:
  * GET /api/inventory       -- the running inventory. Each device dict is
                                 merged with persisted metadata (`name`,
                                 `first_seen`, `last_seen`) when `device_meta`
                                 is given -- all three are None otherwise.
  * GET /api/alerts          -- rogue-device alerts (unchanged).
  * PUT /api/inventory/name  -- {mac, name} -> persists a friendly device name
                                 and returns the updated {mac, name, first_seen,
                                 last_seen} entry. Only registered when
                                 `device_meta` is given; gated by `name_deps`
                                 (the app's require_local CSRF guard), same
                                 rule as every other state-changing route
                                 (mirrors build_devices_router's `delete_deps`).

GETs need no CSRF header -- the app's global loopback-only middleware is the
load-bearing access control, same as before.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from wavr.device_meta import DeviceMeta, normalize_mac
from wavr.netinventory_service import NetworkInventoryService


def _device_view(d, device_meta: DeviceMeta | None = None) -> dict:
    """Trim a Device to the fields the dashboard needs, merged with persisted
    metadata. `risks` is included only when non-empty -- i.e. only when the
    opt-in port-awareness pass ran."""
    view = {
        "mac": d.mac,
        "ip": d.ip,
        "vendor": d.vendor,
        "device_type": d.device_type,
        "known": d.known,
    }
    if d.risks:
        view["risks"] = list(d.risks)
    meta = device_meta.get(d.mac) if device_meta else None
    view["name"] = meta["name"] if meta else None
    view["first_seen"] = meta["first_seen"] if meta else None
    view["last_seen"] = meta["last_seen"] if meta else None
    return view


def build_inventory_router(service: NetworkInventoryService,
                            device_meta: DeviceMeta | None = None,
                            name_deps=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/inventory")
    async def inventory():
        return {"devices": [_device_view(d, device_meta) for d in service.latest_inventory()]}

    @router.get("/api/alerts")
    async def alerts():
        return {"alerts": [a.to_dict() for a in service.recent_alerts()]}

    if device_meta is not None:
        @router.put("/api/inventory/name", dependencies=list(name_deps or []))
        async def name_device(mac: str = Body(...), name: str = Body(...)):
            try:
                mac_norm = normalize_mac(mac)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid MAC address")
            try:
                entry = device_meta.set_name(mac_norm, name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return entry

    return router
