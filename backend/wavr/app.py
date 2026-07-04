from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr import __version__
from wavr.config import load_config
from wavr.housemap import load_house_map, room_names, save_house_map, HouseMapError
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
from wavr.notifier import make_notifier
from wavr.narrator import Narrator, make_gemini_generate
from wavr.netinventory_service import NetworkInventoryService
from wavr.api_inventory import build_inventory_router
from wavr.device_meta import DeviceMeta
from wavr.ha_client import client_from_config
from wavr.ha_import import fetch_registry, import_devices
from wavr.ha_import_store import HAImportStore
from wavr.internet_monitor import InternetMonitor, guess_gateway, make_checker
from wavr.dhcp_monitor import RogueDhcpMonitor, make_collector as make_dhcp_collector
from wavr.gateway_monitor import GatewayIdentityMonitor, GatewayBindingStore
from wavr.health_check import check_health, default_resolver_checkers, default_extra_checkers
from wavr.presence_report import build_report
from wavr.sources.ble import BLESource
from wavr.devices import DeviceStore
from wavr.pairing import PairingManager
from wavr.auth import authorize, parse_bearer, can_change_state, in_subnet
from wavr.api_devices import build_pair_router, build_ws_ticket_router, build_devices_router


_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
_VENDOR_DIR = _INDEX.parent / "vendor"
_CATALOG_PATH = _VENDOR_DIR / "device-catalog.json"


