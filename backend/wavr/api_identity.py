"""FastAPI router for the consent-first identity/device registry
(2026-07-06 ethics decision). Mirrors api_devices.py: a small injectable factory
built around IdentityStore so it stays testable.

Routes (all gated in app.py -- router-level central/root + per-write require_local
CSRF, the same gates as the camera + device-management routes):

  * GET    /api/identity/devices                -> list registered (consented) devices
  * GET    /api/identity/bonded                 -> this PC's bonded BT devices (SUGGESTION)
  * GET    /api/identity/known-presence         -> house-level "likely home" summary
  * POST   /api/identity/devices                -> register (affirmative confirm/manual add)
  * PATCH  /api/identity/devices/{addr}/details -> toggle consent #2 (richer metadata)
  * DELETE /api/identity/devices/{addr}         -> un-register = participation opt-out

The registry holds ONLY admin-confirmed / self-attested devices: the POST is the
affirmative act (a bonded device is a suggestion until it arrives here), and the
DELETE immediately stops the device being a presence signal + drops its label.
PATCH .../details is a SEPARATE, narrower consent (see identity_store.py's module
docstring: consent #2) -- it can only be flipped on an already-registered row,
never registers one, and opting it off never drops the device's presence vote.

Addresses are normalized + validated at this boundary (normalize_mac); a whole
batch is rejected 400 with NOTHING persisted if any address/label is junk, so a
malformed value can never reach SQL or be reflected via a later GET.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.identity_store import VALID_ORIGINS, VALID_SOURCES
from wavr.device_meta import normalize_mac, sanitize_name
from wavr.known_presence import compose_known_presence


def _deps_not_wired() -> None:
    """FAIL-CLOSED default for `write_deps` (sweep #13, mirrors
    api_nodes._admin_deps_not_wired). Previously `write_deps=None` -> `[]`, so if
    app.py's wiring ever forgot to pass the require_local CSRF guard, the
    register/set_details/unregister writes would run completely UNAUTHENTICATED.
    A forgotten argument must never silently open the identity registry, so the
    default now DENIES instead: every write route 403s until the real gate is
    explicitly wired."""
    raise HTTPException(status_code=403,
                        detail="identity write routes have no auth gate wired")

# Bound a single POST's device batch (audit LOW): a real household registration is a
# handful of devices at a time (the admin confirming their own phone/watch, or a
# bonded-suggestion batch); this caps how many normalize_mac/sanitize_name + store.add()
# calls one unauthenticated-shape-but-CSRF-gated request can force. Mirrors
# wavr.nodes._MAX_ARRAY's per-request batch-cap convention.
_MAX_DEVICES_PER_BATCH = 128


def build_identity_router(store, bonded_reader=None, ensure_source=None,
                          write_deps=None, casa_state_provider=None,
                          device_meta=None, known_store=None,
                          net_service=None) -> APIRouter:
    """`store` -- IdentityStore. `bonded_reader` -- async () -> [{address, name}]
    (the OS bonded enumeration; None => bonded read disabled -> []). `ensure_source`
    -- optional sync callback invoked after a 'ble' device is registered so app.py
    can lazily register the BLE source (live, no restart) when the first BLE device
    is added on a previously-empty install. `write_deps` -- FastAPI deps applied to
    the state-changing POST/DELETE/PATCH only (the require_local CSRF guard); the
    GET reads carry no CSRF. `casa_state_provider` -- optional sync () -> RoomState|
    None, the ALREADY-fused house-level "casa" room (e.g. `_fusion.state("casa")`);
    None => known-presence reports no network-source evidence for this cycle.
    `device_meta` -- optional wavr.device_meta.DeviceMeta, read via `.all()` for the
    known-presence route's freshness check; None => no rows => everything absent.
    `known_store`/`net_service` -- optional wavr.known_store.KnownStore /
    NetworkInventoryService (the same pair api_inventory.py's POST /api/inventory/
    known writes through). When BOTH are wired, registering a 'network'-source
    device here (the admin naming/assigning an already-recognized house device to
    a person) ALSO marks that MAC known -- this is two already-explicit admin
    actions (assigning a device to a person, recognizing it) wired together, not a
    new auto-trust path. Either left None => this cross-wire is a no-op (byte-
    identical to before), so it's opt-in per app.py wiring, never implicit."""
    router = APIRouter()
    wdeps = list(write_deps) if write_deps else [Depends(_deps_not_wired)]

    @router.get("/api/identity/devices")
    async def list_devices():
        return {"devices": store.list()}

    @router.get("/api/identity/bonded")
    async def bonded_devices():
        # Read-only SUGGESTION: this PC's bonded BT devices, each flagged whether it
        # is already registered so the UI can PRE-CHECK the unregistered ones for the
        # admin's one-tap "these are mine" confirm. Never writes the store.
        if bonded_reader is None:
            return {"devices": []}
        try:
            found = await bonded_reader()
        except Exception:
            logging.warning("bonded read failed in route", exc_info=True)
            found = []
        registered = {d["address"] for d in store.list()}
        return {"devices": [
            {"address": d["address"], "name": d.get("name", ""),
             "already_registered": d["address"] in registered}
            for d in found
        ]}

    @router.get("/api/identity/known-presence")
    async def known_presence():
        # Honest house-level "likely home": composed PURELY from what's already
        # collected (the fused `casa` state, the registry, DeviceMeta) -- no scan,
        # no re-fusion triggered by this GET. See wavr.known_presence's docstring
        # for the two-level consent this respects.
        casa_state = casa_state_provider() if casa_state_provider is not None else None
        meta_rows = device_meta.all() if device_meta is not None else {}
        return compose_known_presence(
            casa_state=casa_state,
            net_registry=store.as_net_map(),
            detailed_addrs=store.detailed_net_addresses(),
            meta_rows=meta_rows,
            now=datetime.now(timezone.utc),
        )

    @router.post("/api/identity/devices", dependencies=wdeps)
    async def register(person: str = Body("", embed=True),
                       devices: list = Body(..., embed=True)):
        # Affirmative act: register one or more consented devices. `person` is the
        # batch label (e.g. the admin confirming their own devices); a per-device
        # `label` overrides it, `source` picks the modality (ble|network), `origin`
        # records how consent was expressed (bonded|manual, default manual). An
        # optional per-device `details` bool is consent #2 (see identity_store.py) --
        # omitted/None leaves any existing opt-in untouched (a plain re-register
        # never silently revokes it); explicit true/false sets it as part of this
        # same write. Validate the WHOLE batch first -> reject 400 with nothing
        # persisted on any junk.
        if not isinstance(devices, list) or not devices:
            raise HTTPException(status_code=400, detail="devices must be a non-empty list")
        if len(devices) > _MAX_DEVICES_PER_BATCH:
            raise HTTPException(status_code=400,
                                detail=f"devices batch too large (> {_MAX_DEVICES_PER_BATCH})")
        prepared = []
        for d in devices:
            if not isinstance(d, dict):
                raise HTTPException(status_code=400, detail="each device must be an object")
            label = (d.get("label") or person or "")
            source = str(d.get("source") or "ble").strip().lower()
            origin = str(d.get("origin") or "manual").strip().lower()
            if source not in VALID_SOURCES:
                raise HTTPException(status_code=400,
                                    detail=f"source must be one of {sorted(VALID_SOURCES)}")
            if origin not in VALID_ORIGINS:
                raise HTTPException(status_code=400,
                                    detail=f"origin must be one of {sorted(VALID_ORIGINS)}")
            raw_details = d.get("details")
            details = None if raw_details is None else bool(raw_details)
            try:
                addr = normalize_mac(d.get("address", ""))
                who = sanitize_name(label)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            prepared.append((addr, who, source, origin, details))
        added_sources = set()
        for addr, who, source, origin, details in prepared:
            store.add(addr, who, source, origin, details=details)
            added_sources.add(source)
            # Naming/assigning a 'network'-source device to a person IS the admin
            # recognizing it -- mirror api_inventory.py's POST /api/inventory/known
            # so an already-registered house device stops re-alerting as rogue.
            # Only wired when both stores are given (app.py's explicit opt-in);
            # never on DELETE below -- un-labeling must not silently re-arm.
            if source == "network" and known_store is not None and net_service is not None:
                known_store.set_known(addr, True)
                net_service.apply_known_change(addr, True)
        # Live: bring up the BLE source now if the first BLE device just landed on
        # an install that had none (network is always-on already).
        if ensure_source is not None and "ble" in added_sources:
            ensure_source()
        return {"devices": store.list()}

    @router.patch("/api/identity/devices/{address}/details", dependencies=wdeps)
    async def set_details(address: str, on: bool = Body(..., embed=True)):
        # Consent #2 toggle only -- never registers a device. 404 (not 400) when
        # the address is well-formed but not an already-registered row, mirroring
        # DELETE's "unknown device" shape below.
        try:
            addr = normalize_mac(address)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid device address")
        if not store.set_details(addr, on):
            raise HTTPException(status_code=404, detail=f"unknown device: {addr}")
        return {"devices": store.list()}

    @router.delete("/api/identity/devices/{address}", dependencies=wdeps)
    async def unregister(address: str):
        # Participation opt-out. Validate the path address (400 on junk, never
        # reflected as-is), then remove -> the live provider stops returning it on
        # the next scan cycle and its label is gone.
        try:
            addr = normalize_mac(address)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid device address")
        if not store.delete(addr):
            raise HTTPException(status_code=404, detail=f"unknown device: {addr}")
        return {"devices": store.list()}

    return router
