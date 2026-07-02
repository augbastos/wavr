from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr.config import load_config
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sourcemanager import SourceManager
from wavr.sources.simulated import SimulatedSource
from wavr.sources.network import NetworkSource
from wavr.sources.ruview import RuViewSource
from wavr.sources.camera import CameraSource
from wavr.camera_store import CameraStore


_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def _is_loopback(host) -> bool:
    return host in _LOOPBACK_HOSTS


def _default_sources(cfg):
    """Plano A real-source set: network always-on ($0), ruview always-on (harmless
    reconnect loop when the container is absent), sim off by default (toggle it on
    from the dashboard to populate the view when no real data is flowing)."""
    return [
        ("network", lambda: NetworkSource(
            cfg.net_known_macs, interval=cfg.net_interval, grace=cfg.net_grace), True),
        ("ruview", lambda: RuViewSource(
            cfg.ruview_url, room=cfg.ruview_room, reconnect_delay=cfg.ruview_reconnect), True),
        ("sim", lambda: SimulatedSource(interval=cfg.sim_interval), False),
    ]


def _mask_rtsp(url: str) -> str:
    """Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rtsp://user:***@host."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def _camera_factory(cam: dict, cfg):
    return lambda: CameraSource(cam["room"], cam["rtsp_url"],
                                interval=cfg.cam_interval, confidence=cam["confidence"])


def create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold)
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)

    async def _ingest(event):
        rs = _fusion.update(event)
        d = rs.to_dict()
        _storage.insert_state(rs)
        latest[d["room"]] = d
        await _hub.publish(d)

    manager = SourceManager(_ingest)
    for name, factory, enabled in (sources if sources is not None else _default_sources(cfg)):
        manager.register(name, factory, enabled)

    _cameras = camera_store or CameraStore(cfg.db_path)
    for cam in _cameras.list():                       # persisted cameras -> boot-OFF sources
        manager.register(cam["name"], _camera_factory(cam, cfg), False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="Wavr", lifespan=lifespan)

    # PRIVACY: reject any request whose peer isn't loopback. Enforced in code so it
    # holds even if someone runs uvicorn with --host 0.0.0.0. ("testclient" is the
    # pytest TestClient peer.) This is the load-bearing control; the Host allowlist
    # is extra defense against DNS-rebinding.
    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        host = request.client.host if request.client else None
        if not _is_loopback(host):
            return JSONResponse({"detail": "loopback only"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
    )

    def require_local(request: Request):
        # CSRF guard for state-changing routes: a cross-origin browser page can't set
        # a custom header on a simple request without a (failing) CORS preflight, so
        # this blocks drive-by POSTs (e.g. a webpage trying to enable your camera).
        if request.headers.get("x-wavr-local") != "1":
            raise HTTPException(status_code=403, detail="missing X-Wavr-Local header")

    @app.get("/api/history")
    async def history(limit: int = 200):
        return _storage.recent(limit)

    @app.get("/api/state")
    async def state():
        return latest

    @app.get("/api/system")
    async def system():
        return manager.status()

    @app.post("/api/system/toggle")
    async def system_toggle(on: bool = Body(..., embed=True), _=Depends(require_local)):
        await manager.set_running(on)
        return manager.status()

    @app.post("/api/sources/{name}/toggle")
    async def source_toggle(name: str, enabled: bool = Body(..., embed=True), _=Depends(require_local)):
        try:
            await manager.set_enabled(name, enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown source: {name}")
        return manager.status()

    @app.get("/api/cameras")
    async def cameras():
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @app.post("/api/cameras")
    async def add_camera(
        name: str = Body(...), room: str = Body(...),
        rtsp_url: str = Body(...), confidence: float = Body(cfg.cam_confidence),
        _=Depends(require_local),
    ):
        name = name.strip()
        if not name or not room.strip() or not rtsp_url.strip():
            raise HTTPException(status_code=400, detail="name, room, rtsp_url are required")
        if name in {s["name"] for s in manager.status()["sources"]}:
            raise HTTPException(status_code=409, detail=f"source name in use: {name}")
        try:
            _cameras.add(name, room.strip(), rtsp_url.strip(), confidence)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"camera exists: {name}")
        manager.register(name, _camera_factory(_cameras.get(name), cfg), False)  # boots OFF
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @app.delete("/api/cameras/{name}")
    async def delete_camera(name: str, _=Depends(require_local)):
        if not _cameras.delete(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            await manager.unregister(name)
        except KeyError:
            pass   # not registered (e.g. removed before a restart re-registered it)
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        host = ws.client.host if ws.client else None
        if not _is_loopback(host):
            await ws.close(code=1008)  # policy violation; WS isn't covered by the http middleware
            return
        await ws.accept()
        q = _hub.subscribe()
        try:
            while True:
                await ws.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            _hub.unsubscribe(q)

    @app.get("/")
    async def dashboard():
        return FileResponse(_INDEX)

    return app


app = create_app()
