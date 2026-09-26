"""ONVIF PTZ actuator routes (A4.3), moved out of `create_app` unchanged.

Opt-in (WAVR_PTZ) + `require_local` + the master camera kill-switch. Credentials
come ONLY from the stored rtsp_url and NEVER appear in a request, a response or
a log. No frame is ever read.
"""
from __future__ import annotations

import re
from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException

# ONVIF PTZ preset tokens: the token is XML-escaped in the SOAP body anyway,
# but reject obviously-junk tokens early so a hostile id can't reach a log/traceback.
_PRESET_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,100}$")


def build_ptz_router(*, enabled: bool, cameras, ptz,
                     sources_status: Callable[[], dict],
                     require_local, require_scope, name_re) -> APIRouter:
    router = APIRouter()

    def _ptz_cam(camera_id: str) -> dict:
        # Flag gate FIRST (default OFF -> 503 before any store lookup / ONVIF call).
        if not enabled:
            raise HTTPException(status_code=503, detail="PTZ disabled (set WAVR_PTZ=1)")
        if not name_re.match(camera_id):
            raise HTTPException(status_code=400, detail="camera id must be alphanumeric/_/-")
        cam = cameras.get(camera_id)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {camera_id}")
        return cam   # cam["rtsp_url"] carries the creds -- NEVER echo it back

    def _camera_active(camera_id: str) -> bool:
        # Master camera kill-switch coupling: PTZ may only actuate a camera the
        # operator has explicitly turned ON (source task running). System kill or a
        # per-source disable both flip `active` False -> every move short-circuits.
        return any(s["name"] == camera_id and s["active"]
                   for s in sources_status()["sources"])

    @router.post("/api/ptz/{camera_id}/move")
    async def ptz_move(camera_id: str,
                       pan: float = Body(0.0), tilt: float = Body(0.0),
                       zoom: float = Body(0.0), _=Depends(require_local),
                       __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        if not _camera_active(camera_id):
            # Camera off -> no ONVIF call at all (kill-switch dominates PTZ).
            return {"ok": False, "reason": "camera off"}
        ok = await ptz.continuous_move(camera_id, cam["rtsp_url"], pan, tilt, zoom)
        return {"ok": ok}

    @router.post("/api/ptz/{camera_id}/stop")
    async def ptz_stop(camera_id: str, _=Depends(require_local),
                       __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        # Stop is always allowed (safety): even a just-disabled camera should halt.
        return {"ok": await ptz.stop(camera_id, cam["rtsp_url"])}

    @router.get("/api/ptz/{camera_id}/presets")
    async def ptz_presets(camera_id: str, _=Depends(require_scope("camera:view"))):
        cam = _ptz_cam(camera_id)
        return await ptz.get_presets(camera_id, cam["rtsp_url"])

    @router.post("/api/ptz/{camera_id}/preset/{token}")
    async def ptz_goto_preset(camera_id: str, token: str, _=Depends(require_local),
                              __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        if not _PRESET_RE.match(token):
            raise HTTPException(status_code=400, detail="invalid preset token")
        if not _camera_active(camera_id):
            return {"ok": False, "reason": "camera off"}
        return {"ok": await ptz.goto_preset(camera_id, cam["rtsp_url"], token)}

    @router.get("/api/ptz/{camera_id}/capabilities")
    async def ptz_capabilities(camera_id: str, _=Depends(require_scope("camera:view"))):
        cam = _ptz_cam(camera_id)
        return await ptz.capabilities(camera_id, cam["rtsp_url"])

    @router.get("/api/ptz/{camera_id}/status")
    async def ptz_status(camera_id: str, _=Depends(require_scope("camera:view"))):
        # Read-only PTZ position (pan/tilt/zoom) -- the BEARING SEAM for person
        # localization on a pan/tilt camera. Same gate/pattern as capabilities:
        # WAVR_PTZ + loopback; reads ONLY ONVIF control metadata, NEVER a frame
        # (ADR-0002). Creds come from the stored rtsp_url and never reach the response.
        # None (non-PTZ/offline/faulting camera) surfaces as {"status": null}.
        cam = _ptz_cam(camera_id)
        return {"status": await ptz.get_status(camera_id, cam["rtsp_url"])}

    return router
