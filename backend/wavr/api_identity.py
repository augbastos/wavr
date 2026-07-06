"""FastAPI router for the consent-first identity/device registry
(2026-07-06 ethics decision). Mirrors api_devices.py: a small injectable factory
built around IdentityStore so it stays testable.

Routes (all gated in app.py -- router-level central/root + per-write require_local
CSRF, the same gates as the camera + device-management routes):

  * GET    /api/identity/devices        -> list registered (consented) devices
  * GET    /api/identity/bonded         -> this PC's bonded BT devices (SUGGESTION)
  * POST   /api/identity/devices        -> register (affirmative confirm/manual add)
  * DELETE /api/identity/devices/{addr} -> un-register = participation opt-out

The registry holds ONLY admin-confirmed / self-attested devices: the POST is the
affirmative act (a bonded device is a suggestion until it arrives here), and the
DELETE immediately stops the device being a presence signal + drops its label.

Addresses are normalized + validated at this boundary (normalize_mac); a whole
batch is rejected 400 with NOTHING persisted if any address/label is junk, so a
malformed value can never reach SQL or be reflected via a later GET.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from wavr.identity_store import VALID_ORIGINS, VALID_SOURCES
from wavr.device_meta import normalize_mac, sanitize_name


def build_identity_router(store, bonded_reader=None, ensure_source=None,
                          write_deps=None) -> APIRouter:
    """`store` -- IdentityStore. `bonded_reader` -- async () -> [{address, name}]
    (the OS bonded enumeration; None => bonded read disabled -> []). `ensure_source`
    -- optional sync callback invoked after a 'ble' device is registered so app.py
    can lazily register the BLE source (live, no restart) when the first BLE device
    is added on a previously-empty install. `write_deps` -- FastAPI deps applied to
    the state-changing POST/DELETE only (the require_local CSRF guard); the GET
    reads carry no CSRF."""
    router = APIRouter()
    wdeps = list(write_deps or [])

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

    @router.post("/api/identity/devices", dependencies=wdeps)
    async def register(person: str = Body("", embed=True),
                       devices: list = Body(..., embed=True)):
        # Affirmative act: register one or more consented devices. `person` is the
        # batch label (e.g. the admin confirming their own devices); a per-device
        # `label` overrides it, `source` picks the modality (ble|network), `origin`
        # records how consent was expressed (bonded|manual, default manual). Validate
        # the WHOLE batch first -> reject 400 with nothing persisted on any junk.
        if not isinstance(devices, list) or not devices:
            raise HTTPException(status_code=400, detail="devices must be a non-empty list")
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
            try:
                addr = normalize_mac(d.get("address", ""))
                who = sanitize_name(label)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            prepared.append((addr, who, source, origin))
        added_sources = set()
        for addr, who, source, origin in prepared:
            store.add(addr, who, source, origin)
            added_sources.add(source)
        # Live: bring up the BLE source now if the first BLE device just landed on
        # an install that had none (network is always-on already).
        if ensure_source is not None and "ble" in added_sources:
            ensure_source()
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
