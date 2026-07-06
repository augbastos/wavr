from __future__ import annotations

import asyncio
import copy
import hmac
import ipaddress
import logging
import math
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr import __version__
from wavr.config import load_config
from wavr.housemap import load_house_map, room_names, save_house_map, upsert_room, HouseMapError
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sourcemanager import SourceManager
from wavr.sources.simulated import SimulatedSource
from wavr.sources.network import NetworkSource, _local_ipv4
from wavr.sources.phone import PhoneSensorSource
from wavr.sources.ruview import RuViewSource
from wavr.sources.camera import CameraSource
from wavr.sources.mmwave import MmWaveSource
from wavr.camera_store import CameraStore
from wavr.camera_health import CameraHealthMonitor
from wavr.camera_url import rebind_rtsp_host, rtsp_host
from wavr.netaddr import is_lan_ip
from wavr.rules import RulesEngine
from wavr.away import AwayMonitor
from wavr.mqtt_publisher import make_publisher
from wavr.notifier import make_notifier
from wavr.narrator import Narrator, make_gemini_generate
from wavr.netinventory_service import NetworkInventoryService
from wavr.api_inventory import build_inventory_router
from wavr.device_meta import DeviceMeta, normalize_mac
from wavr.netinventory import _same_ip
from wavr.ha_client import client_from_config
from wavr.ha_import import fetch_registry, import_devices
from wavr.ha_import_store import HAImportStore
from wavr.internet_monitor import InternetMonitor, guess_gateway, make_checker
from wavr.dhcp_monitor import RogueDhcpMonitor, make_collector as make_dhcp_collector
from wavr.gateway_monitor import GatewayIdentityMonitor, GatewayBindingStore
from wavr.health_check import check_health, default_resolver_checkers, default_extra_checkers
from wavr.presence_report import build_report
from wavr import wol, diagnostics, speedtest as speedtest_mod
from wavr.sources.onvif import ONVIFProbe
from wavr.ptz import CameraPTZ
from wavr.sources.ble import BLESource
from wavr.devices import DeviceStore, VALID_CONSENT
from wavr.pairing import PairingManager
from wavr.telemetry import TelemetryHub, TelemetryReading, TelemetryPayload, PerDeviceRateLimiter
from wavr.auth import authorize, parse_bearer, can_change_state, in_subnet
from wavr.api_devices import build_pair_router, build_ws_ticket_router, build_devices_router
from wavr.local_token import resolve_local_token
from wavr import arp_block


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


# A5.1: paths reachable WITHOUT the optional local-API token (bootstrap shell + PWA
# assets + liveness). Everything else under loopback requires the token when one is
# set -- deliberately stricter than require_local (which only guards state-changers):
# the point is to stop a same-machine process from even READING inventory/PII.
_TOKEN_EXEMPT_PATHS = frozenset({
    "/", "/index.html", "/measure.html", "/manifest.webmanifest", "/sw.js", "/icon.svg",
    "/healthz",
})


def _is_token_exempt(path: str) -> bool:
    return path in _TOKEN_EXEMPT_PATHS or path.startswith("/vendor/")


# Mobile unification (blueprint step 3): the ONLY authenticated path a write-only
# 'sensor'-role token may reach. Anything else -> 403 at the single choke point in
# loopback_or_authed below, so a stolen sensor token can inject its own telemetry and
# read NOTHING (no history/state/house/inventory/devices/cameras, no /ws/live). The
# pre-auth exemptions (static shell, /api/pair) are already handled earlier in the
# middleware -- they resolve to role=None, never role='sensor' -- so they need no entry
# here. user/central/root roles never touch this check.
#
# /api/consent is LOAD-BEARING here (GDPR withdrawal): a sensor-only node MUST be able to
# lower/raise its OWN consent tier -- without this entry a sensor hits the confinement 403
# and can never withdraw. It is write-only + self-scoped (derives its device_id from its
# own token, carries no device field), so it opens NO read surface.
_SENSOR_ALLOWED_PATHS = frozenset({"/api/telemetry", "/api/consent"})


class _ConsentBody(BaseModel):
    """Body for POST /api/consent: `{"level": "green"|"yellow"|"red"}`. `extra='forbid'`
    so a body that tries to smuggle a `device`, `consent`, or any other field is rejected
    422 -- a device may set only its OWN consent LEVEL (its device_id comes from its token,
    never the body). The level string itself is validated against VALID_CONSENT in the
    handler (single source of truth), which returns 422 for an out-of-set value."""

    model_config = ConfigDict(extra="forbid")
    # max_length bounds the allocation for a hostile oversized body (the valid values are
    # <= 6 chars); an out-of-set-but-short value is caught by the VALID_CONSENT check.
    level: str = Field(..., max_length=16)


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
# ONVIF PTZ preset tokens (A4.3): the token is XML-escaped in the SOAP body anyway,
# but reject obviously-junk tokens early so a hostile id can't reach a log/traceback.
_PRESET_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,100}$")
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


