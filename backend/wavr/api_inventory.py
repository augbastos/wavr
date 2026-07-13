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
                                 `model`, `os`, `hostname`, `open_ports`,
                                 `sources` only when populated. Every
                                 pre-existing field is unchanged. `display_name`
                                 rides alongside `hostname` (only when
                                 `hostname` is set): the router's DHCP
                                 search-domain suffix stripped + separators
                                 prettified (wavr.data.deviceclass.
                                 display_hostname) -- what the Network tab
                                 should render; `hostname` itself stays raw.
  * GET /api/alerts          -- rogue-device alerts (+ `type_confidence` +
                                 `kind: "rogue_device"`), merged chronologically
                                 with `dhcp_monitor`'s rogue/multiple-DHCP-server
                                 alerts (`kind: "rogue_dhcp"`, collectors-lote2
                                 #7) and `gateway_monitor`'s gateway-identity
                                 change alerts (`kind: "gateway_identity"`,
                                 inventory feature #2) -- each omitted entirely (same as
                                 before) when its monitor is None. The merge/sort
                                 itself lives in the module-level `merge_alerts()`
                                 below (Build A10: also the network-layer input to
                                 `wavr.house_status`, so the two never drift).
  * PUT /api/inventory/name  -- {mac, name} -> persists a friendly device name.
  * PUT /api/inventory/type  -- {mac, device_type} -> persists the user
                                 device-type pin (taxonomy value; null/"" to
                                 clear). The pin is recog's highest-precedence
                                 signal and is reflected on the very next GET.
  * POST /api/inventory/known -- {mac, known} -> persists a RUNTIME
                                 known/unknown flag (wavr.known_store.
                                 KnownStore) so an ordinary house device that
                                 was never on the static WAVR_NET_MACS env
                                 allowlist can be marked known WITHOUT a
                                 restart. known=true immediately drops any
                                 existing rogue_device alert for that MAC
                                 from GET /api/alerts and from the very next
                                 scan onward; known=false re-arms it (it
                                 alerts again if it resurfaces unknown). See
                                 `NetworkInventoryService.apply_known_change`.
  * POST /api/inventory/known/bulk -- no body -> marks every device in the
                                 CURRENT inventory snapshot that is currently
                                 unknown as known (loops the same set_known +
                                 apply_known_change pair as the single-device
                                 route above). Returns `{marked: N}`. The
                                 admin-initiated "Trust all N devices" bulk
                                 action -- a one-shot snapshot, never an
                                 auto-trust window.
Both PUTs and the known POST(s) are only registered when their store
(`device_meta`/`known_store` respectively) is given, and are gated by
`name_deps` (the app's require_local CSRF guard), same rule as every other
state-changing route.

GETs need no CSRF header -- the app's global loopback-only middleware is the
load-bearing access control, same as before.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from wavr.data.deviceclass import display_hostname
from wavr.device_meta import DeviceMeta, normalize_mac
from wavr.netinventory_service import NetworkInventoryService


def merge_alerts(service: NetworkInventoryService, *, dhcp_monitor=None,
                 gateway_monitor=None, intrusion_log=None, fall_log=None,
                 intrusion_house_loud: bool = False) -> list[dict]:
    """The ONE merged, chronologically-sorted alert list every alert-consuming
    surface reads: GET /api/alerts below, and (Build A10) `wavr.house_status`'s
    network-layer input via app.py. Factored out so both call sites share
    EXACTLY the same merge/omission rules -- never two copies that could drift.

    Rogue-device sightings always carry "kind" (additive field -- every existing
    consumer already only reads a subset of keys). Merged with the opt-in
    rogue-DHCP-server alerts (collectors-lote2 #7), the opt-in gateway-identity
    change alerts (inventory feature #2, `kind: "gateway_identity"`), Watch/Guard
    intrusion alerts (`kind: "intrusion"`, severity "alert", room-level +
    count-only -- never a target position or identity), and A9's fall/no-motion
    suspicion alerts (`kind: "fall_suspected"`, severity "alert", room + duration
    only, RESEARCH-GRADE per ADR-0003 -- see wavr.fall_detect) -- each source
    omitted entirely (unchanged shape) when its monitor/log is None, same rule
    as before.

    The house-LEVEL intrusion alert (room=None, the sum-of-per-room aggregate) is
    INFORMATIONAL by default: it double-counts a person in a doorway seen by two
    rooms, so it does NOT ride this loud /api/alerts path unless
    `intrusion_house_loud` (WAVR_WATCH_INTRUSION_LOUD) is on. It stays surfaced in
    /api/watch, /api/house-status and the C4 HA house-level binary_sensor
    regardless. The per-room intrusion signal (room set, more reliable, less
    double-count) is ALWAYS emitted here -- unchanged."""
    merged = [{"kind": "rogue_device", **a.to_dict()} for a in service.recent_alerts()]
    if dhcp_monitor is not None:
        merged += [a.to_dict() for a in dhcp_monitor.recent_alerts()]
    if gateway_monitor is not None:
        merged += [a.to_dict() for a in gateway_monitor.recent_alerts()]
    if intrusion_log is not None:
        for a in intrusion_log.recent_alerts():
            d = a.to_dict()
            # House-level aggregate (room=None) is INFORMATIONAL by default: it double-
            # counts a person in a doorway seen by two rooms, so its false-positive risk
            # means it must NOT ride the loud /api/alerts notify path unless
            # WAVR_WATCH_INTRUSION_LOUD is on. It stays visible in /api/watch,
            # /api/house-status and the C4 HA house-level binary_sensor regardless. The
            # per-room signal (room set, more reliable) is ALWAYS emitted -- unchanged.
            if d.get("room") is None and not intrusion_house_loud:
                continue
            merged.append(d)
    if fall_log is not None:
        merged += [a.to_dict() for a in fall_log.recent_alerts()]
    merged.sort(key=lambda a: a["ts"])
    return merged


def _device_view(d, meta: dict | None = None) -> dict:
    """Trim a Device to the fields the dashboard needs, merged with persisted
    metadata. `risks`/`open_ports`/`make`/`model`/`os`/`hostname`/`sources`
    are included only when populated -- i.e. only when the opt-in port pass
    or a richer recog signal produced them. `hostname` is the device's own
    DHCP-fingerprint/PTR-resolved/self-announced (mDNS/SSDP/SNMP/NetBIOS)
    name (wavr.netinventory.apply_recognition) -- additive, was captured on
    Device but never reached this view before. A persisted user type-pin
    overrides device_type immediately (even between scans). `is_gateway` is
    included only when True and `latency_ms` only when the opt-in ping pass
    measured one (both additive -- every existing field unchanged).

    `meta` is THIS device's already-looked-up {name, first_seen, last_seen,
    device_type} dict (or None) -- the caller (`inventory_view`) fetches
    every device's metadata in one batched `DeviceMeta.get_many()` call
    instead of this function querying the store itself per device (that was
    the N+1: one SELECT per device on GET /api/inventory, polled every 15s)."""
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
    if d.hostname:
        view["hostname"] = d.hostname
        # `display_name` is a DERIVED cleanup of `hostname` for the Network-tab
        # label only (strips the router's DHCP search-domain suffix, prettifies
        # separators) -- the raw `hostname` above is left untouched so anything
        # keyed on the full string (wavr.data.deviceclass.hostname_type et al.)
        # keeps seeing it. Omitted (not just None) when nothing survives cleanup.
        cleaned = display_hostname(d.hostname)
        if cleaned:
            view["display_name"] = cleaned
    if d.sources:
        view["sources"] = [dict(s) for s in d.sources]
    if d.is_gateway:
        view["is_gateway"] = True
    if d.latency_ms is not None:
        view["latency_ms"] = d.latency_ms
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


def inventory_view(service: NetworkInventoryService,
                   device_meta: DeviceMeta | None = None) -> list[dict]:
    """The SAME per-device view GET /api/inventory returns -- factored out (mirrors
    merge_alerts's one-function-many-callers precedent) so the MCP
    get_network_inventory tool (wavr.mcp, via app.py's closure) and this route can
    never drift. Reads whatever `service`'s already-scanned/cached inventory
    currently holds -- NEVER triggers a rescan.

    Metadata is fetched for every device in ONE batched `device_meta.get_many()`
    call rather than one `get()` per device (N+1 fix: GET /api/inventory is
    polled every 15s by the dashboard)."""
    devices = service.latest_inventory()
    meta_by_mac = device_meta.get_many(d.mac for d in devices) if device_meta else {}
    return [_device_view(d, meta_by_mac.get(d.mac)) for d in devices]


def build_inventory_router(service: NetworkInventoryService,
                            device_meta: DeviceMeta | None = None,
                            name_deps=None, dhcp_monitor=None,
                            gateway_monitor=None, known_store=None,
                            intrusion_log=None, fall_log=None,
                            intrusion_house_loud=False) -> APIRouter:
    router = APIRouter()

    @router.get("/api/inventory")
    async def inventory():
        return {"devices": inventory_view(service, device_meta)}

    @router.get("/api/alerts")
    async def alerts():
        return {"alerts": merge_alerts(service, dhcp_monitor=dhcp_monitor,
                                       gateway_monitor=gateway_monitor,
                                       intrusion_log=intrusion_log, fall_log=fall_log,
                                       intrusion_house_loud=intrusion_house_loud)}

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

    if known_store is not None:
        @router.post("/api/inventory/known", dependencies=list(name_deps or []))
        async def mark_known(mac: str = Body(...), known: bool = Body(...)):
            try:
                mac_norm = normalize_mac(mac)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid MAC address")
            entry = known_store.set_known(mac_norm, known)
            # Sync the live alert log + cached inventory immediately (see
            # apply_known_change's docstring) -- the KnownStore write above
            # already makes it authoritative for the NEXT scan regardless.
            service.apply_known_change(mac_norm, known)
            return entry

        @router.post("/api/inventory/known/bulk", dependencies=list(name_deps or []))
        async def mark_all_known():
            # Admin-initiated bulk trust: "Trust all N devices" for the CURRENT
            # inventory snapshot only -- never an auto-trust window. Same CSRF
            # gate (name_deps) as the single-device route above; one write per
            # currently-unknown device, same known_store + service sync each.
            marked = 0
            for d in service.latest_inventory():
                if not d.known:
                    known_store.set_known(d.mac, True)
                    service.apply_known_change(d.mac, True)
                    marked += 1
            return {"marked": marked}

    return router
