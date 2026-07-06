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

from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException

from wavr.device_meta import DeviceMeta, normalize_mac
from wavr.netinventory_service import NetworkInventoryService


def _device_view(d, device_meta: DeviceMeta | None = None,
                 bound: dict[str, str] | None = None) -> dict:
    """Trim a Device to the fields the dashboard needs, merged with persisted
    metadata. `risks`/`open_ports`/`make`/`model`/`os`/`sources` are included
    only when populated -- i.e. only when the opt-in port pass or a richer
    recog signal produced them. A persisted user type-pin overrides
    device_type immediately (even between scans). `is_gateway` is included
    only when True and `latency_ms` only when the opt-in ping pass measured
    one (both additive -- every existing field unchanged).

    `bound` (item 4) maps ip -> friendly label for hosts the consent-gated live
    IP-correlation resolver named THIS view (green + fresh + unambiguous +
    MAC-consistent, all re-checked at view time). When this device's IP is in it,
    the view gains `label` + `paired=True` + a prepended `paired` evidence source,
    and its device_type becomes "phone" -- but ONLY when the owner has NOT pinned a
    type for this host: `paired` is the LABEL authority (high), a user type-pin
    stays the top device_type authority and is never overridden."""
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
    has_type_pin = bool(meta and meta.get("device_type"))
    # The user's pin wins instantly -- the scan loop folds it in on the next
    # pass anyway (highest recog precedence), this just closes the gap between
    # PUT /api/inventory/type and the next scan.
    if has_type_pin:
        view["device_type"] = meta["device_type"]
        view["type_confidence"] = "high"
    # LIVE IP-correlation overlay (item 4). Parallel to the user-pin instant override
    # above, and re-checked at THIS view time (the GDPR-red backstop: a silently-withdrawn
    # device that simply stopped POSTing is already gone from `bound`). `paired` is the
    # LABEL authority (high); it sets device_type "phone" ONLY when no user type-pin exists
    # -- an explicit owner pin is NEVER overridden.
    label = bound.get(d.ip) if (bound and d.ip) else None
    if label:
        view["label"] = label
        view["paired"] = True
        paired_src = {"signal": "paired",
                      "value": f"paired device -- live-correlated from {d.ip}"}
        view["sources"] = [paired_src] + view.get("sources", [])
        if not has_type_pin:
            view["device_type"] = "phone"
            view["type_confidence"] = "high"
    return view


def build_inventory_router(service: NetworkInventoryService,
                            device_meta: DeviceMeta | None = None,
                            name_deps=None, dhcp_monitor=None,
                            gateway_monitor=None, host_binder=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/inventory")
    async def inventory():
        devices = service.latest_inventory()
        # LIVE IP-correlation (item 4): resolve the paired-device labels for THIS view
        # against the SAME devices being rendered, so MAC-consistency is checked against the
        # host currently at each IP. Consent (green) is re-checked here, inside resolve, via
        # the binder's injected get_consent -- fail-closed, so a withdrawn/yellow/red or
        # ambiguous host yields no name. None binder (single-device build) -> empty overlay.
        bound: dict[str, str] = {}
        if host_binder is not None:
            mac_of_ip = {d.ip: d.mac for d in devices if d.ip}
            bound = host_binder.resolve(datetime.now(timezone.utc),
                                        lambda ip: mac_of_ip.get(ip))
        return {"devices": [_device_view(d, device_meta, bound) for d in devices]}

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