def _rebind_ip_ok(ip: str) -> bool:
    """SSRF guard for the F3 rebind target: a bare PRIVATE/LAN IPv4 literal ONLY.
    Reuses the shared wavr.netaddr.is_lan_ip (literal-only + cloud-metadata denylist +
    IPv4-mapped-IPv6 normalization -- deliberately stronger than bare
    ipaddress.is_private, which accepts 169.254.169.254 and 127.0.0.1) and additionally
    requires a plain IPv4 literal so rebind_rtsp_host can rewrite the host
    unambiguously (no IPv6-bracket case). Rejects public IPs, DNS hostnames, cloud-
    metadata and IPv4-mapped-IPv6 forms."""
    h = (ip or "").strip()
    if not is_lan_ip(h):
        return False
    try:
        return isinstance(ipaddress.ip_address(h), ipaddress.IPv4Address)
    except ValueError:
        return False


def _camera_factory(cam: dict, cfg, on_health=None):
    # F3: pass the camera name + the health monitor's report callback + the unhealthy
    # threshold so a drifted/dead camera is edge-reported (name+bool only, never a
    # frame -- ADR-0002). `on_health` is None for callers that don't wire the monitor.
    return lambda: CameraSource(cam["room"], cam["rtsp_url"], name=cam["name"],
                                interval=cfg.cam_interval, confidence=cam["confidence"],
                                on_health=on_health,
                                unhealthy_secs=cfg.cam_unhealthy_secs)


