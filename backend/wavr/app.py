from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr.config import load_config
from wavr.housemap import load_house_map
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sourcemanager import SourceManager
from wavr.sources.simulated import SimulatedSource
from wavr.sources.network import NetworkSource, _local_ipv4
from wavr.sources.ruview import RuViewSource
from wavr.sources.camera import CameraSource
from wavr.sources.mmwave import MmWaveSource
from wavr.camera_store import CameraStore
from wavr.rules import RulesEngine
from wavr.away import AwayMonitor
from wavr.mqtt_publisher import make_publisher
from wavr.narrator import Narrator, make_gemini_generate
from wavr.netinventory_service import NetworkInventoryService
from wavr.api_inventory import build_inventory_router
from wavr.sources.ble import BLESource
from wavr.devices import DeviceStore
from wavr.pairing import PairingManager
from wavr.auth import authorize, parse_bearer, can_change_state, in_subnet
from wavr.api_devices import build_pair_router, build_ws_ticket_router, build_devices_router


_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def _is_loopback(host) -> bool:
    return host in _LOOPBACK_HOSTS


def _default_sources(cfg):
    """Plano A real-source set: network always-on ($0), ruview always-on (harmless
    reconnect loop when the container is absent), sim off by default (toggle it on
    from the dashboard to populate the view when no real data is flowing). mmwave is
    only added when a serial port is configured (passive local serial, no frames
    otherwise) — but then it's always-on, same as network/ruview."""
    sources = [
        ("network", lambda: NetworkSource(
            cfg.net_known_macs, interval=cfg.net_interval, grace=cfg.net_grace), True),
        ("ruview", lambda: RuViewSource(
            cfg.ruview_url, room=cfg.ruview_room, reconnect_delay=cfg.ruview_reconnect), True),
        ("sim", lambda: SimulatedSource(interval=cfg.sim_interval), False),
    ]
    if cfg.mmwave_port:
        sources.append(
            ("mmwave", lambda: MmWaveSource(cfg.mmwave_room, cfg.mmwave_port), True))
    if cfg.ble_known:
        sources.append(("ble", lambda: BLESource(
            cfg.ble_known, room=cfg.ble_room, rssi_min=cfg.ble_rssi_min,
            interval=cfg.ble_interval), True))
    return sources


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_URL_SHAPE_RE = re.compile(r"^\w+://.+")
# Same-origin allowlist for the /ws/live handshake (browsers send Origin; native
# clients/tests send none). Blocks a drive-by cross-site page from opening the live
# targets/vitals stream. "testserver" matches the Host allowlist for the TestClient.
_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\]|testserver)(:\d+)?$")


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
    _house = load_house_map(cfg.house_map)

    # Rules/MQTT engine: opt-in via injected `rules_publish` (tests) or WAVR_MQTT_ENABLED
    # (real paho publisher, lazily connected). Off by default -- no publisher, no engine.
    _rules_publish = rules_publish
    if _rules_publish is None and cfg.mqtt_enabled:
        _rules_publish = make_publisher(cfg.mqtt_host, cfg.mqtt_port)
    _rules = RulesEngine(_rules_publish, prefix=cfg.mqtt_prefix) if _rules_publish else None
    _away = AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace) if _rules_publish else None

    # Narrator: opt-in via injected `narrator` (tests) or BOTH WAVR_NARRATE_ENABLED and
    # GEMINI_API_KEY (real Gemini generator, lazily imported). Off by default -- no
    # explicit opt-in, no narrator, 503 on call. The flag is a conscious two-factor
    # gate so merely having a key present (e.g. in ./.env) can't silently enable
    # cloud egress.
    _narrator = narrator
    if _narrator is None and cfg.narrate_enabled and cfg.gemini_api_key:
        _narrator = Narrator(make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model))

    # Wavr Net: defensive LAN inventory + rogue-device alerts (own-network only,
    # loopback-read). Runs its own periodic scan loop; port-awareness stays off
    # unless WAVR_NET_PORTSCAN (ADR-0004).
    _inventory = NetworkInventoryService(cfg.net_known_macs, interval=cfg.net_scan_interval)

    # Multi-device (ADR-0006): device/token store + pairing. ONLY built when
    # WAVR_MULTIDEVICE is on — otherwise it stays None so we don't open a third
    # connection to the db (avoids lock contention) and the middleware below is strict
    # loopback-only, byte-identical to before. `_local_ip` defines the "same /24" that
    # authenticated LAN peers must sit in.
    _local_ip = (_local_ipv4() or "127.0.0.1") if cfg.multidevice else "127.0.0.1"
    _devices = DeviceStore(cfg.db_path) if cfg.multidevice else None
    _pairing = PairingManager(_devices) if cfg.multidevice else None

    async def _ingest(event):
        rs = _fusion.update(event)
        d = rs.to_dict()
        await asyncio.to_thread(_storage.insert_state, rs)  # fsync off the event loop
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
        if cfg.net_inventory:
            await _inventory.start()   # opt-in (WAVR_NET_INVENTORY): real LAN scan loop
        if cfg.ha_discovery and _rules_publish:
            from wavr.ha_discovery import publish_ha_discovery
            publish_ha_discovery(
                _rules_publish,
                [r["name"] for r in _house.get("rooms", [])],
                prefix=cfg.mqtt_prefix,
            )
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
            await _inventory.stop()
            await manager.stop()
            if _owns_cameras:
                with suppress(Exception):
                    _cameras.close()
            if _devices is not None:
                with suppress(Exception):
                    _devices.close()

    app = FastAPI(title="Wavr", lifespan=lifespan)

    app.include_router(build_inventory_router(_inventory))
    def require_central(request: Request):
        # Device-management routes: only a 'central' (or the loopback root) may list or
        # revoke devices; a 'user' is read-only (audit C1). Applied via include_router
        # dependencies so it wraps every route in the devices router.
        if getattr(request.state, "role", None) not in ("root", "central"):
            raise HTTPException(status_code=403, detail="central role required")

    if cfg.multidevice:
        app.include_router(build_pair_router(_devices, _pairing))
        app.include_router(build_ws_ticket_router(_devices, _pairing))
        app.include_router(build_devices_router(_devices),
                           dependencies=[Depends(require_central)])

    # PRIVACY: the load-bearing access control. Default (WAVR_MULTIDEVICE off) is strict
    # loopback-only, enforced in code so it holds even under --host 0.0.0.0 ("testclient"
    # is the pytest peer). When multidevice is ON (ADR-0006), a same-/24 LAN peer with a
    # valid Bearer token is also allowed, and its role is attached to the request; loopback
    # is always "root". Off = byte-identical to before.
    @app.middleware("http")
    async def loopback_or_authed(request: Request, call_next):
        host = request.client.host if request.client else None
        if _is_loopback(host):                       # loopback (incl. TestClient) -> root
            request.state.role = "root"
            return await call_next(request)
        if not cfg.multidevice:                      # off: strict loopback-only, as before
            return JSONResponse({"detail": "loopback only"}, status_code=403)
        # Onboarding: /api/pair is reachable by an in-subnet peer WITHOUT a token
        # (that is the point of pairing; bounded by the one-time, rate-limited code).
        if request.url.path == "/api/pair":
            if in_subnet(host, _local_ip):
                request.state.role = None
                return await call_next(request)
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        token = parse_bearer(request.headers.get("authorization"))
        role = authorize(host, _local_ip, token, _devices)
        if role is None:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        request.state.role = role
        return await call_next(request)

    _allowed_hosts = ["localhost", "127.0.0.1", "testserver"]
    if cfg.multidevice:
        _allowed_hosts.append(_local_ip)   # LAN peers reach the central by its IP
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

    def require_local(request: Request):
        # State-changing routes. Loopback "root" (the local dashboard) still needs the
        # CSRF header (blocks drive-by browser POSTs). An authenticated LAN peer must be
        # 'central'; a 'user' is read-only. Off = same as before (everything is root).
        role = getattr(request.state, "role", None)
        if role == "root":
            if request.headers.get("x-wavr-local") != "1":
                raise HTTPException(status_code=403, detail="missing X-Wavr-Local header")
            return
        if not can_change_state(role):
            raise HTTPException(status_code=403, detail="central role required")

    @app.get("/api/history")
    async def history(limit: int = 200):
        return await asyncio.to_thread(_storage.recent, limit)

    @app.get("/api/state")
    async def state():
        return latest

    @app.get("/api/house")
    async def house():
        return _house

    @app.post("/api/narrate")
    async def narrate(_=Depends(require_local)):
        if _narrator is None:
            raise HTTPException(status_code=503, detail="narration not configured (set GEMINI_API_KEY)")
        try:
            text = await asyncio.to_thread(_narrator.narrate, latest, _storage.recent(50))
        except Exception:
            logging.exception("narrate failed")
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
        if not _NAME_RE.match(room):
            raise HTTPException(status_code=400, detail="room must be alphanumeric/_/-")
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

    if cfg.multidevice:
        @app.post("/api/pair-code")
        async def pair_code(role: str = Body("user", embed=True), _=Depends(require_local)):
            # Operator (loopback root / central) mints a one-time pairing code that a
            # companion then redeems at POST /api/pair. Gated by require_local.
            if role not in ("central", "user"):
                raise HTTPException(status_code=400, detail="role must be central or user")
            return {"code": _pairing.mint_code(role)}

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        host = ws.client.host if ws.client else None
        origin = ws.headers.get("origin")
        did = None   # authenticated device id for a LAN companion (None for loopback root)
        if cfg.multidevice and not _is_loopback(host):
            # LAN companion: WS isn't covered by the http middleware, so re-check the
            # subnet here (M2); a Bearer token can't ride a WS handshake, so require a
            # valid single-use ticket; and re-check the device wasn't revoked between
            # ticket mint and now (M1).
            if not in_subnet(host, _local_ip):
                await ws.close(code=1008)
                return
            ticket = ws.query_params.get("ticket")
            did = _pairing.redeem_ticket(ticket) if ticket else None
            if did is None:
                await ws.close(code=1008)
                return
            dev = _devices.get(did)
            if dev is None or dev.revoked:
                await ws.close(code=1008)
                return
        else:
            # Loopback (or multidevice off): unchanged — loopback peer + Origin allowlist.
            if not _is_loopback(host):
                await ws.close(code=1008)  # WS isn't covered by the http middleware
                return
            if origin is not None and not _ORIGIN_RE.match(origin):
                await ws.close(code=1008)  # cross-site WS: block drive-by reads
                return
        await ws.accept()
        q = _hub.subscribe()
        try:
            n = 0
            while True:
                await ws.send_json(await q.get())
                n += 1
                if did is not None and n % 50 == 0:   # M1: drop an open stream on revoke
                    dev = _devices.get(did)
                    if dev is None or dev.revoked:
                        break
        except WebSocketDisconnect:
            pass
        finally:
            _hub.unsubscribe(q)

    @app.get("/")
    async def dashboard():
        return FileResponse(_INDEX)

    return app


app = create_app()