def _load_device_catalog() -> list:
    """Read the static offline device catalog (a repo asset -- safe to read
    server-side) for HA-import catalog matching. Defensive: any read/parse
    failure or an unexpected shape -> `[]`, never a crash (A4.1 catalog match is
    advisory UI enrichment, never load-bearing)."""
    try:
        import json
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        logging.warning("device catalog unavailable for HA import", exc_info=True)
        return []


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
# Scheme is restricted to rtsp(s) -- the URL is handed straight to cv2.VideoCapture,
# so allowing arbitrary schemes (http://, file://, etc.) would let a caller point it
# at internal/metadata endpoints or the local filesystem (SSRF/LFI via camera add).
_URL_SHAPE_RE = re.compile(r"^rtsps?://.+", re.IGNORECASE)
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
               rules_publish=None, narrator=None, notify=None, device_meta=None,
               internet_monitor=None, health_check=None, dhcp_monitor=None,
               health_resolvers=None, gateway_monitor=None,
               ha_import_store=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold)
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)
    _house = load_house_map(cfg.house_map)

    # Notifier: opt-in via injected `notify` (tests) or WAVR_NTFY_URL (self-hosted
    # ntfy, stdlib POST, lazily built). Off by default -- no notifier, no HTTP calls.
    # Sends ONLY derived edge events (house arrived/left, rogue-device) -- never
    # targets/vitals/frames/MACs.
    _notify = notify
    if _notify is None and cfg.ntfy_url:
        _notify = make_notifier(cfg.ntfy_url)

    # Rules/MQTT engine: opt-in via injected `rules_publish` (tests) or WAVR_MQTT_ENABLED
    # (real paho publisher, lazily connected). Off by default -- no publisher, no engine.
    _rules_publish = rules_publish
    if _rules_publish is None and cfg.mqtt_enabled:
        _rules_publish = make_publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix)
    _rules = RulesEngine(_rules_publish, prefix=cfg.mqtt_prefix) if _rules_publish else None
    # AwayMonitor runs whenever MQTT OR ntfy is opt-in'd -- both consumers need the
    # SAME house-level arrived/left edge detection. `_rules_publish` stays optional
    # (AwayMonitor no-ops its own `publish` when None) so an ntfy-only setup gets
    # notified without also needing WAVR_MQTT_ENABLED.
    _away = (AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace,
                         notify=_notify)
             if (_rules_publish or _notify) else None)

    # Narrator: opt-in via injected `narrator` (tests) or BOTH WAVR_NARRATE_ENABLED and
    # GEMINI_API_KEY (real Gemini generator, lazily imported). Off by default -- no
    # explicit opt-in, no narrator, 503 on call. The flag is a conscious two-factor
    # gate so merely having a key present (e.g. in ./.env) can't silently enable
    # cloud egress.
    _narrator = narrator
    if _narrator is None and cfg.narrate_enabled and cfg.gemini_api_key:
        _narrator = Narrator(make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model))

    # Device metadata (Feature A): persisted per-MAC name + first/last-seen,
    # always built (like CameraStore) -- not itself opt-in, since naming is not
    # sensitive and the store is inert until something calls seen()/set_name().
    _owns_device_meta = device_meta is None
    _device_meta = device_meta or DeviceMeta(cfg.db_path)

    # HA-import store (A4.1): persisted per-MAC identity imported from the local
    # Home Assistant device registry, always built (like device_meta) -- inert
    # until POST /api/ha/import runs. Fed back into every LAN scan as the recog
    # `ha` signal (A4.0). Lives in wavr.db (git-ignored) so HA-derived home data
    # never lands in this public repo.
    _owns_ha_store = ha_import_store is None
    _ha_import_store = ha_import_store or HAImportStore(cfg.db_path)
    # Static device catalog (loaded once) for HA-import catalog matching.
    _catalog = _load_device_catalog()

    # Gateway-MAC-identity tracker (gateway-identity-rogue-dhcp, inventory feature #2):
    # ON by default (cfg.net_gateway_monitor) -- unlike every active collector it
    # opens NO socket and makes ZERO egress (it only consumes the is_gateway
    # binding scan_inventory already produced from THIS host's routing table), so
    # it needs no shared-subnet opt-in and is Wavr's headline privacy edge vs
    # a proprietary tool's cloud-brained version. Injected `gateway_monitor` (tests) wins;
    # otherwise built with a GatewayBindingStore so the trusted baseline survives
    # restarts (inventory feature #7 -- an in-memory baseline would re-adopt a spoof at
    # restart). on_alert shares the SAME opt-in ntfy `notify` as every other
    # alert, derived-only (gateway IP, never the MAC/credential).
    _owns_gateway_store = False
    _gateway_store = None
    _gateway_monitor = gateway_monitor
    if _gateway_monitor is None and cfg.net_gateway_monitor:
        _gateway_store = GatewayBindingStore(cfg.db_path)
        _owns_gateway_store = True
        _gateway_monitor = GatewayIdentityMonitor(
            store=_gateway_store,
            known_macs=cfg.net_gateway_known_macs or None,
            on_alert=(lambda a: _notify(f"Wavr: identidade do gateway mudou ({a.gateway_ip})"))
            if _notify else None,
        )

    # Wavr Net: defensive LAN inventory + rogue-device alerts (own-network only,
    # loopback-read). Runs its own periodic scan loop; port-awareness stays off
    # unless WAVR_NET_PORTSCAN (ADR-0004). `on_rogue` fires the opt-in ntfy alert on
    # the SAME edge-triggered rogue sighting the alert log records -- vendor only,
    # never the MAC/IP. `device_meta` folds every scanned MAC into the persisted
    # first-seen/last-seen store (Feature A).
    _inventory = NetworkInventoryService(
        cfg.net_known_macs, interval=cfg.net_scan_interval,
        on_rogue=(lambda a: _notify(f"Wavr: dispositivo desconhecido na rede ({a.vendor})"))
        if _notify else None,
        device_meta=_device_meta,
        # Passive protocol collectors (defensive-inventory collectors) -- opt-in, default
        # OFF; only ever run when the operator sets WAVR_NET_MDNS/WAVR_NET_SSDP.
        mdns_enabled=cfg.net_mdns, ssdp_enabled=cfg.net_ssdp,
        ssdp_location_enabled=cfg.net_ssdp_location,
        collect_duration=cfg.net_collect_duration,
        # NetBIOS/SNMP (defensive-inventory #5/#8) + DHCP fingerprint (#6) -- opt-in,
        # default OFF (collectors-lote2). Unlike WAVR_NET_PORTSCAN_SCOPE
        # (default OFF -- scans every ARP host unless explicitly narrowed),
        # the NetBIOS/SNMP scope flags default to known-only and require an
        # explicit SCOPE=all to widen (audit fix #4: an active unicast probe
        # is more intrusive than a connect scan); the SNMP community is
        # read-only-by-construction and never logged.
        netbios_enabled=cfg.net_netbios, netbios_scope_known_only=cfg.net_netbios_scope_known_only,
        snmp_enabled=cfg.net_snmp, snmp_community=cfg.net_snmp_community,
        snmp_scope_known_only=cfg.net_snmp_scope_known_only,
        dhcp_fp_enabled=cfg.net_dhcp_fp,
        # Reverse-DNS hostname resolution (gateway-anchored PTR) -- opt-in,
        # default OFF; only queries the LAN gateway resolver when enabled.
        hostname_resolve_enabled=cfg.net_hostnames,
        # Per-device latency (WiFiman parity, wifiman.md #1) -- opt-in, default
        # OFF; actively TCP-connects each host so it is gated like the port pass.
        latency_enabled=cfg.net_latency,
        # Gateway-identity flag (wifiman.md #2) -- reads THIS host's routing
        # table only (zero egress, no neighbour touch), so on unconditionally.
        gateway_detect_enabled=True,
        # Gateway-MAC-identity tracker (inventory feature #2): each scan feeds this
        # cycle's is_gateway binding into the debounced monitor built above.
        gateway_monitor=_gateway_monitor,
        # HA-import identity (A4.1): each scan folds the user-imported HA
        # registry back in as the recog `ha` signal (medium-capped, A4.0).
        ha_store=_ha_import_store)

    # Internet/gateway monitor (Feature B): opt-in via injected `internet_monitor`
    # (tests) or WAVR_INTERNET_MONITOR (real gateway ping, lazily built). Off by
    # default -- no monitor, no background task, no pings. Shares the same
    # opt-in `notify` as AwayMonitor/rogue-device alerts (ntfy, derived-only).
    _internet = internet_monitor
    if _internet is None and cfg.internet_monitor:
        _internet = InternetMonitor(
            host=cfg.internet_check_host or None,
            interval=cfg.internet_check_interval,
            fail_threshold=cfg.internet_fail_threshold,
            notify=_notify,
        )

    # Rogue/multiple-DHCP-server detector (defensive-inventory #7, collectors-lote2):
    # opt-in via injected `dhcp_monitor` (tests) or WAVR_NET_DHCP_MONITOR (real
    # DHCP snoop, lazily built). Off by default -- no monitor, no background
    # task, no packets. Shares the same opt-in ntfy `notify` as every other
    # alert (rogue-device, internet down) -- derived-only (server IP, never a
    # MAC/credential).
    _dhcp_monitor = dhcp_monitor
    if _dhcp_monitor is None and cfg.net_dhcp_monitor:
        _dhcp_monitor = RogueDhcpMonitor(
            collect=make_dhcp_collector(collect_duration=cfg.net_collect_duration,
                                        probe=cfg.net_dhcp_probe),
            known_servers=cfg.net_dhcp_known_servers or None,
            interval=cfg.net_dhcp_interval,
            alert_threshold=cfg.net_dhcp_alert_threshold,
            on_rogue=(lambda a: _notify(f"Wavr: servidor DHCP desconhecido na rede ({a.extra_server})"))
            if _notify else None,
        )

    # GET /api/health (5-tier ladder, defensive-inventory #12): an on-demand,
    # read-only gateway + DNS-resolver + operator-extra-target check -- NOT
    # gated behind the internet_monitor opt-in, since it is a single
    # caller-triggered check (a GET), not a new background scanner. Same
    # LOCAL-ONLY default as InternetMonitor: with zero config the gateway leg
    # pings the LAN gateway (never a fixed cloud host). Audit fix #1: the
    # resolver legs are the one part of this route that makes real
    # public-internet egress, so they are gated behind `WAVR_HEALTH_RESOLVERS`
    # (default OFF -- an empty resolver dict, severity computed from gateway +
    # extra targets only, see wavr.health_check's module docstring); a bare
    # Docker HEALTHCHECK/uptime monitor hitting this route no longer silently
    # pings three US cloud providers. `health_check`/`health_resolvers` are
    # the injectable transports (tests inject fakes -- no real network).
    _health_host = cfg.internet_check_host or guess_gateway()
    _health_check = health_check or make_checker(_health_host or "127.0.0.1")
    _health_resolvers = (
        health_resolvers if health_resolvers is not None
        else (default_resolver_checkers() if cfg.health_resolvers_enabled else {})
    )
    _health_extra = default_extra_checkers(cfg.health_extra_targets)

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
        if _internet:
            await _internet.start()    # opt-in (WAVR_INTERNET_MONITOR or injected): gateway ping loop
        if _dhcp_monitor:
            await _dhcp_monitor.start()   # opt-in (WAVR_NET_DHCP_MONITOR or injected): DHCP snoop loop
        if cfg.ha_discovery and _rules_publish:
            from wavr.ha_discovery import publish_ha_discovery
            publish_ha_discovery(
                _rules_publish,
                room_names(_house),
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
            if _internet:
                await _internet.stop()
            if _dhcp_monitor:
                await _dhcp_monitor.stop()
            await manager.stop()
            if _owns_cameras:
                with suppress(Exception):
                    _cameras.close()
            if _owns_device_meta:
                with suppress(Exception):
                    _device_meta.close()
            if _owns_ha_store:
                with suppress(Exception):
                    _ha_import_store.close()
            if _owns_gateway_store and _gateway_store is not None:
                with suppress(Exception):
                    _gateway_store.close()
            if _devices is not None:
                with suppress(Exception):
                    _devices.close()

    app = FastAPI(title="Wavr", lifespan=lifespan)

    def require_central(request: Request):
        # Device-management routes: only a 'central' (or the loopback root) may list or
        # revoke devices; a 'user' is read-only (audit C1). Applied via include_router
        # dependencies so it wraps every route in the devices router (GET + DELETE).
        role = getattr(request.state, "role", None)
        if role not in ("root", "central"):
            raise HTTPException(status_code=403, detail="central role required")

    def require_csrf_root(request: Request):
        # CSRF guard for STATE-CHANGING device routes (DELETE only -- the GET list is a
        # read and needs no CSRF). Same rule as every other state-changing route: the
        # loopback 'root' additionally needs the X-Wavr-Local header, so a same-origin
        # browser drive-by `fetch('/api/devices/x',{method:'DELETE'})` can't revoke a
        # device using just the operator's session. A token-authed LAN central is
        # header-independent and unaffected.
        role = getattr(request.state, "role", None)
        if role == "root" and request.headers.get("x-wavr-local") != "1":
            raise HTTPException(status_code=403, detail="missing X-Wavr-Local header")

    if cfg.multidevice:
        app.include_router(build_pair_router(_devices, _pairing))
        app.include_router(build_ws_ticket_router(_devices, _pairing))
        app.include_router(
            build_devices_router(_devices, delete_deps=[Depends(require_csrf_root)]),
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
        # Static shell (index + PWA manifest/sw/icon + vendored three.js): reachable by an
        # in-subnet peer WITHOUT a token, because the companion must LOAD the page to pair
        # and these carry nothing sensitive (the page shows only the pairing screen until a
        # token is entered). The DATA endpoints (/api/*, /ws/*) still require the token.
        # "/index.html" is the same shell as "/" (H3 audit fix: sw.js precaches it by name).
        _p = request.url.path
        if _p in ("/", "/index.html", "/manifest.webmanifest", "/sw.js", "/icon.svg") or _p.startswith("/vendor/"):
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

    # Self-hosted three.js (3D house view): same-origin static mount, zero external
    # requests. Scoped to /vendor only -- does not touch "/" or the pre-existing
    # manifest/sw/icon gap. Sits behind loopback_or_authed like every other route, so
    # a LAN companion still needs to be an authenticated peer under WAVR_MULTIDEVICE.
    app.mount("/vendor", StaticFiles(directory=_VENDOR_DIR), name="vendor")

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

    # PUT /api/inventory/name is state-changing (Feature A) -- gated by the same
    # require_local rule as the camera/system/pair-code routes, so registration
    # happens here (after require_local is defined) rather than up near the
    # other include_router calls.
    app.include_router(build_inventory_router(
        _inventory, device_meta=_device_meta, name_deps=[Depends(require_local)],
        dhcp_monitor=_dhcp_monitor, gateway_monitor=_gateway_monitor))

    @app.get("/api/history")
    async def history(limit: int = 200):
        # Clamp: a negative limit means "no limit" to SQLite's `LIMIT ?` (full-table
        # dump), and an unbounded positive value is still a resource-exhaustion risk.
        limit = max(1, min(limit, 1000))
        return await asyncio.to_thread(_storage.recent, limit)

    @app.get("/api/state")
    async def state():
        return latest

    @app.get("/api/house")
    async def house():
        return _house

    @app.put("/api/house")
    async def put_house(doc: dict = Body(...), _=Depends(require_local)):
        try:
            save_house_map(cfg.house_map, doc)
        except HouseMapError as exc:
            # empty-path -> 409 (server misconfig); invalid doc -> 422.
            code = 409 if "no house_map path" in str(exc) else 422
            raise HTTPException(status_code=code, detail=str(exc))
        _house.clear()
        _house.update(doc)          # keep the in-memory map (GET, room_names) in sync
        return _house

    @app.post("/api/narrate")
    async def narrate(_=Depends(require_local)):
        if _narrator is None:
            raise HTTPException(status_code=503, detail="narration not configured (set GEMINI_API_KEY)")
        try:
            rows = await asyncio.to_thread(_storage.recent, 50)
            text = await asyncio.to_thread(_narrator.narrate, latest, rows)
        except Exception:
            logging.exception("narrate failed")
            raise HTTPException(status_code=502, detail="narration backend error")
        return {"narration": text}

    @app.post("/api/ha/import")
    async def ha_import(dry_run: bool = Body(False, embed=True),
                        _=Depends(require_local)):
        # A4.1 HA -> Wavr registry import. USER-TRIGGERED ONLY (never a timer),
        # gated by require_local (CSRF), local-HA-only + SSRF-safe (wavr.ha_import
        # only ever contacts the configured ha_url). The HA token is read from
        # config here and passed to the transport only -- it is NEVER in the
        # response or any error string below.
        if not cfg.ha_import:
            raise HTTPException(status_code=403,
                                detail="HA import disabled (WAVR_HA_IMPORT=0)")
        if client_from_config(cfg) is None:
            # HA not configured (empty ha_url/ha_token) -> nothing to import, no write.
            raise HTTPException(status_code=400,
                                detail="Home Assistant not configured (set WAVR_HA_URL + WAVR_HA_TOKEN)")
        try:
            registry = await fetch_registry(cfg.ha_url, cfg.ha_token)
        except Exception as exc:
            # WavrHAError (unreachable / bad token / bad url) -- the message never
            # carries the token (wavr.ha_import guarantees it); surface as 502.
            logging.warning("HA import fetch failed: %s", exc)
            raise HTTPException(status_code=502,
                                detail="Home Assistant registry unreachable")
        summary = await asyncio.to_thread(
            import_devices, registry, _catalog, _ha_import_store, dry_run)
        return summary

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    async def status():
        # READ-ONLY, NO SECRETS: sources are name+active only (no rtsp/mac), features
        # are opt-in booleans only (no urls/tokens), house is a bare count. Gated by
        # the same loopback_or_authed middleware as every other GET route.
        return {
            "version": __version__,
            "sources": [
                {"name": s["name"], "active": s["active"]}
                for s in manager.status()["sources"]
            ],
            "features": {
                "multidevice": cfg.multidevice,
                "mqtt": cfg.mqtt_enabled,
                "ha_discovery": cfg.ha_discovery,
                "mcp_control": cfg.mcp_control,
                "narrate": cfg.narrate_enabled,
                "net_inventory": cfg.net_inventory,
                # TLS is coupled 1:1 to multidevice mode (see serve.py: HTTPS/WSS is
                # only enabled when WAVR_MULTIDEVICE is on).
                "tls": cfg.multidevice,
                "ntfy": bool(cfg.ntfy_url),
                "internet_monitor": cfg.internet_monitor,
                # Passive/active protocol collectors (defensive-inventory collectors +
                # collectors-lote2) -- every one opt-in, default OFF; surfaced
                # here so the frontend can show which signal sources are live.
                "mdns": cfg.net_mdns,
                "ssdp": cfg.net_ssdp,
                "netbios": cfg.net_netbios,
                "snmp": cfg.net_snmp,
                "dhcp_fp": cfg.net_dhcp_fp,
                "rogue_dhcp": cfg.net_dhcp_monitor,
                # Gateway-MAC-identity tracker (inventory feature #2) -- the one signal
                # here that is ON by default (zero-egress, on-box); surfaced so
                # the Privacy & Egress view stays honest about what is live.
                "gateway_monitor": cfg.net_gateway_monitor,
                # Audit fix #1: the ONLY egress path in this dict that isn't a
                # dedicated background collector -- GET /api/health's public-
                # DNS-resolver legs, opt-in via WAVR_HEALTH_RESOLVERS. Surfaced
                # here so the Privacy & Egress dashboard stays honest about it.
                "health_resolvers": cfg.health_resolvers_enabled,
            },
            "house": {
                "floors": len(_house.get("floors", [])),
                "rooms": len(room_names(_house)),
            },
            # Feature B: current internet/gateway reachability. Null/null when
            # the monitor is off (or hasn't completed its first check yet).
            "internet": _internet.status() if _internet else {"ok": None, "since": None},
        }

    @app.get("/api/presence/report")
    async def presence_report():
        # Pure aggregation of wavr.device_meta's first/last-seen store (Feature
        # A) -- no new scanning, no I/O beyond the existing sqlite read (same
        # synchronous-call convention netinventory_service already uses for
        # this same store). Safe to call on every GET.
        return build_report(_device_meta)

    @app.get("/api/health")
    async def health():
        # On-demand only -- no background task, no new opt-in flag (see the
        # _health_check/_health_resolvers construction above for the
        # LOCAL-ONLY rationale). 5-tier severity ladder (defensive-inventory #12):
        # gateway + public-resolver reachability + optional operator-extra
        # targets, rolled into one severity verdict (wavr.health_check).
        result = await check_health(
            gateway_check=_health_check, gateway_host=_health_host,
            resolver_checks=_health_resolvers, extra_checks=_health_extra,
        )
        result["internet_monitor"] = _internet.status() if _internet else None
        return result

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
            raise HTTPException(status_code=400, detail="rtsp_url must be rtsp:// or rtsps://")
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

    # sw.js precaches "./index.html" by name (Cache.addAll is all-or-nothing), but only
    # "/" was ever registered -- so that entry 404'd and the service worker never
    # installed on the live origin (H3 audit fix). Same response as "/"; exempted from
    # the token gate the same way "/" is (see loopback_or_authed above).
    @app.get("/index.html")
    async def dashboard_index_html():
        return FileResponse(_INDEX)

    # PWA shell files, served same-origin so the app installs + caches without any
    # external request (the SW registers, the manifest resolves, the icon loads). These
    # are the static shell; like "/" they carry nothing sensitive.
    _FRONTEND = _INDEX.parent

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(_FRONTEND / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(_FRONTEND / "sw.js", media_type="text/javascript")

    @app.get("/icon.svg")
    async def icon():
        return FileResponse(_FRONTEND / "icon.svg", media_type="image/svg+xml")

    return app


app = create_app()