def _json_safe(value):
    """Recursively replace non-finite floats (NaN/+Inf/-Inf) with their string form so a
    structure is safe for Starlette's JSONResponse.render, which calls
    json.dumps(..., allow_nan=False) and raises ValueError on a bare NaN/Inf.

    This is the load-bearing half of the non-finite-telemetry fix: FastAPI's default
    RequestValidationError handler echoes the OFFENDING input back in each error's
    "input"/"ctx" fields. When a client sends a raw body like `{"battery_pct": NaN}`
    (Starlette's json.loads accepts the literal), that NaN reaches the 422 render and turns
    it into a 500 + traceback -- breaking the "malformed -> 4xx, never 500" invariant. Only
    non-finite floats are rewritten; every ordinary value passes through unchanged, so the
    normal 422 body shape is preserved byte-for-byte. Applied app-wide because the footgun
    affects every float-validated route, not just /api/telemetry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None,
               rules_publish=None, narrator=None, notify=None, device_meta=None,
               internet_monitor=None, health_check=None, dhcp_monitor=None,
               health_resolvers=None, gateway_monitor=None,
               ha_import_store=None,
               wol_send=None, ping_probe=None, traceroute_runner=None,
               dns_query_fn=None, speedtest_fn=None,
               onvif_discover=None, onvif_soap=None, ptz_soap=None, arp_send=None,
               net_inventory=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold)
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)
    # deepcopy: load_house_map returns the module-level housemap.DEFAULT_MAP object
    # itself on any fallback (missing/invalid file), and put_house below mutates _house
    # in place (clear/update). Without this copy, the first PUT on a fresh install --
    # now that WAVR_HOUSE_MAP defaults to a (usually not-yet-existing) "house.json" --
    # would corrupt DEFAULT_MAP process-wide. Copy once so _house is always private.
    _house = copy.deepcopy(load_house_map(cfg.house_map))

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
    # `net_inventory` is a test seam (mirrors sources=/storage=/device_meta=): when
    # provided it replaces the built service so a route test can seed a deterministic
    # inventory (e.g. exercise POST /api/block's 200 success path). None in production.
    _inventory = net_inventory or NetworkInventoryService(
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

    # Phone-telemetry ingest hub (blueprint step 2): the bounded queue POST /api/telemetry
    # offers readings onto and PhoneSensorSource (step 4) drains. Built HERE -- before the
    # source register loop -- so the phone factory below binds the SAME instance that the
    # route offers onto; attached to app.state further down. Built even when multidevice is
    # off (inert; the /api/telemetry route itself is multidevice-gated).
    _telemetry_hub = TelemetryHub()

    manager = SourceManager(_ingest)
    for name, factory, enabled in (sources if sources is not None else _default_sources(cfg)):
        manager.register(name, factory, enabled)
    # PhoneSensorSource (blueprint step 4): folds paired-phone telemetry into fusion as a
    # coarse whole-home ('casa') presence vote. Multidevice-gated -- when off it is NEVER
    # registered, so the single-device source set stays byte-identical. It consumes the
    # shared telemetry hub and resolves the who's-home operator label via DeviceStore; the
    # label/rssi never ride the SensingEvent (privacy invariant).
    if cfg.multidevice:
        manager.register("phone", lambda: PhoneSensorSource(
            _telemetry_hub, get_label=_devices.get_label,
            get_consent=_devices.get_consent), True)

    _owns_cameras = camera_store is None   # only close a store this function built itself
    _cameras = camera_store or CameraStore(cfg.db_path)
    # F3 camera IP-drift monitor: always available (like _device_meta), inert until a
    # camera reports down AND a stored MAC drifts. Reads camera defs from _cameras and
    # the current LAN devices from _inventory (opt-in WAVR_NET_INVENTORY -- when off,
    # latest_inventory() is empty and suggestions stay honestly empty).
    _camera_health = CameraHealthMonitor(
        get_camera=_cameras.get, latest_inventory=_inventory.latest_inventory)
    for cam in _cameras.list():                       # persisted cameras -> boot-OFF sources
        manager.register(cam["name"], _camera_factory(cam, cfg, _camera_health.report), False)

    def _masked_cameras():
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    def _resolve_mac_for_url(rtsp_url: str) -> str | None:
        # F3 best-effort MAC capture at add/rebind time: match the rtsp host IP against
        # the running inventory (Device.ip -> Device.mac). LOCAL-only (ARP-based
        # inventory, zero egress). Returns None when net_inventory is off or no host
        # matches -- honest, never guesses. Never logs the url (carries credentials).
        host = rtsp_host(rtsp_url)
        if not host:
            return None
        try:
            for d in _inventory.latest_inventory():
                if d.ip and _same_ip(d.ip, host):
                    return d.mac
        except Exception:
            return None
        return None

    # ONVIF PTZ actuator (A4.3): opt-in (cfg.ptz, default OFF). Inert until a
    # /api/ptz/* route runs -- it reads creds only from a stored camera's rtsp_url,
    # contacts only LAN-IP hosts, and reads NO frame. `ptz_soap` is the test seam.
    _ptz = CameraPTZ(soap=ptz_soap)

    # A5.1 hardening: resolve the optional local-API token (default "" => disabled =>
    # every check below is a no-op, byte-identical to before) and the /api/v1 alias
    # flag. A5.2: the ARP blocker -- inert unless WAVR_NET_BLOCKING is on AND an elevated
    # arp_send transport is injected (the route 503s otherwise, never a silent no-op).
    _local_token = resolve_local_token(cfg.local_token, cfg.db_path)
    _api_v1 = cfg.api_v1
    _block_local_ip = _local_ipv4() or ""
    _blocker = arp_block.ArpBlocker(send=arp_send)

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
            with suppress(Exception):
                await _blocker.stop()   # A5.2: undo every active block on shutdown
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

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        # JSON-SAFE 422 render (app-wide). FastAPI's default handler returns
        # {"detail": jsonable_encoder(exc.errors())} -- but exc.errors() echoes the caller's
        # OFFENDING input, and a raw body like `{"battery_pct": NaN}` (Starlette's json.loads
        # accepts the NaN/Infinity literals; `1e400` parses to +Inf) leaves a non-finite float
        # in that echo. Starlette's JSONResponse.render then does json.dumps(allow_nan=False)
        # -> ValueError -> 500 + traceback, breaking "malformed -> 4xx, never 500". We mirror
        # the default shape EXACTLY and only sanitize non-finite floats, so ordinary
        # validation errors keep their usual detail list untouched. Same 422 status.
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
        )

    # Phone-telemetry ingest seam (blueprint §4). The hub (built above, before the source
    # register loop, so PhoneSensorSource binds the SAME instance) is attached here; the
    # limiter throttles POST /api/telemetry per authenticated device. Both live on
    # app.state as a single per-app instance the handler reads and tests can inspect/
    # override (a stolen sensor token flooding telemetry trips the limiter -> 429). Built
    # even when multidevice is off (inert objects; the /api/telemetry route itself is gated).
    app.state.telemetry_hub = _telemetry_hub
    app.state.telemetry_limiter = PerDeviceRateLimiter(
        capacity=cfg.telemetry_rate_capacity, refill_per_sec=cfg.telemetry_rate_refill)
    # Separate per-device bucket for POST /api/consent, so a telemetry flood can't starve a
    # device's ability to withdraw consent (and vice versa). Same class, own instance; tests
    # can override it on app.state exactly like the telemetry limiter.
    app.state.consent_limiter = PerDeviceRateLimiter(
        capacity=cfg.telemetry_rate_capacity, refill_per_sec=cfg.telemetry_rate_refill)

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
        # A5.1: optional /api/v1 alias (WAVR_API_V1, default OFF). Normalize the version
        # prefix to the canonical path BEFORE any auth/path check, so the alias routes to
        # the IDENTICAL handler + deps and can never become an auth-bypass shortcut (it is
        # literally the same route after this rewrite).
        if _api_v1:
            _vp = request.scope.get("path", "")
            if _vp == "/api/v1" or _vp.startswith("/api/v1/"):
                _np = "/api" + _vp[len("/api/v1"):]
                request.scope["path"] = _np
                request.scope["raw_path"] = _np.encode("utf-8")
        host = request.client.host if request.client else None
        if _is_loopback(host):                       # loopback (incl. TestClient) -> root
            # A5.1: optional same-machine local-API token. Unset => no-op. When set, even
            # the loopback root must present it (X-Wavr-Token or Bearer) on non-exempt
            # paths -> a same-box process/localhost page that can open a socket but cannot
            # read the one-time token is denied. Constant-time compare (no timing oracle).
            if _local_token and not _is_token_exempt(request.scope.get("path", "")):
                supplied = (request.headers.get("x-wavr-token")
                            or parse_bearer(request.headers.get("authorization")) or "")
                # Encode to bytes before comparing: hmac.compare_digest raises TypeError
                # on str inputs containing non-ASCII, so a hostile loopback request with a
                # non-ASCII token header would otherwise crash to 500 (crash-on-hostile-
                # input). Bytes compare is still constant-time and fails CLOSED -> 401.
                if not hmac.compare_digest(supplied.encode("utf-8"), _local_token.encode("utf-8")):
                    return JSONResponse({"detail": "local token required"}, status_code=401)
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
        # "/measure.html" is the F2 phone-capture shell: an unpaired LAN phone must be
        # able to LOAD it, but PUT /api/house/room still needs a central token.
        _p = request.url.path
        if _p in ("/", "/index.html", "/measure.html", "/manifest.webmanifest", "/sw.js", "/icon.svg") or _p.startswith("/vendor/"):
            if in_subnet(host, _local_ip):
                request.state.role = None
                return await call_next(request)
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        token = parse_bearer(request.headers.get("authorization"))
        role = authorize(host, _local_ip, token, _devices)
        if role is None:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        request.state.role = role
        # Confinement (blueprint step 3): a write-only 'sensor' token is boxed to
        # POST /api/telemetry only. This is the SINGLE choke point -- reads are open to
        # any authed role today, so without this a sensor token would read the whole
        # house. Only reachable via the token branch, so 'root'/None (loopback + static
        # shell) can never be a 'sensor'; user/central are untouched.
        if role == "sensor" and request.url.path not in _SENSOR_ALLOWED_PATHS:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
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

    def require_root(request: Request):
        # A5.2 (red-team mitigation #2 -- "the single most important add"): the ARP-block
        # route is an inward LAN-attack primitive, so it is loopback-ROOT ONLY. Even an
        # authenticated multidevice 'central' peer -- who can change other state -- must
        # NOT wield it: a paired/stolen central token would otherwise bypass the
        # X-Wavr-Local CSRF header (require_local lets 'central' through header-less),
        # the F-C bypass. Reject any non-root role. On the default (non-multidevice)
        # build every request is already 'root', so this is a no-op there.
        if getattr(request.state, "role", None) != "root":
            raise HTTPException(status_code=403, detail="blocking is loopback-root only")

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

    @app.put("/api/house/room")
    async def put_house_room(body: dict = Body(...), _=Depends(require_local)):
        # F2 "medir com o celular": upsert ONE room into the existing map WITHOUT wiping
        # the hand-edited maquette. Only x/y METER coordinates arrive here -- NO camera
        # frame is ever touched or read, so ADR-0002 (frames RAM-only) stays intact.
        # require_local gates it: loopback root needs X-Wavr-Local; a LAN peer needs a
        # central-role token (a 'user' token -> 403). validate runs once via save_house_map.
        level = body.get("level")
        room = body.get("room")
        if not isinstance(level, int) or isinstance(level, bool):
            raise HTTPException(status_code=422, detail="level must be an integer")
        if (not isinstance(room, dict) or not isinstance(room.get("name"), str)
                or not isinstance(room.get("polygon"), list)):
            raise HTTPException(status_code=422, detail="room must be {name: str, polygon: list}")
        merged = upsert_room(_house, level, room)   # deep-copies _house; no mutation yet
        try:
            save_house_map(cfg.house_map, merged)   # full validate + atomic persist
        except HouseMapError as exc:
            # empty-path -> 409 (server misconfig); invalid geometry/doc -> 422.
            code = 409 if "no house_map path" in str(exc) else 422
            raise HTTPException(status_code=code, detail=str(exc))
        _house.clear()
        _house.update(merged)       # keep the in-memory map (GET, room_names) in sync
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

    @app.post("/api/wol")
    async def wake_on_lan(mac: str = Body(..., embed=True),
                          broadcast: str = Body("255.255.255.255", embed=True),
                          port: int = Body(9, embed=True),
                          _=Depends(require_local)):
        # A3.1 Wake-on-LAN: a LAN-LOCAL actuator (zero external egress). Opt-in
        # (WAVR_NET_WOL, default OFF -> 503) + require_local CSRF. The MAC +
        # broadcast (LAN/private only) + port (0/7/9 only) are validated in
        # wavr.wol, so this can't become a unicast-to-internet UDP primitive.
        if not wol.wol_enabled():
            raise HTTPException(status_code=503,
                                detail="Wake-on-LAN disabled (set WAVR_NET_WOL=1)")
        try:
            return wol.wake(mac, broadcast=broadcast, port=port, send=wol_send)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/diag/{kind}")
    async def diag(kind: str, host: str = Body("", embed=True),
                   count: int = Body(3, embed=True),
                   resolvers: list[str] | None = Body(None, embed=True),
                   _=Depends(require_local)):
        # A3.2 diagnostics: ping / traceroute / dns. LAN/local family, opt-in
        # (WAVR_NET_DIAGNOSTICS, default OFF -> 503) + require_local CSRF. NO
        # command injection: the target is regex-validated (rejecting every shell
        # metacharacter) and traceroute is invoked with an argv LIST (shell=False)
        # in wavr.diagnostics. Transports are injectable for tests.
        if not diagnostics.diagnostics_enabled():
            raise HTTPException(status_code=503,
                                detail="diagnostics disabled (set WAVR_NET_DIAGNOSTICS=1)")
        try:
            if kind == "ping":
                return await diagnostics.ping(host, count=count, probe=ping_probe)
            if kind == "traceroute":
                return await diagnostics.traceroute(host, runner=traceroute_runner)
            if kind == "dns":
                return await diagnostics.dnsbench(
                    name=host or "example.com", resolvers=resolvers, query_fn=dns_query_fn)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(status_code=404, detail=f"unknown diagnostic: {kind}")

    @app.post("/api/speedtest")
    async def run_speedtest(confirm: bool = Body(False, embed=True),
                            _=Depends(require_local)):
        # A3.3 speed test: THE single sanctioned external egress -- treated like
        # the narrator, with one extra gate because the M-Lab/ndt7 provider
        # PUBLISHES the caller's public IP. THREE gates: (1) WAVR_NET_SPEEDTEST
        # opt-in (503 when off); (2) the IP-publishing ndt7 path is only reachable
        # when WAVR_SPEEDTEST_PROVIDER=ndt7 (default cloudflare) -- decided by
        # config, never by request body, so the single flag can't publish the IP;
        # (3) per-invocation confirm=true (409 without it). The response DISCLOSES
        # exactly what leaves the box (speedtest.describe). Never called by any
        # background task.
        if not speedtest_mod.speedtest_enabled():
            raise HTTPException(status_code=503,
                                detail="speed test disabled (set WAVR_NET_SPEEDTEST=1)")
        if confirm is not True:
            raise HTTPException(
                status_code=409,
                detail=("speed test requires explicit confirm=true -- it contacts an "
                        "external server; see the disclosure before confirming"))
        provider = speedtest_mod.speedtest_provider()
        runner = speedtest_fn or speedtest_mod.run_speedtest
        try:
            result = await asyncio.to_thread(runner, provider)
        except Exception:
            logging.exception("speedtest failed")
            raise HTTPException(status_code=502, detail="speed test backend error")
        result["disclosure"] = speedtest_mod.describe(result.get("provider", provider))
        return result

    @app.get("/api/speedtest/info")
    async def speedtest_info():
        # A3.3 PRE-egress disclosure source (audit fix). Side-effect-free, ZERO
        # egress, no secrets: it makes NO external call, it only reports the
        # configured provider + its egress disclosure so the frontend consent
        # modal can render the EXACT provider-specific M-Lab public-IP-publication
        # warning BEFORE the user sends confirm=true. Without this the disclosure
        # was only knowable AFTER the egress (attached to the POST response), which
        # broke disclose-before-confirm. `publishes_ip` is true only for the
        # ndt7/M-Lab path. Gated by the same loopback_or_authed middleware as
        # /api/status (read-only, so no require_local/confirm needed).
        provider = speedtest_mod.speedtest_provider()
        return {
            "enabled": speedtest_mod.speedtest_enabled(),
            "provider": provider,
            "publishes_ip": provider == "ndt7",
            "disclosure": speedtest_mod.describe(provider),
        }

    @app.post("/api/block")
    async def block_device(mac: str = Body(..., embed=True),
                           action: str = Body("block", embed=True),
                           confirm: bool = Body(False, embed=True),
                           _=Depends(require_local), __=Depends(require_root)):
        # A5.2 ARP device blocking -- the roadmap's SINGLE active-LAN-attack primitive,
        # pointed at the owner's OWN network. TRIPLE GATE: (1) WAVR_NET_BLOCKING default
        # OFF -> 503; (2) require_local CSRF; (3) per-invocation confirm=true -> 409
        # without it. Target denylist + gateway hard-deny + inventory-only live in
        # wavr.arp_block. NEVER default-on, NEVER agent/MCP-reachable. Honest 503 when
        # the elevated ARP-send transport is unavailable (never a silent no-op).
        if not arp_block.blocking_enabled():
            raise HTTPException(status_code=503,
                                detail="device blocking disabled (set WAVR_NET_BLOCKING=1)")
        if not _blocker.available():
            raise HTTPException(
                status_code=503,
                detail=("device blocking needs elevated raw-socket/npcap privileges that "
                        "are not available -- refusing rather than faking a block"))
        if action not in ("block", "unblock"):
            raise HTTPException(status_code=400, detail="action must be 'block' or 'unblock'")
        # confirm is required ONLY for the destructive 'block'. The corrective 'unblock'
        # (which only ever REMOVES an active block and sends a healing ARP) must always
        # be runnable without ceremony so an operator can halt a live block immediately;
        # gating the undo identically would weaken the 'full reversibility' invariant.
        if action == "block" and confirm is not True:
            raise HTTPException(
                status_code=409,
                detail=("device blocking requires explicit confirm=true -- it ACTIVELY "
                        "cuts a device off your LAN via ARP spoofing; own network only"))
        inv = _inventory.latest_inventory()
        gw = next((d for d in inv if getattr(d, "is_gateway", False)), None)
        # Independent, flag-free gateway derivation ('.1' heuristic from THIS host's LAN
        # IP; zero egress) folded into the gateway deny-set so the catastrophic
        # gateway-block guard doesn't rest solely on the best-effort is_gateway flag.
        gw_ip_indep = guess_gateway()
        try:
            if action == "block":
                return await _blocker.block(mac, inventory=inv, gateway=gw,
                                            local_ip=_block_local_ip, gateway_ip=gw_ip_indep)
            return await _blocker.unblock(mac, inventory=inv, gateway=gw,
                                          local_ip=_block_local_ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/block")
    async def list_blocks(_=Depends(require_local), __=Depends(require_root)):
        # Read-only audit view: active blocks + recent block/unblock events (topology
        # only, no PII). require_local + require_root -- active-attack state is sensitive
        # and, like the block action itself, is loopback-root only (never a LAN peer).
        return {"blocks": _blocker.list_blocks(), "events": _blocker.recent_events()}

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
                # Standalone tools (A3) -- opt-in, default OFF. `wol` +
                # `diagnostics` are LAN/local; `speedtest` is the ONE sanctioned
                # external egress (double-gated + per-invocation confirm). The
                # configured provider + its egress disclosure are returned in the
                # POST /api/speedtest response itself (features stays bool-only).
                "wol": cfg.net_wol,
                "diagnostics": cfg.net_diagnostics,
                "speedtest": cfg.net_speedtest,
                # ONVIF camera probe (A4.2) -- opt-in, default OFF. Active WS-
                # Discovery + unicast SOAP that pre-fills a camera's RTSP URL for
                # the rung-2 add form; never auto-adds. Surfaced so the Privacy &
                # Egress view stays honest that an active LAN probe is available.
                "onvif_probe": cfg.net_onvif_probe,
                # ONVIF PTZ actuator (A4.3) -- opt-in, default OFF. The first camera
                # ACTUATOR: gates /api/ptz/* (move/stop/presets/goto). Surfaced so the
                # Privacy & Egress view stays honest that a camera-control path exists.
                "ptz": cfg.ptz,
                # A5.2 ARP device blocking (WAVR_NET_BLOCKING) -- opt-in, default OFF.
                # The single active-LAN-attack primitive; surfaced so the Privacy &
                # Egress view stays honest that a device-blocking path can exist.
                "blocking": cfg.net_blocking,
                # A5.1 hardening posture, surfaced honestly (bool-only, never the
                # secret). `api_token`: a same-machine shared secret (WAVR_LOCAL_TOKEN)
                # is REQUIRED on /api/* even on loopback. `health_gate`: F6 -- the
                # side-effecting GET /api/health now requires the X-Wavr-Local CSRF
                # header, so a drive-by tab can't fire its public-DNS egress (always on).
                "api_token": bool(_local_token),
                "health_gate": True,
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
    async def health(_=Depends(require_local)):
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
        mac: str | None = Body(None),
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
        # F3: optional MAC for IP-drift detection. A supplied MAC is validated +
        # normalized (reject junk so it can never be persisted then reflected via
        # /api/cameras/suggestions); if omitted, best-effort resolve it from the
        # running inventory (null when net_inventory is off / no match -- never guessed).
        clean_mac: str | None
        if mac is not None and str(mac).strip():
            try:
                clean_mac = normalize_mac(mac)
            except ValueError:
                raise HTTPException(status_code=400, detail="mac must be a 6-octet MAC address")
        else:
            clean_mac = _resolve_mac_for_url(rtsp_url)
        if name in {s["name"] for s in manager.status()["sources"]}:
            raise HTTPException(status_code=409, detail=f"source name in use: {name}")
        try:
            _cameras.add(name, room, rtsp_url, confidence, mac=clean_mac)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"camera exists: {name}")
        manager.register(name, _camera_factory(_cameras.get(name), cfg, _camera_health.report), False)  # boots OFF
        return _masked_cameras()

    @app.post("/api/onvif/probe")
    async def onvif_probe(targets: list[str] | None = Body(None, embed=True),
                          username: str | None = Body(None, embed=True),
                          password: str | None = Body(None, embed=True),
                          timeout: float = Body(3.0, embed=True),
                          _=Depends(require_local)):
        # A4.2 ONVIF camera probe: auto-discovers LAN cameras (WS-Discovery) and
        # fetches their RTSP URI (GetProfiles/GetStreamUri) to PRE-FILL the rung-2
        # add form. It NEVER auto-adds a camera -- the user still confirms via
        # POST /api/cameras (which keeps the rtsp-scheme guard) and cameras boot OFF.
        # Opt-in (WAVR_ONVIF_PROBE, default OFF -> 503) + require_local CSRF. SSRF-
        # hard: wavr.sources.onvif validates BOTH the device-service XAddrs host and
        # the returned rtsp host to a LAN-IP literal before any connection / before
        # surfacing (public/DNS/cloud-metadata refused, redirects blocked, XXE
        # rejected). Camera creds are request-scoped only: used to build the WS-
        # UsernameToken digest and NEVER persisted/logged/echoed; the response rtsp
        # URLs are masked. Clamp the per-call timeout so a request can't hang.
        if not cfg.net_onvif_probe:
            raise HTTPException(status_code=503,
                                detail="ONVIF probe disabled (set WAVR_ONVIF_PROBE=1)")
        probe = ONVIFProbe(discover=onvif_discover, soap=onvif_soap)
        clamped = max(0.5, min(float(timeout), 10.0))
        result = await probe.probe(targets=targets, username=username,
                                   password=password, timeout=clamped)
        # Defence in depth: never let creds ride back out even if a transport bug
        # tried to. The result dicts are built creds-free by design (masked rtsp);
        # this strips any stray top-level echo without touching the camera list.
        result.pop("username", None)
        result.pop("password", None)
        return result

    @app.delete("/api/cameras/{name}")
    async def delete_camera(name: str, _=Depends(require_local)):
        if not _cameras.delete(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            await manager.unregister(name)
        except KeyError:
            pass   # not registered (e.g. removed before a restart re-registered it)
        _camera_health.clear(name)   # drop any stale drift suggestion for the removed cam
        return _masked_cameras()

    @app.get("/api/cameras/suggestions")
    async def camera_suggestions():
        # F3 read-only IP-drift suggestions (loopback middleware is the gate, like
        # GET /api/inventory -- no CSRF). Each: {camera, mac, current_ip, suggested_ip,
        # vendor, ts}. IP+MAC+vendor only (already non-sensitive per /api/inventory);
        # the rtsp_url (creds) is NEVER included. Empty when there is no drift, no
        # inventory, or no stored MAC. NOT authoritative -- a MAC-spoofing LAN attacker
        # can manufacture one, so the UI must require explicit confirmation before /rebind.
        return {"suggestions": _camera_health.suggestions()}

    @app.post("/api/cameras/{name}/rebind")
    async def rebind_camera(name: str, ip: str = Body(..., embed=True),
                            _=Depends(require_local)):
        # F3 one-click IP-drift rebind. A rebind is NEVER automatic -- this is the
        # load-bearing mitigation: a MAC-spoofing LAN attacker can manufacture a drift
        # suggestion, so the change is applied ONLY on the user's explicit confirmation.
        # Confirming will send the camera's STORED credentials to `ip` on next enable.
        # State-changing -> require_local (CSRF).
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        ip = (ip or "").strip()
        # SSRF-hard: private LAN IPv4 literal ONLY (mirrors the ONVIF guard). Rejects
        # public IPs, DNS hostnames, cloud-metadata (169.254.169.254) and mapped forms.
        if not _rebind_ip_ok(ip):
            raise HTTPException(status_code=400, detail="ip must be a private LAN IPv4 literal")
        cam = _cameras.get(name)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        new_url = rebind_rtsp_host(cam["rtsp_url"], ip)
        # rebind_rtsp_host returns the ORIGINAL on an odd shape; re-check the rtsp scheme
        # on the rewritten URL and refuse (500-safe) rather than persist something
        # unusable. NEVER log/echo the raw url (carries credentials).
        if new_url == cam["rtsp_url"] or not _URL_SHAPE_RE.match(new_url):
            raise HTTPException(status_code=500, detail="could not rewrite camera address")
        _cameras.set_url(name, new_url)
        with suppress(KeyError):
            await manager.unregister(name)   # mirror delete_camera: kill before re-register
        # Re-register boot-OFF (ADR-0002: a rebind never auto-enables a camera).
        manager.register(name, _camera_factory(_cameras.get(name), cfg, _camera_health.report), False)
        _camera_health.clear(name)
        return _masked_cameras()

    # ------------------------------------------------------------------- #
    # ONVIF PTZ actuator routes (A4.3) -- opt-in (WAVR_PTZ) + require_local +
    # master camera kill-switch. Creds come ONLY from the stored rtsp_url and
    # NEVER appear in a request/response/log. No frame is ever read.
    # ------------------------------------------------------------------- #
    def _ptz_cam(camera_id: str) -> dict:
        # Flag gate FIRST (default OFF -> 503 before any store lookup / ONVIF call).
        if not cfg.ptz:
            raise HTTPException(status_code=503, detail="PTZ disabled (set WAVR_PTZ=1)")
        if not _NAME_RE.match(camera_id):
            raise HTTPException(status_code=400, detail="camera id must be alphanumeric/_/-")
        cam = _cameras.get(camera_id)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {camera_id}")
        return cam   # cam["rtsp_url"] carries the creds -- NEVER echo it back

    def _camera_active(camera_id: str) -> bool:
        # Master camera kill-switch coupling: PTZ may only actuate a camera the
        # operator has explicitly turned ON (source task running). System kill or a
        # per-source disable both flip `active` False -> every move short-circuits.
        return any(s["name"] == camera_id and s["active"]
                   for s in manager.status()["sources"])

    @app.post("/api/ptz/{camera_id}/move")
    async def ptz_move(camera_id: str,
                       pan: float = Body(0.0), tilt: float = Body(0.0),
                       zoom: float = Body(0.0), _=Depends(require_local)):
        cam = _ptz_cam(camera_id)
        if not _camera_active(camera_id):
            # Camera off -> no ONVIF call at all (kill-switch dominates PTZ).
            return {"ok": False, "reason": "camera off"}
        ok = await _ptz.continuous_move(camera_id, cam["rtsp_url"], pan, tilt, zoom)
        return {"ok": ok}

    @app.post("/api/ptz/{camera_id}/stop")
    async def ptz_stop(camera_id: str, _=Depends(require_local)):
        cam = _ptz_cam(camera_id)
        # Stop is always allowed (safety): even a just-disabled camera should halt.
        return {"ok": await _ptz.stop(camera_id, cam["rtsp_url"])}

    @app.get("/api/ptz/{camera_id}/presets")
    async def ptz_presets(camera_id: str):
        cam = _ptz_cam(camera_id)
        return await _ptz.get_presets(camera_id, cam["rtsp_url"])

    @app.post("/api/ptz/{camera_id}/preset/{token}")
    async def ptz_goto_preset(camera_id: str, token: str, _=Depends(require_local)):
        cam = _ptz_cam(camera_id)
        if not _PRESET_RE.match(token):
            raise HTTPException(status_code=400, detail="invalid preset token")
        if not _camera_active(camera_id):
            return {"ok": False, "reason": "camera off"}
        return {"ok": await _ptz.goto_preset(camera_id, cam["rtsp_url"], token)}

    @app.get("/api/ptz/{camera_id}/capabilities")
    async def ptz_capabilities(camera_id: str):
        cam = _ptz_cam(camera_id)
        return await _ptz.capabilities(camera_id, cam["rtsp_url"])

    if cfg.multidevice:
        @app.post("/api/pair-code")
        async def pair_code(role: str = Body("user", embed=True), _=Depends(require_local)):
            # Operator (loopback root / central) mints a one-time pairing code that a
            # companion then redeems at POST /api/pair. Gated by require_local.
            # 'sensor' (blueprint step 1) mints a write-only phone-telemetry code.
            if role not in ("central", "user", "sensor"):
                raise HTTPException(status_code=400, detail="role must be central, user, or sensor")
            # Out-of-band MitM defense (audit blocking #1): return the SHA-256 fingerprint
            # of the LIVE serving cert, read off this TRUSTED loopback response, so the
            # operator can verify it against the fingerprint the phone's browser shows in
            # its certificate warning BEFORE accepting. A pairing-time TLS MitM presents a
            # different self-signed cert -> different fingerprint -> the operator sees the
            # mismatch and stops. `cryptography` is not imported (pure-stdlib fingerprint).
            from wavr.tls import cert_fingerprint, resolved_cert_path
            fingerprint = cert_fingerprint(resolved_cert_path(cfg.tls_cert))
            return {"code": _pairing.mint_code(role), "cert_fingerprint": fingerprint}

        @app.post("/api/telemetry")
        async def telemetry(payload: TelemetryPayload, request: Request):
            # Phone sensor telemetry (blueprint step 2). Bearer-authed by the
            # loopback_or_authed middleware (any authed device role, incl. 'sensor');
            # NO require_local/CSRF -- a token-authed LAN device is header-independent,
            # the same rule every token route follows. A malformed body fails the
            # TelemetryPayload model BEFORE this coroutine runs -> 422, never 500.
            #
            # IDENTITY: the reading is keyed to the CALLER'S OWN device_id, re-derived
            # from the presented token here (same pattern as /api/ws-ticket), NEVER from
            # payload.device -- so a phone can never attribute telemetry to another
            # device. No house-state write: the normalized reading is enqueued for
            # PhoneSensorSource (step 4) to fold into fusion.
            token = parse_bearer(request.headers.get("authorization"))
            device = _devices.verify(token) if (token and _devices is not None) else None
            if device is None:
                # Loopback root (no token) / any tokenless caller: telemetry is a paired-
                # device feature, there is no own-device to key the reading to.
                raise HTTPException(status_code=401, detail="device token required")
            if not request.app.state.telemetry_limiter.allow(device.device_id):
                raise HTTPException(status_code=429, detail="telemetry rate limit exceeded")
            # CONSENT TIER gate (privacy centerpiece), read off the row verify() already
            # fetched (no extra query). Purely SUBTRACTIVE -- it only ever DROPS or REDUCES,
            # never raises trust. Rate-limit stays BEFORE this so a red flood still 429s.
            #   red    -> drop server-side; the reading NEVER reaches the hub. Return HTTP 200
            #             {accepted:False}: a 4xx would trip the on-device shim's token-wipe
            #             and force an un-pair, which is NOT what a privacy withdrawal means.
            #   yellow -> ingest a data-minimized copy (reduced(): rssi/ssid/bssid=None,
            #             sensors={}) -> anonymous presence vote, no location/signal leak.
            #   green  -> ingest the full reading, as today.
            if device.consent == "red":
                return {"accepted": False, "device_id": device.device_id, "consent": "red"}
            reading = TelemetryReading.from_payload(payload, device.device_id)
            if device.consent == "yellow":
                reading = reading.reduced()
            request.app.state.telemetry_hub.offer(reading)
            return {"accepted": True, "device_id": device.device_id}

        @app.post("/api/consent")
        async def consent(body: _ConsentBody, request: Request):
            # Self-service consent withdrawal/restore (GDPR). Bearer-authed by the same
            # loopback_or_authed middleware as /api/telemetry; NO require_local/CSRF -- a
            # token-authed device is header-independent. The device_id is derived from the
            # CALLER'S OWN token here (verify), NEVER from the body -- a device sets only its
            # OWN tier, so the body carries no device field (extra='forbid' rejects one).
            token = parse_bearer(request.headers.get("authorization"))
            device = _devices.verify(token) if (token and _devices is not None) else None
            if device is None:
                raise HTTPException(status_code=401, detail="device token required")
            if not request.app.state.consent_limiter.allow(device.device_id):
                raise HTTPException(status_code=429, detail="consent rate limit exceeded")
            if body.level not in VALID_CONSENT:
                raise HTTPException(status_code=422, detail="invalid consent level")
            _devices.set_consent(device.device_id, body.level)
            return {"device_id": device.device_id, "level": body.level}

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
            # Reject unknown/revoked AND 'sensor' devices. Confinement defence-in-depth
            # (blueprint step 3): the http choke point already 403s a sensor at
            # POST /api/ws-ticket so it can never MINT a ticket -- this closes the WS
            # handshake directly too, mirroring the M1 revoke re-check, so /ws/live is
            # sensor-proof even if a ticket somehow existed. user/central unaffected.
            if dev is None or dev.revoked or dev.role == "sensor":
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

    # F2 phone-capture shell (WebXR "medir com o celular"). Static, carries nothing
    # sensitive -- like "/" it is token/subnet-exempt so an unpaired LAN phone can load
    # it; the data endpoint (PUT /api/house/room) still requires a central-role token.
    @app.get("/measure.html")
    async def measure_page():
        return FileResponse(_FRONTEND / "measure.html", media_type="text/html")

    return app


app = create_app()
