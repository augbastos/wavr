"""Read-only Wavr Net HTTP surface: the running inventory + rogue alerts.

`build_inventory_router(service)` returns a FastAPI APIRouter exposing two GETs
backed by a NetworkInventoryService. GETs only -- no state change, so (like the
existing GET /api/state) they need no CSRF header; the app's global loopback-only
middleware is the load-bearing access control.
"""
from __future__ import annotations

from fastapi import APIRouter

from wavr.netinventory_service import NetworkInventoryService


def _device_view(d) -> dict:
    """Trim a Device to the fields the dashboard needs. `risks` is included only
    when non-empty -- i.e. only when the opt-in port-awareness pass ran."""
    view = {
        "mac": d.mac,
        "ip": d.ip,
        "vendor": d.vendor,
        "device_type": d.device_type,
        "known": d.known,
    }
    if d.risks:
        view["risks"] = list(d.risks)
    return view


def build_inventory_router(service: NetworkInventoryService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/inventory")
    async def inventory():
        return {"devices": [_device_view(d) for d in service.latest_inventory()]}

    @router.get("/api/alerts")
    async def alerts():
        return {"alerts": [a.to_dict() for a in service.recent_alerts()]}

    return router
