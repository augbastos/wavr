from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
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
from wavr.rules import RulesEngine
from wavr.away import AwayMonitor
from wavr.mqtt_publisher import make_publisher
from wavr.narrator import Narrator, make_gemini_generate


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


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_URL_SHAPE_RE = re.compile(r"^\w+://.+")


def _mask_rtsp(url: str) -> str:
    """Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rtsp://user:***@host.
    Never raises: any unexpected shape (e.g. "a@b://c") is returned unchanged rather than crashing a GET/POST."""
    try:
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user = creds.split(":", 1)[0]
            creds = f"{user}:***"
        return f"{scheme}://{creds}@{host}"
    except (ValueError, IndexError):
        return url


def _camera_factory(cam: dict, cfg):
    return lambda: CameraSource(cam["room"], cam["rtsp_url"],
                                interval=cfg.cam_interval, confidence=cam["confidence"])


def create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None,
               rules_publish=None, narrator=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold)
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)

    # Rules/MQTT engine: opt-in via injected `rules_publish` (tests) or WAVR_MQTT_ENABLED
    # (real paho publisher, lazily connected). Off by default -- no publisher, no engine.
    _rules_publish = rules_publish
    if _rules_publish is None and cfg.mqtt_enabled:
        _rules_publish = make_publisher(cfg.mqtt_host, cfg.mqtt_port)
    _rules = RulesEngine(_rules_publish, prefix=cfg.mqtt_prefix) if _rules_publish else None
    _away = AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace) if _rules_publish else None

    # Narrator: opt-in via injected `narrator` (tests) or GEMINI_API_KEY (real Gemini
    # generator, lazily imported). Off by default -- no key, no narrator, 503 on call.
    _narrator = narrator
    if _narrator is None and cfg.gemini_api_key:
        _narrator = Narrator(make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model))

    async def _ingest(event):
        rs = _fusion.update(event)
        d = rs.to_dict()
        _storage.insert_state(rs)
        latest[d["room"]] = d
        await _hub.publish(d)

    manager = SourceManager(_ingest)
    for name, factory, enabled in (sources if sources is not None else _default_sources(cfg)):
        manager.register(name, factory, enabled)

    _owns_cameras = camera_store is None   # only close a store this function built itself
    _cameras = camera_store or CameraStore(cfg.db_path)
    for cam in _cameras.list():                       # persisted cameras -> boot-OFF sources
        manager.register(cam["name"], _camera_factory(cam, cfg), False)

    def _masked_cameras():
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        rules_task = asyncio.create_task(_rules.run(_hub)) if _rules else None
        away_task = asyncio.create_task(_away.run(_hub)) if _away else None
        try:
            yield
        finally:
            # Suppress CancelledError AND any error a caller-injected publisher
            # might raise, so shutdown always reaches manager.stop() + camera close.
            for t in (rules_task, away_task):
                if t:
                    t.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await t
            await manager.stop()
            if _owns_cameras:
                with suppress(Exception):
                    _cameras.close()

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

    @app.post("/api/narrate")
    async def narrate(_=Depends(require_local)):
        if _narrator is None:
            raise HTTPException(status_code=503, detail="narration not configured (set GEMINI_API_KEY)")
        try:
            text = _narrator.narrate(latest, _storage.recent(50))
        except Exception:
            raise HTTPException(status_code=502, detail="narration backend error")
        return {"narration": text}

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
        return _masked_cameras()

    @app.post("/api/cameras")
    async def add_camera(
        name: str = Body(...), room: str = Body(...),
        rtsp_url: str = Body(...), confidence: float = Body(cfg.cam_confidence),
        _=Depends(require_local),
    ):
        name = name.strip()
        room = room.strip()
        rtsp_url = rtsp_url.strip()
        if not name or not room or not rtsp_url:
            raise HTTPException(status_code=400, detail="name, room, rtsp_url are required")
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="name must be alphanumeric/_/-")
        if not _URL_SHAPE_RE.match(rtsp_url):
            raise HTTPException(status_code=400, detail="rtsp_url must look like scheme://...")
        if not (0.0 <= confidence <= 1.0):
            raise HTTPException(status_code=400, detail="confidence must be between 0.0 and 1.0")
        if name in {s["name"] for s in manager.status()["sources"]}:
            raise HTTPException(status_code=409, detail=f"source name in use: {name}")
        try:
            _cameras.add(name, room, rtsp_url, confidence)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"camera exists: {name}")
        manager.register(name, _camera_factory(_cameras.get(name), cfg), False)  # boots OFF
        return _masked_cameras()

    @app.delete("/api/cameras/{name}")
    async def delete_camera(name: str, _=Depends(require_local)):
        if not _cameras.delete(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            await manager.unregister(name)
        except KeyError:
            pass   # not registered (e.g. removed before a restart re-registered it)
        return _masked_cameras()

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
