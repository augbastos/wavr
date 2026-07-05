"""Wavr Net HTTP surface: the running inventory + rogue alerts (read-only),
plus the state-changing writes this module owns (naming and type-pinning a
device).

`build_inventory_router(service, device_meta=None, name_deps=None,
dhcp_monitor=None)` returns a FastAPI APIRouter exposing:
  * GET /api/inventory       -- the running inventory. Each device dict is
                                 merged with persisted metadata (`name`,
                                 `first_seen`, `last_seen`) when `device_meta`
                                 is given -- all three are None otherwise.
                                 ADDITIVE identity fields from the local recog
                                 fusion: `type_confidence` always; `make`,
                                 `model`, `os`, `open_ports`, `sources` only
                                 when populated. Every pre-existing field is
                                 unchanged.
  * GET /api/alerts          -- rogue-device alerts (+ `type_confidence` +
                                 `kind: "rogue_device"`), merged chronologically
                                 with `dhcp_monitor`'s rogue/multiple-DHCP-server
                                 alerts (`kind: "rogue_dhcp"`, collectors-lote2
                                 #7) and `gateway_monitor`'s gateway-identity
                                 change alerts (`kind: "gateway_identity"`,
                                 inventory feature #2) -- each omitted entirely (same as
                                 before) when its monitor is None.
  * PUT /api/inventory/name  -- {mac, name} -> persists a friendly device name.
  * PUT /api/inventory/type  -- {mac, device_type} -> persists the user
                                 device-type pin (taxonomy value; null/"" to
                                 clear). The pin is recog's highest-precedence
                                 signal and is reflected on the very next GET.
Both PUTs are only registered when `device_meta` is given and are gated by
`name_deps` (the app's require_local CSRF guard), same rule as every other
state-changing route.

GETs need no CSRF header -- the app's global loopback-only middleware is the
load-bearing access control, same as before.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from wavr.device_meta import DeviceMeta, normalize_mac
from wavr.netinventory_service import NetworkInventoryService


def _device_view(d, device_meta: DeviceMeta | None = None) -> dict:
    """Trim a Device to the fields the dashboard needs, merged with persisted
    metadata. `risks`/`open_ports`/`make`/`model`/`os`/`sources` are included
    only when populated -- i.e. only when the opt-in port pass or a richer
    recog signal produced them. A persisted user type-pin overrides
    device_type immediately (even between scans). `is_gateway` is included
    only when True and `latency_ms` only when the opt-in ping pass measured
    one (both additive -- every existing field unchanged)."""
    view = {
        "mac": d.mac,
        "ip": d.ip,
        "vendor": d.vendor,
        "device_type": d.device_type,
        "type_confidence": d.type_confidence,
        "known": d.known,
    }
    if d.risks:
        view["risks"] = list(d.risks)
    if d.open_ports:
        view["open_ports"] = list(d.open_ports)
    if d.make:
        view["make"] = d.make
    if d.model:
        view["model"] = d.model
    if d.os:
        view["os"] = d.os
    if d.sources:
        view["sources"] = [dict(s) for s in d.sources]
    if d.is_gateway:
        view["is_gateway"] = True
    if d.latency_ms is not None:
        view["latency_ms"] = d.latency_ms
    meta = device_meta.get(d.mac) if device_meta else None
    view["name"] = meta["name"] if meta else None
    view["first_seen"] = meta["first_seen"] if meta else None
    view["last_seen"] = meta["last_seen"] if meta else None
    # The user's pin wins instantly -- the scan loop folds it in on the next
    # pass anyway (highest recog precedence), this just closes the gap between
    # PUT /api/inventory/type and the next scan.
    if meta and meta.get("device_type"):
        view["device_type"] = meta["device_type"]
        view["type_confidence"] = "high"
    return view


def build_inventory_router(service: NetworkInventoryService,
                            device_meta: DeviceMeta | None = None,
                            name_deps=None, dhcp_monitor=None,
                            gateway_monitor=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/inventory")
    async def inventory():
        return {"devices": [_device_view(d, device_meta) for d in service.latest_inventory()]}

    @router.get("/api/alerts")
    async def alerts():
        # Rogue-device sightings always carry "kind" (additive field -- every
        # existing consumer already only reads a subset of keys). Merged with
        # the opt-in rogue-DHCP-server alerts (collectors-lote2 #7), oldest
        # first (same ordering `recent_alerts()` already returns), when a
        # monitor is wired in; omitted entirely (unchanged shape) otherwise.
        merged = [{"kind": "rogue_device", **a.to_dict()} for a in service.recent_alerts()]
        if dhcp_monitor is not None:
            merged += [a.to_dict() for a in dhcp_monitor.recent_alerts()]
        # Gateway-identity change alerts (gateway-identity-rogue-dhcp, the inventory roadmap
        # #2), `kind: "gateway_identity"`; omitted entirely when no monitor is
        # wired in (unchanged shape), same rule as the dhcp merge above.
        if gateway_monitor is not None:
            merged += [a.to_dict() for a in gateway_monitor.recent_alerts()]
        merged.sort(key=lambda a: a["ts"])
        return {"alerts": merged}

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

        @router.put("/api/inventory/type", dependencies=list(name_deps or []))
        async def pin_device_type(mac: str = Body(...),
                                  device_type: str | None = Body(None)):
            try:
                mac_norm = normalize_mac(mac)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid MAC address")
            try:
                entry = device_meta.set_type(mac_norm, device_type)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return entry

    return router
