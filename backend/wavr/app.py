from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import hmac
import io
import ipaddress
import logging
import os
import re
import sqlite3
import tarfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr import __version__
from wavr.config import load_config
from wavr.housemap import load_house_map, room_names, room_polygon, save_house_map, upsert_room, HouseMapError
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine, house_person_count
from wavr.sourcemanager import SourceManager
from wavr.sources.simulated import SimulatedSource
from wavr.sources.network import NetworkSource, _local_ipv4
from wavr.sources.ruview import RuViewSource
from wavr.sources.camera import CameraSource, yolo_pose_detect
from wavr.sources.mmwave import MmWaveSource
from wavr.camera_store import CameraStore
from wavr.calib_store import CalibrationStore, validate_mount, CalibrationError
from wavr.localize import make_localizer, floor_spots_for_room
from wavr.calib_sample import CalibSampleStore
from wavr.calib_refine import solve_progressive
from wavr.calib_session import CalibSessionStore, CalibSessionError, SessionState
from wavr.camera_health import CameraHealthMonitor
from wavr.camera_url import rebind_rtsp_host, rtsp_host
from wavr.camera_privacy import PrivacyControlNotImplemented, set_privacy_mode
from wavr.netaddr import is_lan_ip
from wavr.rules import RulesEngine
from wavr.away import AwayMonitor
from wavr.mqtt_publisher import make_publisher
from wavr.notifier import make_notifier
from wavr.narrator import Narrator, make_generate, provider_configured
from wavr.netinventory_service import NetworkInventoryService
from wavr.api_inventory import build_inventory_router, inventory_view, merge_alerts
from wavr.house_status import compose_house_status, DEFAULT_NETWORK_WINDOW_MINUTES
from wavr.watch import (WatchMode, IntrusionAlertLog, known_present_persons,
                        project_state, room_unrecognized, house_unrecognized)
from wavr.fall_detect import FallDetector, lying_outside_zone
from wavr.device_meta import DeviceMeta, normalize_mac, sanitize_name
from wavr.occupancy_log import OccupancyLog
from wavr.known_store import KnownStore
from wavr.netinventory import _same_ip
from wavr.ha_client import client_from_config
from wavr.ha_import import fetch_registry, import_devices
from wavr.ha_import_store import HAImportStore
from wavr.internet_monitor import InternetMonitor, guess_gateway, make_checker
from wavr.dhcp_monitor import RogueDhcpMonitor, make_collector as make_dhcp_collector
from wavr.gateway_monitor import GatewayIdentityMonitor, GatewayBindingStore
from wavr.health_check import check_health, default_resolver_checkers, default_extra_checkers
from wavr.net_doctor import diagnose, apply_fixes, DoctorLog
from wavr.presence_report import build_report
from wavr import wol, diagnostics, speedtest as speedtest_mod
from wavr.sources.onvif import ONVIFProbe
from wavr.ptz import CameraPTZ
from wavr.sources.ble import BLESource
from wavr.identity_store import ROOT_DEVICE_ID, IdentityStore
from wavr.routines import ActionExecutor, RoutineStore, RoutinesEngine
from wavr.person_presence import PersonPresence, RoomPresence
from wavr.connector_store import ConnectorStore
from wavr.api_connectors import build_connectors_router
from wavr.connectors.notify.telegram import make_telegram_send
from wavr.connectors.notify.digest import compose_digest, send_digest
from wavr.assistant_store import AssistantEngineStore
from wavr.api_assistant import build_assistant_router
from wavr.bonded import read_bonded
from wavr.api_identity import build_identity_router
from wavr.api_routines import build_routines_router
from wavr.devices import DeviceStore, VALID_CONSENT
from wavr.pairing import PairingManager
from wavr.pair_requests import PairApprovalManager
from wavr.auth import access_for_scoped, parse_bearer, can_change_state, can_view, in_subnet, has_scope, effective_scopes
from wavr.api_devices import build_pair_router, build_ws_ticket_router, build_devices_router
from wavr.api_pair_requests import build_pair_request_router, build_pending_pairings_router
from wavr.peers import PeerStore
from wavr.api_peers import build_peers_public_router, build_peers_admin_router
from wavr.nodes import NodeStore, NodeEnroller
from wavr.api_nodes import (
    build_nodes_public_router, build_nodes_ingest_router, build_nodes_admin_router,
)
from wavr.local_token import resolve_local_token
from wavr import arp_block
from wavr.companion_presence import resolve_source_mac, mac_prefix
from wavr.pin_store import PinStore
from wavr.pin_ratelimit import PinAttemptLimiter


_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
_VENDOR_DIR = _INDEX.parent / "vendor"
_CATALOG_PATH = _VENDOR_DIR / "device-catalog.json"

# PERF: GET /api/house-status's own routine/is_unusual sweep (see _compute_house_status)
# re-runs occupancy_log.is_unusual() -- up to a 5000-row sqlite scan PER currently-fused
# room -- on every call. The dashboard polls this route every ~20s, so amortize just
# that sweep over a short TTL. Deliberately NOT applied to intrusion/network/fall
# reasons -- those must self-clear/appear the instant they resolve/fire (see
# wavr.house_status's own "RECENCY, honestly" docstring), only the routine-anomaly
# sweep (a soft, non-security signal) is cached.
_HOUSE_STATUS_ROUTINE_TTL_S = 5.0

# Wall-clock cadence at which an open /ws/live stream re-checks the companion's
# revoked flag (see the live() loop). Bounds revocation latency: a revoked device's
# stream drops within this window regardless of frame cadence -- even on a silent
# hub, where the old frame-COUNT throttle (n % 50) never fired because the loop was
# parked on `await q.get()`. Small enough to feel prompt, large enough that the
# per-tick DeviceStore read is negligible for a handful of paired companions.
_WS_REVOKE_RECHECK_S = 2.0


async def _stream_live(ws, q, did, get_device, recheck_s):
    """Pump hub frames to an accepted /ws/live socket, severing the stream within
    `recheck_s` of the companion being revoked. The revoked flag is re-read on a WALL-CLOCK
    cadence -- `q.get()` is bounded by a timeout -- so a SILENT hub (no frames at all) still
    drops a revoked device; the old frame-COUNT throttle parked forever on `await q.get()`
    and never rechecked. `did` is None for loopback root: no revoke check, streams until the
    socket closes. `get_device(did)` -> Device|None (None, or a truthy `.revoked`, ends it).
    A WebSocketDisconnect from send_json propagates to the caller, which owns unsubscribe."""
    last_check = 0.0
    while True:
        try:
            await ws.send_json(await asyncio.wait_for(q.get(), recheck_s))
        except asyncio.TimeoutError:
            pass   # no frame this interval -- fall through to the revoke re-check
        if did is not None:
            now = asyncio.get_running_loop().time()
            if now - last_check >= recheck_s:
                last_check = now
                dev = get_device(did)
                if dev is None or dev.revoked:
                    return

# 2C: cadence of the opt-in daily-digest scheduler task (see _digest_loop below) --
# one composition+send pass every 24h. Gated on the SEPARATE "digest" connector row
# (default-OFF), so this constant only controls the wakeup cadence, never whether
# anything is actually sent. A test drives one deterministic tick via the
# app.state.digest_once seam instead of waiting on this interval.
_DIGEST_INTERVAL_S = 24 * 3600.0
# How often the routines loop evaluates time/deadline triggers (schedule,
# house_away_by_time). 30s is fine-grained enough for an "at 23:00" routine while
# the matching stays pure (only a fired routine touches a sink). Test seam:
# app.state.routines_tick fires one pass without waiting on this interval.
_ROUTINES_INTERVAL_S = 30.0


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


# OTA (Augusto sign-off): the pinned/hashed/web-only/next-launch update channel a
# paired companion polls (GET /api/app/manifest + GET /api/app/bundle below).
# Deliberately the SAME static-shell file set the "/" "/index.html"
# "/manifest.webmanifest" "/sw.js" "/icon.svg" "/measure.html" routes already
# serve individually -- never /vendor (the large, frozen three.js payload --
# re-bundling it would bloat every update for content that never changes) and,
# non-negotiably, never the mobile shim/lib/native code that HOLDS the pin: an
# OTA channel may ship a new dashboard but must never be able to rewrite its
# own verifier. Version is `__version__` (the same version string /api/status
# and /api/companion/health already disclose) -- monotonic by the package's own
# release discipline, not a separate counter this feature invents.
_OTA_ASSET_NAMES = ("index.html", "manifest.webmanifest", "sw.js", "icon.svg", "measure.html")


@functools.lru_cache(maxsize=1)
def _build_ota_bundle() -> dict:
    """Gzip-tar the OTA-eligible web assets + hash/size the result. Cached
    (process-lifetime -- the frontend shell doesn't change while a server is
    running) so GET /api/app/manifest and GET /api/app/bundle always agree
    byte-for-byte without re-reading disk on every call."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _OTA_ASSET_NAMES:
            path = _INDEX.parent / name
            if path.exists():
                tar.add(path, arcname=name)
    data = buf.getvalue()
    return {"data": data, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


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


def _default_sources(cfg, ble_provider=None, net_provider=None, net_detail_provider=None):
    """Plano A real-source set: network always-on ($0), ruview always-on (harmless
    reconnect loop when the container is absent), sim off by default (toggle it on
    from the dashboard to populate the view when no real data is flowing). mmwave is
    only added when a serial port is configured (passive local serial, no frames
    otherwise) — but then it's always-on, same as network/ruview.

    `ble_provider`/`net_provider` are the LIVE consent-registry providers (callables
    returning the current {addr: person} map, env allowlist merged with the identity
    store). When present the sources re-read them each scan cycle so a registration /
    opt-out takes effect with no restart. The BLE source is registered at boot only
    when there is at least one known device (env OR registry) — a truly-empty install
    stays byte-identical; a first BLE registration on such an install brings the
    source up live via the POST route's ensure_source hook."""
    sources = [
        ("network", lambda: NetworkSource(
            cfg.net_known_macs, interval=cfg.net_interval, grace=cfg.net_grace,
            known=cfg.net_known, emit_identity=cfg.identity_enabled,
            known_provider=net_provider, detail_provider=net_detail_provider), True),
        ("ruview", lambda: RuViewSource(
            cfg.ruview_url, room=cfg.ruview_room, reconnect_delay=cfg.ruview_reconnect), True),
        ("sim", lambda: SimulatedSource(interval=cfg.sim_interval), False),
    ]
    if cfg.mmwave_port:
        sources.append(
            ("mmwave", lambda: MmWaveSource(cfg.mmwave_room, cfg.mmwave_port), True))
    _ble_known_now = ble_provider() if ble_provider is not None else cfg.ble_known
    if _ble_known_now:
        sources.append(("ble", lambda: BLESource(
            cfg.ble_known, room=cfg.ble_room, rssi_min=cfg.ble_rssi_min,
            interval=cfg.ble_interval, emit_identity=cfg.identity_enabled,
            known_provider=ble_provider), True))
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
# Core Panel unlock PIN: digits only, a reasonable length band (rejects a
# oversized string reaching pbkdf2, and an empty/1-digit PIN that offers no
# real protection).
_PIN_RE = re.compile(r"^[0-9]{4,12}$")


def _mask_rtsp(url: str) -> str:
    """Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rtsp://user:***@host.
    Never raises: any unexpected shape (e.g. "a@b://c") is returned unchanged rather than crashing a GET/POST."""
    try:
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        # rpartition = the LAST "@", which is the userinfo/host boundary per RFC 3986 (and
        # what ffmpeg does). Splitting on the FIRST "@" leaked the password TAIL verbatim
        # whenever the password itself contained an "@" (a common thing in camera creds):
        # rtsp://user:p@ss@cam/stream masked to rtsp://user:***@ss@cam/stream -- "ss" shipped
        # to every paired user via GET /api/cameras. camera_url.py already parses the same
        # URL with rpartition; this is now consistent with it.
        creds, at, host = rest.rpartition("@")
        if not at:
            # No "@" in the AUTHORITY (the "@" was in the scheme, e.g. "a@b://c"). rpartition
            # never raises, so unlike the old split-unpack there is no ValueError to fall into
            # the except below -- return unchanged explicitly, as the docstring promises.
            return url
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


# Walk-to-calibrate feet-pixel extraction reuses the SAME pose pass as normal operation
# (no 2nd model -- still yolo_pose_detect / _pose_model) but needs its OWN confidence
# floor, independent of the camera's day-to-day `confidence` setting. That setting is a
# per-camera product-exposed tunable an operator may deliberately lower (see
# computer-vision-engineer domain notes) to catch partially-occluded people during normal
# operation -- fine there, because a weak Target still only feeds a discounted signal into
# fusion. Left unmodified during a calibration walk, that same low floor would let a
# marginal/noisy feet pixel become a homography CORRESPONDENCE POINT, permanently
# distorting every future localization from that camera. A walk-to-calibrate session is a
# few seconds with the operator standing in full view on purpose, so raising the floor
# costs nothing in practice -- GET calib-sample just reads `person: false` until a
# confident detection lands, exactly like "no person yet". No frame is read or kept either
# way (ADR-0002); this only changes which detections are trusted enough to sample.
_CALIB_SAMPLE_MIN_CONFIDENCE = 0.5


def _camera_factory(cam: dict, cfg, on_health=None, calib=None, house=None,
                    sample_store=None, sampling=False, on_privacy=None):
    # F3: pass the camera name + the health monitor's report callback + the unhealthy
    # threshold so a drifted/dead camera is edge-reported (name+bool only, never a
    # frame -- ADR-0002). `on_health` is None for callers that don't wire the monitor.
    #
    # Spec A localization: when the camera has a stored calibration (a 4-point
    # homography OR a mount prior) AND its room polygon is known, build a localizer and
    # turn the pose pass ON so the source emits POSITIONED Target(x, y). Without a
    # calibration the camera keeps its old behaviour exactly (pose off, room-centred) --
    # so a freshly added, uncalibrated camera never pays the pose-inference cost and is
    # byte-identical to before. The localizer works on the feet PIXEL + stored matrices
    # only; no frame is ever read or persisted (ADR-0002).
    pose = False
    pose_detect = None
    loc = None
    if calib is not None and house is not None:
        try:
            c = calib.get(cam["name"])
        except Exception:
            c = None
        if c and (c.get("homography") or c.get("mount")):
            poly = room_polygon(house, cam["room"], level=cam.get("level"))
            if poly:
                calib_img_size = None
                if c.get("img_w") is not None and c.get("img_h") is not None:
                    calib_img_size = (c.get("img_w"), c.get("img_h"))
                loc = make_localizer(poly, homography=c.get("homography"),
                                     mount=c.get("mount"),
                                     calib_img_size=calib_img_size,
                                     homography_quality=c.get("quality"))
    # Walk-to-calibrate SAMPLING: when a calibration session is active, run the pose
    # pass to capture the walker's raw FEET PIXEL into the sample store so GET
    # calib-sample can pair it with the known floor spot. Turns pose ON even with NO
    # calibration (the raw pixel is all the wizard needs), and coexists with a localizer
    # if one already exists. The closure hands the store ONLY a coordinate + image dims
    # + confidence -- never a frame (ADR-0002).
    on_feet = None
    if sampling and sample_store is not None:
        _nm = cam["name"]

        def on_feet(feet_px, img_size, conf, _nm=_nm, _sink=sample_store):
            _sink.record(_nm, feet_px, img_size[0], img_size[1], conf)

    if loc is not None or on_feet is not None:
        pose = True
        kwargs = {}
        if loc is not None:
            kwargs["localize"] = loc
        if on_feet is not None:
            kwargs["on_feet"] = on_feet
        pose_detect = functools.partial(yolo_pose_detect, **kwargs)
    # Sampling (an active walk-to-calibrate session) overrides the effective confidence
    # with the dedicated calibration floor -- never LOWERS it below the operator's own
    # setting, only ever raises it, so a camera already tuned strict keeps its own,
    # stricter number. See `_CALIB_SAMPLE_MIN_CONFIDENCE` for the reasoning.
    effective_confidence = (max(cam["confidence"], _CALIB_SAMPLE_MIN_CONFIDENCE)
                            if sampling else cam["confidence"])
    return lambda: CameraSource(cam["room"], cam["rtsp_url"], name=cam["name"],
                                interval=cfg.cam_interval, confidence=effective_confidence,
                                on_health=on_health,
                                unhealthy_secs=cfg.cam_unhealthy_secs,
                                pose=pose, pose_detect=pose_detect,
                                on_privacy=on_privacy)


def create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None,
               rules_publish=None, narrator=None, notify=None, device_meta=None,
               internet_monitor=None, health_check=None, dhcp_monitor=None,
               health_resolvers=None, gateway_monitor=None,
               ha_import_store=None,
               wol_send=None, ping_probe=None, traceroute_runner=None,
               dns_query_fn=None, speedtest_fn=None,
               onvif_discover=None, onvif_soap=None, ptz_soap=None, arp_send=None,
               net_inventory=None, identity_store=None, bonded_reader=None,
               connector_store=None, pin_store=None, companion_resolve_mac=None,
               known_store=None, occupancy_log=None, assistant_store=None,
               routine_store=None) -> FastAPI:
    cfg = load_config()
    # Peer pairing (Phase 1) is a strict superset of multidevice: a peer authenticates
    # as a `role=central` device, so the whole DeviceStore/PairingManager/middleware
    # stack that multidevice builds MUST be present. Fail fast rather than silently
    # mounting peer routers that reference a None _devices/_pairing.
    if cfg.peers_enabled and not cfg.multidevice:
        raise RuntimeError(
            "WAVR_PEERS_ENABLED requires WAVR_MULTIDEVICE=1 -- peer identity "
            "IS a multidevice central identity")
    # Sensor nodes (design 2026-07-11): a node is a LAN device that needs multidevice's
    # LAN bind + local TLS -- same "fail fast rather than silently mount a broken
    # surface" rule as the peers check above.
    if cfg.nodes_enabled and not cfg.multidevice:
        raise RuntimeError(
            "WAVR_NODES_ENABLED requires WAVR_MULTIDEVICE=1 -- a node is a LAN "
            "device that needs multidevice's LAN bind + local TLS")
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    # Wall-clock ageing is applied ONLY to the engine this function builds itself
    # (the real app). An injected `fusion` (tests) keeps its own now_fn=None so
    # fixed-timestamp determinism is unaffected. now_fn flips _fuse from ageing
    # against the room's newest event (age 0 -> frozen reading) to the wall clock,
    # so a source that stops reporting decays to zero instead of freezing.
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold,
                                     now_fn=lambda: datetime.now(timezone.utc))
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)
    # Watch/Guard ("Vigia") -- server-side, in-memory, DEFAULT OFF (privacy-first boot).
    # A single toggle that, while on, suppresses the family geometry/identity/vitals from
    # every egress and surfaces only counts + the intrusion room. latest stays the FULL
    # internal truth; the suppression is applied by wavr.watch.project_state at each egress.
    _watch = WatchMode()
    _intrusion = IntrusionAlertLog()  # edge-triggered "unrecognized person in <room>" alerts
    # A9 fall/no-motion suspicion (RESEARCH-GRADE, ADR-0003) -- default OFF
    # (cfg.fall_detect_enabled). None means the feature is fully inert: `_publish` below
    # never even evaluates `lying_outside_zone`, so an operator who never opted in pays
    # zero extra cost and sees it nowhere (same "None => skip" idiom as `_occupancy_log`).
    _fall = FallDetector(dwell_s=cfg.fall_dwell_s) if cfg.fall_detect_enabled else None
    # deepcopy: load_house_map returns the module-level housemap.DEFAULT_MAP object
    # itself on any fallback (missing/invalid file), and put_house below mutates _house
    # in place (clear/update). Without this copy, the first PUT on a fresh install --
    # now that WAVR_HOUSE_MAP defaults to a (usually not-yet-existing) "house.json" --
    # would corrupt DEFAULT_MAP process-wide. Copy once so _house is always private.
    _house = copy.deepcopy(load_house_map(cfg.house_map))

    # Connector registry (project_wavr_connectors_vision): the persistence for the
    # single 'Connectors & Services' egress surface. Always built (like CameraStore /
    # identity_store), inert until the admin toggles something -- an EMPTY registry is
    # byte-identical to today (no built-in suppressed, no generic active). Shares
    # wavr.db (git-ignored) so no connector metadata lands in this public repo. Built
    # HERE (moved up from its original spot near identity_store) so the notify
    # fan-out below can see the "telegram" connector's boot-time enabled state --
    # see _notify_all's docstring for why.
    _owns_connectors = connector_store is None
    _connectors = connector_store or ConnectorStore(cfg.db_path)

    # Notifier: opt-in via injected `notify` (tests) or WAVR_NTFY_URL (self-hosted
    # ntfy, stdlib POST, lazily built). Off by default -- no notifier, no HTTP calls.
    # Sends ONLY derived edge events (house arrived/left, rogue-device, fall) -- never
    # targets/vitals/frames/MACs.
    _notify = notify
    if _notify is None and cfg.ntfy_url:
        _notify = make_notifier(cfg.ntfy_url)

    # 2C notify fan-out: Telegram alongside the existing ntfy `_notify`, on its OWN
    # "telegram" connector row (default-OFF, independent of WAVR_NTFY_URL). The
    # factory itself is a bare closure (zero cost/network until called) -- always
    # built so a live Connectors-screen enable takes effect with no restart for the
    # rogue-device/fall callbacks below (they call `_notify_all` unconditionally).
    _telegram_send = make_telegram_send(_connectors)

    def _notify_all(msg: str, *, kind: str, severity: str = "alert",
                    room: str | None = None) -> None:
        """Fan a derived-only alert edge out to every opt-in sink: the existing
        ntfy `_notify` (unchanged, sync fire) AND Telegram (`telegram` connector
        row). `send()` is a blocking urllib POST (see connectors/notify/
        telegram.py's own NON-BLOCKING note), so it is offloaded via
        asyncio.to_thread rather than awaited inline -- this never stalls the
        fusion/ingest path it is called from. Each sink is independently
        opt-in: calling this unconditionally costs nothing when both are off
        (ntfy: `_notify` is None -> skipped; Telegram: the is_enabled() check
        below skips the thread dispatch entirely -- zero network attempted)."""
        if _notify:
            _notify(msg)
        if _connectors.is_enabled("telegram"):
            try:
                asyncio.create_task(asyncio.to_thread(_telegram_send, kind, severity, room, msg))
            except RuntimeError:
                pass  # no running event loop (e.g. a sync test calling the callback directly)

    # --- Routines: the user-authored "when THIS -> do THAT" spine (routines.py). The
    # engine taps the SAME arrived/left edge AwayMonitor already emits (via the on_edge
    # hook wired into _away below) plus a time/deadline tick, and runs each routine's
    # actions through the existing gated sinks. Empty store => nothing fires =>
    # byte-identical to today. Room/person/device triggers are engine-ready and wired in
    # a following step. ------------------------------------------------------------------
    _routine_store = routine_store or RoutineStore(cfg.db_path)

    def _routine_ha_call(domain: str, service: str, entity_id) -> None:
        # A routine's light/switch action goes through the EXACT gate chain the MCP
        # agent's control tool uses (control flag + entity shape + sensitive-domain
        # refusal + allowlist). A refusal is a non-raising {"ok": False}; turn it into an
        # exception so the executor counts it as a failed action. Imported lazily to keep
        # wavr.mcp out of the default import path.
        from wavr.mcp import call_ha_service
        res = call_ha_service(client_from_config(cfg), domain, service, entity_id or "",
                              control_enabled=cfg.mcp_control,
                              allowed_services=cfg.ha_allowed_services)
        if not res.get("ok"):
            raise RuntimeError(res.get("message") or "Home Assistant refused the action")

    _routine_executor = ActionExecutor(
        ha_call=_routine_ha_call,
        notify=lambda m: _notify_all(m, kind="routine", severity="note"),
        watch_set=_watch.set)
    _routines = RoutinesEngine(
        _routine_store,
        # Lazy closures: `manager` + `_routine_house` are assigned later in create_app but
        # only READ at runtime (an edge or a tick), by which point both are bound.
        sensing_on=lambda: bool(manager.status().get("running")),
        # The house CONDITION reads the dedicated always-on tracker (_routine_house),
        # NOT the optional MQTT/ntfy AwayMonitor (_away) which is None with no sink -- else
        # a routine with a {house: home/away} condition would silently never fire on a Core
        # without MQTT (it read None -> UNKNOWN -> skip). Same source the tick uses.
        house_home=lambda: _routine_house.home)

    async def _run_routines(matched: list) -> None:
        for r in matched:
            # Actions block (HA HTTP); run off the event loop so a slow HA never stalls
            # the fusion/ingest path the edge callback fires from.
            status = await asyncio.to_thread(_routine_executor.run, r["actions"])
            _routine_store.mark_fired(
                r["id"], datetime.now().isoformat(timespec="seconds"), status)

    _routine_tasks: set = set()   # strong refs to in-flight dispatches

    def _dispatch_routines(matched: list) -> None:
        if not matched:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (a sync test driving the monitor edge directly)
        # Hold a STRONG reference until the task completes: asyncio keeps only a weak ref,
        # so a bare create_task can be garbage-collected mid-flight and the routine action
        # silently never runs (bug-bank #9). The done-callback drops the ref; shutdown
        # cancels any still in flight.
        t = loop.create_task(_run_routines(matched))
        _routine_tasks.add(t)
        t.add_done_callback(_routine_tasks.discard)

    async def _run_routine_test(actions: list) -> str:
        # The routines /test button: run the actions once, off the event loop (blocking
        # HA), through the SAME gated executor a fired routine uses. Returns the status.
        return await asyncio.to_thread(_routine_executor.run, actions)

    # Domains a routine may actuate via Home Assistant for the picker: actuatable AND
    # non-sensitive (media_player / lock / camera etc. stay out -- they are refused by
    # call_ha_service anyway, so offering them would only mislead). scene/script are
    # excluded too (INDIRECTION_DOMAINS): "everything off" is a multi-entity action list,
    # not a scene.
    _ROUTINE_HA_DOMAINS = frozenset({"light", "switch", "fan", "input_boolean"})

    def _ha_entities() -> list:
        client = client_from_config(cfg)
        if client is None:
            return []
        try:
            ents = client.get_entities()
        except Exception:
            return []   # an HA outage must never break the routines screen
        return [{"entity_id": e["entity_id"], "name": e.get("friendly_name") or e["entity_id"],
                 "domain": e["domain"]}
                for e in ents if e.get("domain") in _ROUTINE_HA_DOMAINS]

    def _routines_house_edge(home: bool) -> None:
        _dispatch_routines(_routines.on_house_edge(home))

    def _routines_person_edge(person: str, home: bool) -> None:
        _dispatch_routines(_routines.on_person_edge(person, home))

    def _routines_room_edge(room: str, occupied: bool) -> None:
        _dispatch_routines(_routines.on_room_edge(room, occupied))

    # Per-person arrived/left edges ("when I arrive") + a dedicated house-level arrived/
    # left edge detector, BOTH fed in real time off the always-running ingest path (see
    # _publish) rather than the hub. So routines depend on NO subscription/task and need
    # no lazy bring-up: an arrival fires the moment the fused state shows it, and a house
    # with zero routines still adds nothing (the trackers are cheap objects that just
    # observe `latest`). Consent is inherited: known_present_persons already applies the
    # identity/consent gate, so an anonymous/withdrawn device never produces a named edge.
    _person_presence = PersonPresence(on_edge=_routines_person_edge)
    _routine_house = AwayMonitor(away_grace=cfg.away_grace, on_edge=_routines_house_edge)
    _routine_rooms = RoomPresence(on_edge=_routines_room_edge)

    async def _routines_tick() -> None:
        # One time/deadline pass (test seam: app.state.routines_tick). Local wall-clock
        # (the house tz); matching is pure, only a fired routine touches a sink. AWAITS
        # the run so the seam is deterministic. Presence edges are NOT evaluated here --
        # they are driven in real time off the ingest path (see _publish); this pass only
        # handles schedule + house_away_by_time (which reads the ingest-fed house state).
        matched = _routines.tick(datetime.now(), _routine_house.home)
        await _run_routines(matched)

    async def _routines_loop():
        while True:
            await asyncio.sleep(_ROUTINES_INTERVAL_S)
            try:
                await _routines_tick()
            except Exception:
                logging.warning("routines tick failed", exc_info=True)

    # Rules/MQTT engine: opt-in via injected `rules_publish` (tests) or WAVR_MQTT_ENABLED
    # (real paho publisher, lazily connected). Off by default -- no publisher, no engine.
    _rules_publish = rules_publish
    if _rules_publish is None and cfg.mqtt_enabled:
        _rules_publish = make_publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix)
    _rules = RulesEngine(_rules_publish, prefix=cfg.mqtt_prefix) if _rules_publish else None
    # AwayMonitor runs whenever MQTT OR ntfy OR Telegram is opt-in'd -- all three
    # consumers need the SAME house-level arrived/left edge detection. `_rules_publish`
    # stays optional (AwayMonitor no-ops its own `publish` when None) so an ntfy/
    # Telegram-only setup gets notified without also needing WAVR_MQTT_ENABLED. The
    # `telegram` connector check here is a BOOT-TIME read (mirrors the narrator-
    # override-at-startup precedent below): enabling Telegram live via the Connectors
    # screen while nothing else is configured needs a restart before away-edge push
    # starts (honest limitation, not a silent gap -- the away monitor object itself
    # doesn't exist yet to fan out through).
    _away = (AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace,
                         notify=lambda msg: _notify_all(msg, kind="away_edge", severity="note"))
             if (_rules_publish or _notify or _connectors.is_enabled("telegram")) else None)

    # Narrator: opt-in via injected `narrator` (tests) or the two-factor gate below.
    # PROVIDER-AGNOSTIC (WAVR_NARRATE_PROVIDER=gemini|ollama|openai|anthropic); the
    # privacy allowlist (narrator.build_prompt) is shared by every provider. Off by
    # default -- no explicit opt-in, no narrator, 503 on call. The gate is a conscious
    # TWO-FACTOR check held PER PROVIDER: narrate_enabled AND provider_configured(cfg)
    # (a key present for cloud providers; merely selecting local Ollama, which needs
    # none). So merely having a key present (e.g. in ./.env) can't silently enable
    # cloud egress, and a local Ollama narrator is still an opt-in LLM call.
    _narrator = narrator
    if _narrator is None and cfg.narrate_enabled and provider_configured(cfg):
        _narrator = Narrator(make_generate(cfg))

    # Device metadata (Feature A): persisted per-MAC name + first/last-seen,
    # always built (like CameraStore) -- not itself opt-in, since naming is not
    # sensitive and the store is inert until something calls seen()/set_name().
    _owns_device_meta = device_meta is None
    _device_meta = device_meta or DeviceMeta(cfg.db_path)

    # A4 house memory: append-only, edge-triggered per-room occupancy history feeding
    # timeline/routine/anomaly reads (see wavr.occupancy_log). Mirrors gateway_monitor's
    # opt-in-store pattern (not device_meta's always-on one): an injected `occupancy_log`
    # (tests) wins outright; otherwise built ONLY when WAVR_OCCUPANCY_LOG is on. When it
    # stays None, `_publish` below simply skips logging (identical to today's behaviour --
    # no new table, no new reads, no `/api/occupancy/*` data).
    _owns_occupancy_log = False
    _occupancy_log = occupancy_log
    if _occupancy_log is None and cfg.occupancy_log_enabled:
        _occupancy_log = OccupancyLog(cfg.db_path, retention_days=cfg.occupancy_retention_days)
        _owns_occupancy_log = True

    # Runtime known-device store: persisted per-MAC known/unknown flag, always
    # built (like device_meta) -- inert until POST /api/inventory/known runs
    # (an empty store changes nothing: today's static WAVR_NET_MACS-only
    # behaviour). Lets an ordinary house device that was never on the static
    # env allowlist be marked known WITHOUT a restart -- the core fix for
    # rogue alerts firing on non-intruder devices.
    _owns_known_store = known_store is None
    _known_store = known_store or KnownStore(cfg.db_path)

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
        # 2C notify fan-out: always wired (not gated on `_notify` alone anymore) --
        # `_notify_all` is a no-op-cost no-op when both ntfy and Telegram are off,
        # and unconditional wiring means enabling the "telegram" connector live
        # (no restart) actually takes effect on the very next rogue-device sighting.
        on_rogue=lambda a: _notify_all(
            f"Wavr: dispositivo desconhecido na rede ({a.vendor})",
            kind="rogue_device", severity=a.severity),
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
        ha_store=_ha_import_store,
        # Runtime known-device store: read fresh every scan (same pattern as
        # device_meta/ha_store above) and unioned with the static
        # WAVR_NET_MACS allowlist -- a POST /api/inventory/known mark-known
        # takes effect on the very next scan with no restart.
        known_provider=_known_store.known_macs,
        # system-toggles sensing master (feature "system-toggles"): read fresh
        # every scan cycle -- a System-tab block/unblock takes effect on the
        # very next scan_once(), no restart. Gates the OPTIONAL active/passive
        # collectors (port scan, mDNS/SSDP/NetBIOS/SNMP/DHCP-fp, latency); the
        # base zero-egress ARP inventory (scan_inventory) is core LAN-read
        # presence, not an optional collector, and stays unaffected.
        sensing_allowed=_connectors.sensing_allowed)

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
    # "Approve on the Core" (design 2026-07-11): SECOND, additive onboarding path
    # alongside the 8-digit code above (PairingManager, unchanged, stays the
    # fallback). In-memory, never-persisted, TTL'd request/approval bookkeeping --
    # the only mint site is approve(), which calls the SAME _devices.add(name, role)
    # /api/pair already uses. None when multidevice is off (route mounting below is
    # itself gated on cfg.multidevice, so this stays inert either way).
    _pair_approvals = PairApprovalManager(_devices) if cfg.multidevice else None
    # Peer pairing (Phase 1): OWN-direction peer bookkeeping (how WE reach THEM) +
    # ephemeral in-memory handshake state. Built ONLY when WAVR_PEERS_ENABLED (which
    # the check above guarantees implies multidevice). PeerStore shares cfg.db_path but
    # owns its own `peers` table; closed in the lifespan finally alongside _devices.
    _peer_store = PeerStore(cfg.db_path) if cfg.peers_enabled else None
    # Sensor nodes (design 2026-07-11): NodeStore shares cfg.db_path but owns its own
    # `nodes` table (same pattern as PeerStore/DeviceStore); NodeEnroller is ephemeral
    # in-memory enrollment-code bookkeeping bound to the store. Built ONLY when
    # WAVR_NODES_ENABLED (which the check above guarantees implies multidevice); closed
    # in the lifespan finally alongside _peer_store.
    _node_store = NodeStore(cfg.db_path) if cfg.nodes_enabled else None
    _node_enroller = NodeEnroller(_node_store) if cfg.nodes_enabled else None

    async def _publish(rs, *, persist=True):
        # Shared publish path for both the event-driven ingest and the periodic
        # re-fuse tick. `persist=False` skips the DB write (the tick stores
        # on-change only, to avoid a row every few seconds per room forever).
        d = rs.to_dict()
        if persist:
            await asyncio.to_thread(_storage.insert_state, rs)  # fsync off the event loop
        # A4 house memory: independently edge-triggered off the room's OWN last-logged
        # row (OccupancyLog.append_if_changed), not off `persist` above -- so the tick's
        # every-5s no-op re-fuse passes never grow the table, but a genuine occupied/
        # confidence/person_count change is captured even on a `persist=False` tick pass.
        # None when disabled (WAVR_OCCUPANCY_LOG=0) -- identical to today's behaviour.
        if _occupancy_log is not None:
            await asyncio.to_thread(_occupancy_log.append_if_changed, d["room"],
                                    d["occupied"], d["confidence"],
                                    d.get("person_count"), d["ts"])
        latest[d["room"]] = d          # FULL internal truth (never suppressed in `latest`)
        # Drive the routines presence trackers off this always-running ingest (real time,
        # no hub subscription): the dedicated house edge detector per room, and the
        # per-person tracker off the current named-present set. Any arrived/left edge
        # dispatches a routine off-loop. Cheap + inert when the store has no routines.
        _routine_house.handle(d)
        _routine_rooms.handle(d["room"], bool(d["occupied"]))
        _person_presence.update(known_present_persons(latest.values()))
        # Watch/Guard: at THIS fan-out egress (WS clients + MQTT via the hub) publish the
        # SUPPRESSED view -- family geometry/identity/vitals stripped, only counts + the
        # intrusion room leave. Intrusion needs the consent identity layer to know who is
        # "known", so it is gated on identity_enabled: with identity off Watch still
        # suppresses (fail-safe privacy) but fires NO alert (it cannot honestly tell known
        # from unknown). `latest` stays FULL so the intrusion math + /api/state read the
        # real identities; only what leaves the box is projected.
        unrecognized = False
        if _watch.on and cfg.identity_enabled:
            known = len(known_present_persons(latest.values()))
            unrecognized = room_unrecognized(d, known)
            hit = _intrusion.record(d["room"], unrecognized, d.get("person_count"), known, d["ts"])
            if hit is not None and _notify:
                _notify("Wavr Vigia: pessoa nao reconhecida em " + str(d["room"]))
            # Build C4: forward the CURRENT active/clear verdict to MQTT every time (not
            # just on a new-alert edge) -- RulesEngine.handle_intrusion dedupes internally,
            # but it needs every re-evaluation to ever see the flagged-to-clear transition.
            if _rules is not None:
                _rules.handle_intrusion(d["room"], d["room"] in _intrusion.active_rooms())
            # House-level aggregate (room=None): catches a SPREAD-OUT intrusion no single
            # room's count reveals -- unaccounted people split across rooms so the honest
            # SUM exceeds the known-present count even when no one room does. Room-AGNOSTIC
            # + count-only: never says which room, who, or where. house_person_count is
            # None (never a fabricated 0) for a fully-uncounted house, so it stays silent
            # on "unknown". Edge-triggered like the per-room path -> fires once.
            house_count = house_person_count(latest.values())
            hhit = _intrusion.record(None, house_unrecognized(house_count, known),
                                     house_count, known, d["ts"])
            if hhit is not None and _notify:
                _notify("Wavr Vigia: pessoa nao reconhecida em casa")
            if _rules is not None:
                _rules.handle_intrusion(None, None in _intrusion.active_rooms())
        # A9 fall/no-motion suspicion (RESEARCH-GRADE, ADR-0003): independent of Watch/
        # identity -- posture is not family geometry/PII, so it is evaluated unconditionally
        # once opted in (`_fall` is None otherwise). Reads `d["targets"]` -- the FULL
        # internal truth, same as the intrusion check above -- so a Watch-suppressed egress
        # never blinds this rule. The alert itself carries only room + duration (see
        # wavr.fall_detect.FallAlert); it reaches GET /api/alerts via merge_alerts, never
        # through the (possibly-suppressed) hub/WS/MQTT state fan-out below.
        if _fall is not None:
            at_risk = lying_outside_zone(_house, d["room"], d.get("targets") or [])
            fhit = _fall.record(d["room"], at_risk, d["ts"])
            if fhit is not None:
                # 2C notify fan-out: unconditional call (see on_rogue above for why) --
                # _notify_all no-ops cleanly when both ntfy and Telegram are off.
                _notify_all("Wavr: possivel queda em " + str(d["room"]) +
                            " (deteccao experimental, nao e um dispositivo medico -- ADR-0003)",
                            kind="fall_suspected", severity="alert", room=d["room"])
        await _hub.publish(project_state(d, _watch.on, unrecognized))
        return d

    async def _ingest(event):
        rs = _fusion.update(event)
        # PERF-CRITICAL (SD-card write-wear on the live G9 Core): mmwave publishes at
        # ~0.2s cadence, camera at ~0.5s -- writing a full sqlite INSERT+commit to
        # `_storage` (the `room_states` table GET /api/history and /api/narrate read)
        # on EVERY SensingEvent wears the card for no benefit when the fused RoomState
        # hasn't actually changed (a still room re-asserting the same reading every
        # frame). Mirror _refuse_once's own persist=changed change-gate here, but
        # compare the FULL derived state (not just occupied/confidence) so a
        # sources[]/targets/identities/person_count-only change still persists. `ts` is
        # excluded from the comparison -- it is per-event metadata (every event has a
        # new ts by construction), not signal state; including it would make the
        # comparison never match and defeat the whole gate.
        #
        # DURABILITY TRADE-OFF: a room holding a perfectly steady reading now logs only
        # its LAST-persisted row's `ts`, not every intervening (identical) event's --
        # /api/history and /api/narrate see fewer, coarser rows while a room is steady
        # (still an honestly reconstructible history: state X held from that row's ts
        # until the next one), never a WRONG one. `occupancy_log` (A4 house memory,
        # below via `_publish`) is UNCHANGED -- it already applies its own independent
        # on-change gate (`OccupancyLog.append_if_changed`) regardless of `persist`.
        prev = latest.get(event.room)
        new = rs.to_dict()
        changed = prev is None or (
            {k: v for k, v in new.items() if k != "ts"}
            != {k: v for k, v in prev.items() if k != "ts"}
        )
        await _publish(rs, persist=changed)

    async def _refuse_once():
        # One periodic re-fuse pass. Fusion is otherwise purely event-driven, so a
        # room whose only source (e.g. a single camera) is unplugged/disabled stops
        # emitting frames and its last reading FREEZES ("occupied 82%" forever).
        # Re-running fusion against the wall clock ages every known room: a dead
        # source's freshness decays to zero (fusion._freshness) and the vacate dwell
        # finally advances, so the room honestly fades to unoccupied. FAIL-SAFE:
        # confidence can only DROP as a source ages — the tick never invents presence
        # (num only accumulates from real presence events). Store-on-change only
        # (a row every few seconds per room forever is unacceptable DB bloat); always
        # refresh latest + hub so the live map fades. Per-room guarded so one bad
        # room never stalls the sweep.
        for room in _fusion.rooms():
            try:
                rs = _fusion.state(room)
                if rs is None:
                    continue
                prev = latest.get(room)
                changed = (prev is None or prev["occupied"] != rs.occupied
                           or abs(prev["confidence"] - rs.confidence) >= 0.01)
                await _publish(rs, persist=changed)
            except Exception:
                logging.warning("refuse tick failed for room %s", room, exc_info=True)

    async def _publish_derived_mqtt():
        # Build C4: push A4's routine-anomaly + A10's composed house-status verdict
        # onto MQTT so a user can build Home Assistant automations off them (ADR-0005
        # -- Wavr stays a signal SOURCE, never an automation engine). No-op when
        # MQTT/rules aren't wired, mirroring every other `_rules is None` skip in this
        # module. Runs on the SAME cadence as the re-fuse tick (cfg.refuse_interval) --
        # no new loop/config knob -- and RulesEngine dedupes internally, so a steady
        # "nothing to report" tick never re-publishes.
        if _rules is None:
            return
        routine_flags = []
        if _occupancy_log is not None:
            now = datetime.now(timezone.utc)
            rooms = list(latest.items())
            # Concurrent, off the event loop -- mirrors GET /api/house-status's own
            # per-room is_unusual() sweep (one sqlite read per currently-fused room,
            # never a blocking serial loop).
            checks = await asyncio.gather(*(
                asyncio.to_thread(_occupancy_log.is_unusual, room, d.get("occupied"), at=now)
                for room, d in rooms
            ))
            for (room, _d), verdict in zip(rooms, checks):
                # is_unusual()'s honest `None` ("insufficient data") folds to False here
                # -- an anomaly binary_sensor must never assert "unusual" on a "don't
                # know" verdict.
                unusual = verdict.get("unusual") is True
                _rules.handle_routine_anomaly(room, unusual)
                if unusual:
                    routine_flags.append({"room": room, "ts": now.isoformat()})
        network_alerts = merge_alerts(_inventory, dhcp_monitor=_dhcp_monitor,
                                      gateway_monitor=_gateway_monitor)
        status = compose_house_status(
            network_alerts=network_alerts,
            intrusion_alerts=_intrusion.active_alerts(),
            fall_alerts=_fall.active_alerts() if _fall is not None else None,
            routine_flags=routine_flags,
        )
        _rules.handle_house_status(status)

    async def _refuse_loop():
        while True:
            await asyncio.sleep(cfg.refuse_interval)
            await _refuse_once()
            try:
                await _publish_derived_mqtt()
            except Exception:
                logging.warning("publish_derived_mqtt tick failed", exc_info=True)

    # Consent-first identity/device registry (2026-07-06 ethics decision): the
    # persistent, admin-confirmed source of {addr -> person}. Built like CameraStore
    # (always available, shares wavr.db). The two providers below are re-read by the
    # BLE/network sources each scan cycle -- env allowlist merged with the registry,
    # registry taking precedence -- so a registration/opt-out is live without restart.
    # Default behaviour is unchanged when the registry is empty (env still works).
    _owns_identity = identity_store is None
    _identity_store = identity_store or IdentityStore(cfg.db_path)
    _bonded_reader = bonded_reader or read_bonded

    # Wavr Assistant engine picker (Phase 2B): the persisted selection + the one
    # "manual" engine's non-secret config. Always built (like CameraStore/
    # ConnectorStore), inert until the admin picks/asks something.
    _owns_assistant = assistant_store is None
    _assistant = assistant_store or AssistantEngineStore(cfg.db_path)
    # The Wavr Assistant's cloud-egress kill switch: a SEPARATE connector row from
    # "narrator" (a distinct feature -- the tool-using assistant picker, not the
    # single-shot dashboard summarizer) so each has its own independent DEFAULT-OFF
    # toggle, same granularity precedent as mcp-read vs mcp-http vs ha-import vs
    # ha-control each being their own row. `kind="generic"` reuses the EXISTING
    # single-egress-surface gate verbatim (ConnectorStore.is_enabled) -- no second,
    # ungated egress path. Idempotent upsert: NEVER flips an already-persisted
    # enabled bit (ConnectorStore.upsert's own contract), so re-wiring on every
    # restart can't silently re-arm a kill-switched connector.
    _connectors.upsert(
        "assistant-cloud", "generic", "Wavr Assistant (cloud engine)",
        scope=("outbound-cloud: OpenAI/Anthropic/Gemini, or ANY engine (including "
              "Wavr Assistant/Local LLM) whose actual configured endpoint is not "
              "loopback -- coarse current-state tool scope only (rooms/room-context/"
              "house-status); never the floor-plan/house-map geometry, network "
              "inventory, occupancy history, alerts, or Home Assistant entity names"))

    # 2C first-wave external connectors (project_wavr_agentic_home_mission /
    # DESIGN-external-connectors.md, backend/wavr/connectors/): each is its own
    # `kind="generic"` row, DEFAULT-OFF, same idempotent-upsert contract as
    # "assistant-cloud" above -- an upsert NEVER flips an already-persisted enabled
    # bit, so re-running this on every restart can't silently re-arm a kill-switched
    # connector. Scope strings are the HONEST, code-verified disclosure each module's
    # own docstring documents (see connectors/enrich/*.py, connectors/notify/*.py) --
    # copied here verbatim so the UI badge matches what the code actually does.
    # None of these are called from anywhere yet except the notify fan-out
    # (rogue-device/away-edge/fall -> "telegram") and the digest scheduler below
    # ("digest") -- the enrich connectors (open-meteo/urlhaus/abuseipdb/wikipedia)
    # are registered so an admin CAN opt in, but nothing in app.py calls their
    # fetch()/lookup() closures yet (a separate, future wiring step, same as any
    # other not-yet-consumed generic row).
    _connectors.upsert(
        "open-meteo", "generic", "Open-Meteo Weather",
        scope=("outbound-location: house's own COARSE lat/lon (rounded to 2dp, ~1.1km "
              "grid) to Open-Meteo, keyless. Requires WAVR_HOME_LAT/WAVR_HOME_LON "
              "explicitly configured; no default coordinate. Weather-only response, "
              "nothing else disclosed."))
    _connectors.upsert(
        "telegram", "generic", "Telegram Notify",
        scope=("outbound-notify: kind/severity/room/summary only -- never a raw camera "
              "frame, occupancy vitals/geometry, MAC, or credential. Summary is "
              "runtime-sanitised (MAC-like/coordinate-like/rtsp/frame tokens redacted) "
              "as defence-in-depth on top of the field allowlist. Requires "
              "WAVR_TELEGRAM_TOKEN + WAVR_TELEGRAM_CHAT_ID; token never logged."))
    _connectors.upsert(
        "digest", "generic", "Daily Digest (proactive push)",
        scope=("outbound-notify, PROACTIVE & UNPROMPTED (routes through Telegram/ntfy, "
              "whichever is enabled+configured): once/day whole-house empty-window "
              "schedule + alert/new-device counts + routine status -- never identity, "
              "geometry, or room names. HONESTY: the empty-window schedule itself is "
              "burglary-relevant even with no identity disclosed, and this pushes it "
              "unprompted rather than waiting for a GET -- a SEPARATE opt-in from "
              "\"telegram\"/ntfy alone, deliberately: enabling Telegram for occasional "
              "alerts does NOT imply wanting this daily schedule push too."))
    _connectors.upsert(
        "urlhaus", "generic", "URLhaus (malware URL/host/hash lookup)",
        scope=("outbound-enrich: exactly one URL/hostname/hash under investigation, "
              "keyless. Nothing about the house (device inventory, MAC, the querier's "
              "own IP) leaves."))
    _connectors.upsert(
        "abuseipdb", "generic", "AbuseIPDB (IP reputation lookup)",
        scope=("outbound-enrich: a single remote peer IP the house's devices have "
              "talked to -- never a house device's own IP/MAC. This IS genuine "
              "third-party disclosure (the query itself is the leak); key sent in "
              "header only via WAVR_ABUSEIPDB_KEY, rate-limit-aware."))
    _connectors.upsert(
        "wikipedia", "generic", "Wikipedia Lookup",
        scope=("outbound-lookup: the user's own search-query text only, keyless. No "
              "house/device/occupancy state is folded into the query."))

    # Connectors override: an admin who turned the narrator ON in the Connectors screen
    # (a persisted enable override, WAVR_NARRATE_ENABLED unset) gets the provider client
    # built HERE, at startup, exactly like the env path above -- so the override actually
    # activates the feature after a restart. Still TWO-FACTOR: provider_configured(cfg)
    # must hold (a key for a cloud provider, or the local Ollama selection), so an
    # enable-without-a-key stays inert (the /api/narrate chokepoint reports "needs a
    # provider key" and egresses nothing). No key configured => _narrator stays None.
    if (_narrator is None and provider_configured(cfg)
            and _connectors.override("narrator") == "on"):
        _narrator = Narrator(make_generate(cfg))

    # Core Panel admin unlock PIN: always built (like CameraStore/ConnectorStore),
    # inert until POST /api/core/pin sets one -- an unset PIN just verifies False
    # (never a crash / never a bypass). PinAttemptLimiter is pure in-memory
    # (ephemeral by design, mirrors PairingManager's failed-attempt window) so it
    # is never persisted/injectable via create_app.
    _owns_pin_store = pin_store is None
    _pin_store = pin_store or PinStore(cfg.db_path)
    _pin_limiter = PinAttemptLimiter()

    # Companion presence self-registration (Feature: a paired LAN companion
    # registers its OWN device by source IP -> ARP -> MAC). Injectable for tests
    # (a canned resolver keyed by IP); defaults to the real ARP-table resolver.
    _resolve_companion_mac = companion_resolve_mac or resolve_source_mac

    def _ble_known_provider() -> dict:
        merged = dict(cfg.ble_known)
        merged.update(_identity_store.as_ble_map())   # registry wins ties
        return merged

    def _consent_of(device_id: str) -> str:
        """This device's LIVE participation tri-color, for the identity registry's
        consent gate. Fails CLOSED at every step: a grant we cannot READ is not a
        grant we may act on.

          * ROOT_DEVICE_ID -- the operator's own loopback box. It holds no Device
            row by design (its lever is /api/system/toggle, hence the 409 on
            /api/consent), so it is the one companion row that legitimately has no
            level to look up.
          * _devices is None -- multidevice is OFF (cfg.multidevice, :711). Identity
            rows OUTLIVE that flag: a companion can have registered green, withdrawn
            to red, and only then had multidevice disabled. Returning "green" here
            (as this did until 2026-07-16) silently RESURRECTED the withdrawn name,
            turning a feature flag into a consent override. With multidevice off
            nothing can pair or register anyway, so the only rows this darkens are
            exactly the leftovers that must stay dark.
          * no Device row -- hard-deleted, or a row from another DB.

        `consent or "green"` mirrors devices.Device.to_dict: NULL means "never
        explicitly set", which resolves to green everywhere."""
        if device_id == ROOT_DEVICE_ID:
            return "green"
        if _devices is None:
            return "red"
        device = _devices.get(device_id)
        if device is None:
            return "red"
        return device.consent or "green"

    # Make the registry consent-aware BEFORE anything reads it. Without this the
    # tri-color would only ever be enforced where a row is WRITTEN, which is the
    # bug this closes: the level is changed LATER (POST /api/consent), so a device
    # registered green and then withdrawn to red kept feeding named presence.
    _identity_store.set_consent_lookup(_consent_of)

    def _net_known_provider() -> dict:
        merged: dict[str, str | None] = dict(cfg.net_known)
        # as_net_known (not as_net_map): the PRESENCE map, which also carries the
        # {mac: None} "counted but never named" entries that yellow needs. The env
        # allowlist has no consent axis -- it is the operator's own box -- so it
        # stays as-is and the registry's live decision is layered on top.
        merged.update(_identity_store.as_net_known())
        return merged

    manager = SourceManager(_ingest)
    for name, factory, enabled in (
            sources if sources is not None
            else _default_sources(cfg, _ble_known_provider, _net_known_provider,
                                  _identity_store.detailed_net_addresses)):
        manager.register(name, factory, enabled)

    def _ensure_ble_source() -> None:
        # Live bring-up: register the BLE source the moment the first BLE device is
        # registered on an install that had none, so the registration takes effect
        # without a restart. Boots ENABLED (a device only reaches the registry by an
        # affirmative consent act) -- SourceManager spawns it immediately when running.
        # No-op if a 'ble' source already exists or default sources are overridden.
        if "ble" in {s["name"] for s in manager.status()["sources"]}:
            return
        manager.register("ble", lambda: BLESource(
            cfg.ble_known, room=cfg.ble_room, rssi_min=cfg.ble_rssi_min,
            interval=cfg.ble_interval, emit_identity=cfg.identity_enabled,
            known_provider=_ble_known_provider), True)

    _owns_cameras = camera_store is None   # only close a store this function built itself
    _cameras = camera_store or CameraStore(cfg.db_path)
    # Spec A per-camera localization calibration (mount prior + optional 4-point
    # homography). Always built (like CameraStore/DeviceMeta) -- inert until a camera has
    # a row; NEVER a frame, only stored matrices/detection-space parameters (ADR-0002).
    _calib = CalibrationStore(cfg.db_path)
    # Walk-to-calibrate feet-pixel sink (Spec A). In-memory, ephemeral, coordinate-only
    # -- NEVER a frame (ADR-0002). Populated ONLY while a calibration session is active
    # (see POST /api/cameras/{name}/calib-session); read by GET calib-sample. Inert and
    # empty out of the box, so it costs nothing until an operator starts a walk.
    _calib_sample = CalibSampleStore()
    # Guided-calibration session state machine (Tier 1's session half): the
    # server-side 'stand here -> capture -> repeat -> solve' walk bookkeeping, so the
    # quest survives a frontend reload and a non-browser driver (MCP/voice) can run
    # it. In-memory, ephemeral, coordinate-only (ADR-0002) -- same lifecycle class as
    # _calib_sample; nothing here reaches CalibrationStore/disk until an explicit,
    # successful solve (change-gated write, SD-wear).
    _calib_session = CalibSessionStore()
    # F3 camera IP-drift monitor: always available (like _device_meta), inert until a
    # camera reports down AND a stored MAC drifts. Reads camera defs from _cameras and
    # the current LAN devices from _inventory (opt-in WAVR_NET_INVENTORY -- when off,
    # latest_inventory() is empty and suggestions stay honestly empty).
    _camera_health = CameraHealthMonitor(
        get_camera=_cameras.get, latest_inventory=_inventory.latest_inventory)
    # network-doctor (GET /api/health/doctor): bounded in-memory log of executed
    # auto-fixes -- always built (like _camera_health), inert until the route is
    # actually called with auto_fix=true AND WAVR_NET_DOCTOR_AUTOFIX is on.
    _doctor_log = DoctorLog()
    for cam in _cameras.list():                       # persisted cameras -> boot-OFF sources
        manager.register(cam["name"],
                         _camera_factory(cam, cfg, _camera_health.report, _calib, _house,
                                        on_privacy=_camera_health.report_privacy),
                         False)

    def _masked_cameras():
        # Per-camera liveness enum (read-only, name+enum only — no frame, no creds).
        # 'offline' = F3 health hook latched the camera down (frames stopped >=
        # cam_unhealthy_secs) — something is actually wrong, worth attention. 'privacy'
        # = the RTSP session opened but produced no frames (CameraPrivacySignal): the
        # honest read for a deliberately-covered Tapo camera — NOT an error, NEVER
        # counted as 'offline'/unhealthy, so a covered camera never cries wolf. 'live' =
        # its source task is running (frames flowing); 'unknown' = registered but
        # boot-OFF / not started, so we HONESTLY don't know — never asserted empty.
        # 'offline' wins over 'privacy' if a camera somehow reports both in the same
        # tick (a real fault is worth surfacing even if a stale privacy read lingers).
        down = set(_camera_health.down())
        privacy = set(_camera_health.privacy())
        active = {s["name"]: s["active"] for s in manager.status()["sources"]}
        out = []
        for cam in _cameras.list():
            name = cam["name"]
            liveness = ("offline" if name in down
                        else "privacy" if name in privacy
                        else "live" if active.get(name) else "unknown")
            out.append({**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"]),
                        "liveness": liveness})
        return out

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

    # PERF cache for _compute_house_status's routine/is_unusual sweep -- see
    # _HOUSE_STATUS_ROUTINE_TTL_S above. Single-slot (house-wide, not per-window_minutes:
    # routine_flags never depends on window_minutes, only the network-alert filter
    # inside compose_house_status does, and that stays uncached below).
    _routine_cache: dict = {"ts": None, "flags": []}

    async def _compute_house_status(window_minutes: float = DEFAULT_NETWORK_WINDOW_MINUTES) -> dict:
        # Build A10 v0: the unified "esta tudo bem em casa?" answer, fusing the NETWORK
        # layer (rogue-device/rogue-DHCP/gateway-identity -- the SAME `merge_alerts()`
        # GET /api/alerts uses, so the two views can never disagree on what a network
        # alert IS) with the PHYSICAL layer (Watch's A2 currently-ACTIVE intrusion rooms
        # + A4's current-hour occupancy anomaly). Derived-only composition of signals
        # that already exist -- see wavr.house_status's module docstring for the
        # recency-window/score honesty rules. Factored out (mirrors merge_alerts's one-
        # function-many-callers precedent) so GET /api/house-status AND the
        # get_house_status MCP tool (wavr.mcp, via mcp_http_route wiring below) share
        # this EXACT composition and can never drift.
        network_alerts = merge_alerts(_inventory, dhcp_monitor=_dhcp_monitor,
                                      gateway_monitor=_gateway_monitor)
        intrusion_alerts = _intrusion.active_alerts()
        fall_alerts = _fall.active_alerts() if _fall is not None else None
        routine_flags = []
        if _occupancy_log is not None:
            now = datetime.now(timezone.utc)
            cached_ts = _routine_cache["ts"]
            if cached_ts is not None and (now - cached_ts).total_seconds() < _HOUSE_STATUS_ROUTINE_TTL_S:
                # Amortize the expensive per-room sqlite sweep below -- see
                # _HOUSE_STATUS_ROUTINE_TTL_S. A few seconds of staleness on a
                # "is this hour's occupancy unusual" note is a non-issue; a genuinely
                # new anomaly is still caught within one TTL window, never suppressed.
                routine_flags = _routine_cache["flags"]
            else:
                rooms = list(latest.items())
                # Concurrent, off the event loop -- one sqlite read per currently-fused
                # room, never a blocking serial loop.
                checks = await asyncio.gather(*(
                    asyncio.to_thread(_occupancy_log.is_unusual, room, d.get("occupied"), at=now)
                    for room, d in rooms
                ))
                routine_flags = [
                    {"room": room, "ts": now.isoformat()}
                    for (room, _d), verdict in zip(rooms, checks)
                    if verdict.get("unusual") is True
                ]
                _routine_cache["ts"] = now
                _routine_cache["flags"] = routine_flags
        return compose_house_status(network_alerts=network_alerts,
                                    intrusion_alerts=intrusion_alerts,
                                    fall_alerts=fall_alerts,
                                    routine_flags=routine_flags,
                                    window_minutes=window_minutes)

    async def _digest_once() -> dict:
        # One daily-digest composition+send pass (test seam: app.state.digest_once,
        # mirrors _refuse_once -- a test drives one deterministic tick without
        # waiting _DIGEST_INTERVAL_S). Gated on the "digest" connector's OWN row --
        # SEPARATE from "telegram"/ntfy being merely configured for regular alerts,
        # see connectors/notify/digest.py's HONESTY note: reads NOTHING (not even
        # occupancy_log) when the gate is off, so a disabled digest is byte-identical
        # to before this feature existed.
        try:
            # The gate lives INSIDE the try on purpose. It is a raw sqlite read
            # (ConnectorStore.get -> SELECT, unguarded), and `wavr.db` is shared by five
            # stores on SD-card-backed sqlite on a Core that runs for weeks -- "database is
            # locked" is a real recurring error class there, not a theoretical one. Sitting
            # one line ABOVE the try, a single blip propagated out of _digest_once, exited
            # _digest_loop's `while True` (which has no guard of its own), and killed the
            # scheduler for the whole process lifetime -- with ZERO log output: the task's
            # exception was never retrieved, the strong reference kept it from being GC'd so
            # asyncio's own "exception was never retrieved" fallback never fired, and the
            # shutdown `suppress(CancelledError, Exception)` swallowed the last chance to see
            # it. The tick runs once per 24h, so nothing ever retried. Same hazard the sibling
            # _refuse_loop already guards deliberately.
            # Gate semantics are unchanged: it is still evaluated FIRST and still reads
            # NOTHING (not even occupancy_log) when the digest is off.
            if not _connectors.is_enabled("digest"):
                return {"ok": False, "status": "disabled", "via": None}
            now = datetime.now(timezone.utc)
            start = now - timedelta(seconds=_DIGEST_INTERVAL_S)
            house_status = await _compute_house_status()
            # SAME merged alert list every alert-consuming surface reads (GET
            # /api/alerts, _assistant_tool_deps, _compute_house_status's own
            # network layer) -- never a second, divergent count.
            alert_count = len(merge_alerts(
                _inventory, dhcp_monitor=_dhcp_monitor, gateway_monitor=_gateway_monitor,
                intrusion_log=_intrusion, fall_log=_fall,
                intrusion_house_loud=cfg.watch_intrusion_loud))
            start_iso = start.isoformat()
            # new_device_count: DeviceMeta's own public first-seen field, counted
            # over the SAME trailing window compose_digest reconstructs occupancy
            # for -- always built/available (see _device_meta's own "always on"
            # docstring), never a name/hostname, count only.
            new_device_count = sum(
                1 for d in _device_meta.all().values()
                if d.get("first_seen") and d["first_seen"] >= start_iso)
            digest = compose_digest(
                occupancy_log=_occupancy_log, house_status=house_status,
                alert_count=alert_count, new_device_count=new_device_count,
                start=start, end=now, now=now)
            # send_digest's own gate: telegram_send re-checks is_enabled("telegram")
            # internally; ntfy_notify is `_notify` (None unless WAVR_NTFY_URL/an
            # injected test notify is configured) -- a no-op with zero egress when
            # neither sink is actually usable, even with "digest" enabled.
            return await asyncio.to_thread(
                send_digest, digest, telegram_send=_telegram_send, ntfy_notify=_notify)
        except Exception:
            logging.warning("daily digest tick failed", exc_info=True)
            return {"ok": False, "status": "error", "via": None}

    async def _digest_loop():
        while True:
            await asyncio.sleep(_DIGEST_INTERVAL_S)
            await _digest_once()

    def _assistant_tool_deps() -> dict:
        # The SAME already-built, already-scanned data sources the MCP-HTTP mount
        # below reuses -- built here UNCONDITIONALLY (not gated behind
        # cfg.multidevice / the [mcp] extra), because the Wavr Assistant loop is an
        # IN-PROCESS caller of the plain wavr.mcp functions, not the MCP-over-HTTP
        # transport (Phase 2B design spec §4: "no [mcp] extra, no second transport,
        # no network hop for a same-process tool call"). `ha_client` is rebuilt live
        # per call (client_from_config is a cheap, pure constructor -- same
        # precedent as its other inline per-request use at POST /api/ha/import).
        return {
            "fusion": _fusion,
            "house_map": _house,
            "ha_client": client_from_config(cfg),
            "network_inventory_fn": lambda: inventory_view(_inventory, _device_meta),
            "alerts_fn": lambda: merge_alerts(
                _inventory, dhcp_monitor=_dhcp_monitor, gateway_monitor=_gateway_monitor,
                intrusion_log=_intrusion, fall_log=_fall,
                intrusion_house_loud=cfg.watch_intrusion_loud),
            "occupancy_provider": _occupancy_log,
            "house_status_fn": _compute_house_status,
        }

    # MCP-over-streamable-HTTP (ADR-0008, Slice 1): mount the READ-ONLY MCP transport
    # in-process at /mcp so it inherits loopback_or_authed + TrustedHostMiddleware + TLS +
    # DeviceStore. Wired ONLY when multidevice is ON (TLS present) AND the [mcp] extra is
    # importable; the per-request mcp-http kill-switch (Connectors, default-OFF) is enforced
    # inside the guard. call_ha_service is ABSENT from this transport (read-only). The stdio
    # bridge (wavr.mcp_serve) keeps the full gated toolset, unchanged.
    _mcp_http_route = None
    _mcp_http_sm = None
    if cfg.multidevice:
        try:
            from wavr.mcp import FusionStateProvider
            from wavr.mcp_http import build_mcp_http_mount
            _mcp_http_route, _mcp_http_sm = build_mcp_http_mount(
                FusionStateProvider(_fusion, _house),
                is_enabled=lambda: _connectors.is_enabled("mcp-http"),
                local_ip=_local_ip, ha_client=client_from_config(cfg),
                # Phase 2A / B1-B3: read the WHOLE house, not just rooms. Every one
                # reuses the SAME already-scanned/already-composed data sources the
                # equivalent HTTP route reads -- none of these trigger a rescan or a
                # new detection pass.
                network_inventory_fn=lambda: inventory_view(_inventory, _device_meta),
                alerts_fn=lambda: merge_alerts(
                    _inventory, dhcp_monitor=_dhcp_monitor, gateway_monitor=_gateway_monitor,
                    intrusion_log=_intrusion, fall_log=_fall,
                    intrusion_house_loud=cfg.watch_intrusion_loud),
                occupancy_provider=_occupancy_log,
                house_status_fn=_compute_house_status)
        except ImportError:
            logging.info("MCP-over-HTTP mount skipped: [mcp] extra not installed")

    # Mutable single-slot holder for the mDNS self-advertise handle (Phase 1 peer
    # discovery). A bare local inside `lifespan()` below is NOT visible to route
    # closures (different nested-function scope) -- network-doctor's mdns_advertise
    # check/fix needs to read AND replace the live handle from a route, so it lives
    # here in create_app's own scope instead. `lifespan()` and the route below both
    # read/write `_mdns_state["handle"]`; None means "not currently advertising".
    _mdns_state: dict = {"handle": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # MCP-over-HTTP session manager (once per process) when the read-only mount is
        # wired. The CM is created here but ENTERED last -- just before the yield below,
        # AFTER every fallible startup step -- so a failure in manager.start() or any
        # collector start can't leave the session-manager task group orphaned (appsec LOW).
        # Exited in the finally. No-op when the mount is absent.
        _mcp_cm = _mcp_http_sm.run() if _mcp_http_sm is not None else None
        # Peer discovery (Phase 1): advertise THIS instance as `_wavr._tcp` so LAN peers
        # can browse and find it (Core already does this natively; Desktop has no native
        # equivalent, so it advertises from Python). LAZY import inside the guard --
        # `zeroconf` is the optional [mdns] extra and is absent in a base/test install --
        # and the whole start is wrapped so a missing dep or a registration failure LOGS
        # and continues instead of crashing startup: peer discovery is a convenience,
        # never load-bearing for the app booting. Handle stopped in the finally.
        if cfg.peers_enabled:
            try:
                from wavr.mdns_peers import advertise_self
                _mdns_state["handle"] = advertise_self(cfg.instance_name, cfg.port, role="desktop")
            except Exception:
                logging.warning("peer mDNS self-advertise unavailable "
                                "(zeroconf missing or registration failed)", exc_info=True)
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
        # Periodic re-fuse loop (WAVR_REFUSE_S, default 5s; 0 disables). Ages rooms
        # that have stopped receiving events so a disconnected source fades to
        # unoccupied instead of freezing its last reading.
        refuse_task = (asyncio.create_task(_refuse_loop())
                       if cfg.refuse_interval > 0 else None)
        # Daily-digest scheduler (2C): always created (a bare sleep-then-check loop,
        # zero cost while asleep) -- _digest_once itself is the real gate (the
        # "digest" connector row), so flipping that on via the Connectors screen
        # takes effect on the NEXT tick with no restart, unlike AwayMonitor's
        # Telegram wiring above.
        digest_task = asyncio.create_task(_digest_loop())
        routines_task = asyncio.create_task(_routines_loop())
        # Enter the MCP-over-HTTP session manager LAST: all fallible startup is done, so it
        # can't be orphaned by an earlier failure. Requests aren't served until after the
        # yield, so the transport is live before the first /mcp dispatch.
        if _mcp_cm is not None:
            await _mcp_cm.__aenter__()
        try:
            yield
        finally:
            # Suppress CancelledError AND any error a caller-injected publisher
            # might raise, so shutdown always reaches manager.stop() + camera close.
            for t in (rules_task, away_task, refuse_task, digest_task, routines_task,
                      *list(_routine_tasks)):
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
            with suppress(Exception):
                _calib.close()
            if _owns_identity:
                with suppress(Exception):
                    _identity_store.close()
            if _owns_connectors:
                with suppress(Exception):
                    _connectors.close()
            if _owns_assistant:
                with suppress(Exception):
                    _assistant.close()
            if _owns_pin_store:
                with suppress(Exception):
                    _pin_store.close()
            if _owns_device_meta:
                with suppress(Exception):
                    _device_meta.close()
            if _owns_occupancy_log:
                with suppress(Exception):
                    _occupancy_log.close()
            if _owns_known_store:
                with suppress(Exception):
                    _known_store.close()
            if _owns_ha_store:
                with suppress(Exception):
                    _ha_import_store.close()
            if _owns_gateway_store and _gateway_store is not None:
                with suppress(Exception):
                    _gateway_store.close()
            if _mdns_state["handle"] is not None:
                with suppress(Exception):
                    _mdns_state["handle"].stop()   # unregister `_wavr._tcp` + close zeroconf
            if _peer_store is not None:
                with suppress(Exception):
                    _peer_store.close()
            if _node_store is not None:
                with suppress(Exception):
                    _node_store.close()
            if _devices is not None:
                with suppress(Exception):
                    _devices.close()
            if _mcp_cm is not None:
                with suppress(Exception):
                    await _mcp_cm.__aexit__(None, None, None)

    app = FastAPI(title="Wavr", lifespan=lifespan)
    # Test seams (not routes — never reachable over HTTP, carry no secrets):
    #  * refuse_once: the periodic re-fuse body, invokable once without loop timing
    #    so a test can drive one deterministic decayed tick.
    #  * camera_health: the F3 monitor, so a test can latch a camera down/up and
    #    assert the /api/cameras liveness tri-state (names only, no frame/creds).
    app.state.refuse_once = _refuse_once
    #  * publish_derived_mqtt: Build C4's routine-anomaly/house-status MQTT tick, so a
    #    test can drive one deterministic push without waiting on cfg.refuse_interval.
    app.state.publish_derived_mqtt = _publish_derived_mqtt
    app.state.camera_health = _camera_health
    #  * net_known_provider: the exact callable NetworkSource re-reads every scan
    #    cycle, so a test can assert what a given consent level actually DELIVERS
    #    to presence (counted? named?) without driving a real ARP scan.
    app.state.net_known_provider = _net_known_provider
    #  * calib_sample: the walk-to-calibrate feet-pixel sink, so a test can record a
    #    coordinate and assert GET calib-sample surfaces it (never a frame -- ADR-0002).
    app.state.calib_sample = _calib_sample
    #  * calib_session: the guided-calibration session state machine, so a test can
    #    drive/inspect a walk's server-side state directly (never a frame -- ADR-0002).
    app.state.calib_session = _calib_session
    #  * ingest: the fusion event-ingest path (SourceManager's on_event), so a test can
    #    drive one deterministic SensingEvent through fusion + the SD-wear persist=
    #    changed write-gate without needing a real/timed source or a node bearer token.
    app.state.ingest = _ingest
    #  * digest_once: the daily-digest composition+send body (2C), so a test can drive
    #    one deterministic tick without waiting _DIGEST_INTERVAL_S -- mirrors refuse_once.
    app.state.digest_once = _digest_once
    #  * routines_tick: one time/deadline routines pass, so a test can drive a schedule
    #    or house_away_by_time trigger deterministically without waiting on the loop.
    app.state.routines_tick = _routines_tick
    #  * routine_store: the live store, so a test can seed a routine + assert what a real
    #    arrived/left edge or a tick actually fired.
    app.state.routine_store = _routine_store
    #  * person_presence: the per-person tracker, so a test can drive an arrival/departure
    #    directly (update({...})) and assert a person_arrived/left routine fires, without
    #    injecting full fusion state.
    app.state.person_presence = _person_presence
    #  * routine_house: the dedicated house edge detector, so a test can drive a house
    #    arrived/left (handle({"room":.., "occupied":..})) and assert a house routine fires.
    app.state.routine_house = _routine_house
    #  * routine_rooms: the per-room edge detector, so a test can drive a room fill/empty
    #    (handle(room, occupied)) and assert a room_occupied/room_empty routine fires.
    app.state.routine_rooms = _routine_rooms

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

    def require_scope(scope: str):
        # Wavr Pass (Phase 1): a dependency FACTORY -- `Depends(require_scope("control"))`
        # wires one ADDITIONAL scope check onto a route. Never a substitute for an
        # existing require_local/require_central/require_root/require_authenticated gate
        # (design spec §3): a caller must pass BOTH, so even a mis-mapped scope can't
        # widen access -- the original role gate still denies. Loopback root ALWAYS
        # bypasses (root is never scope-limited, see auth.ALL_SCOPES); everyone else is
        # 403'd when `scope` isn't in `request.state.scopes` (set by the access_for()
        # call in loopback_or_authed below -- a NULL Device.scopes resolves to the
        # role's DEFAULT_SCOPES there, so a pre-Wavr-Pass token's very first request
        # after upgrade is allowed/denied IDENTICALLY to before this feature existed).
        def _dep(request: Request):
            if getattr(request.state, "role", None) == "root":
                return
            if not has_scope(getattr(request.state, "scopes", None), scope):
                raise HTTPException(status_code=403, detail=f"missing scope: {scope}")
        return _dep

    if cfg.multidevice:
        app.include_router(build_pair_router(_devices, _pairing))
        # ws-ticket unlocks /ws/live, which streams per-person x/y + vitals -- the
        # most sensitive live-only class (ADR-0002). It MUST carry the same scope
        # its stream does: without this the router had NO dependency (unlike the
        # devices router below), so an 'agent' device -- whose only intended surface
        # is /mcp -- could mint a ticket and open the stream, exactly the data the
        # scope-gated /api/state already denies it. presence:read is the one gate;
        # 'agent' lacks it, 'user'/'central'/root have it. (2026-07-16)
        app.include_router(
            build_ws_ticket_router(_devices, _pairing),
            dependencies=[Depends(require_scope("presence:read"))])
        app.include_router(
            build_devices_router(_devices, delete_deps=[Depends(require_csrf_root)]),
            dependencies=[Depends(require_central), Depends(require_scope("admin"))])

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
            # Wavr Pass: root is never scope-limited (require_scope bypasses it before
            # ever looking at this value) -- None mirrors auth.access_for's own
            # loopback return, so the "root has no explicit scopes to read" invariant
            # holds everywhere, not just through access_for.
            request.state.scopes = None
            # Wavr Pass (Phase 2A / B4): root is never tool-scope-limited either --
            # None mirrors auth.effective_tool_scopes's "not restricted by this axis"
            # return for every non-agent role.
            request.state.tool_scopes = None
            return await call_next(request)
        if not cfg.multidevice:                      # off: strict loopback-only, as before
            return JSONResponse({"detail": "loopback only"}, status_code=403)
        # Onboarding: /api/pair is reachable by an in-subnet peer WITHOUT a token
        # (that is the point of pairing; bounded by the one-time, rate-limited code).
        # The ONE peer entry point (/api/peers/redeem) gets the IDENTICAL in-subnet-
        # bounded exemption: a remote peer must reach it before it holds any token,
        # exactly like /api/pair, and it is bounded by the same one-time ~2-min pairing-
        # code window (now minted ONLY on a trusted loopback screen -- /api/peers/exchange,
        # which network-vended a code, is DELETED, closing C1). /api/peers/link-back is
        # AUTHENTICATED (require_central) so it is NOT exempt here. When peers are disabled
        # these routes simply don't exist (-> 404), so the check is inert unless mounted.
        # Sensor nodes (design 2026-07-11): the same in-subnet-bounded exemption as
        # /api/pair / /api/peers/redeem, extended to the four NODE data-plane paths.
        # Unlike a device/peer, a node never holds a DeviceStore token -- it carries a
        # NODE bearer token that /api/nodes/enroll issues and the ingest routes
        # self-verify IN-HANDLER (wavr.api_nodes._auth_node -> NodeStore.get_by_token),
        # so this middleware only needs to let an in-subnet caller reach the handler,
        # never to authenticate it. The ADMIN routes (/api/nodes, /api/nodes/enroll-code,
        # /api/nodes/{id}/disable, DELETE /api/nodes/{id}) are deliberately NOT in this
        # tuple -- they stay loopback-root-only via admin_deps below.
        # "Approve on the Core" (design 2026-07-11): the IDENTICAL in-subnet-bounded
        # exemption as /api/pair, extended to the two companion-facing pair-request
        # paths -- a companion has to reach these before it holds any token. Neither
        # route mints anything (create() only opens a PENDING record; poll() only ever
        # returns a token after a loopback-root Approve), and request_id/token travel
        # in the body, never here in the URL. The admin surface (list/approve/deny at
        # /api/pending-pairings) is deliberately NOT in this tuple -- it stays
        # loopback-root-only via admin_deps below, same as the node/peer admin routes.
        if request.url.path in (
            "/api/pair", "/api/peers/redeem",
            "/api/nodes/enroll", "/api/nodes/telemetry",
            "/api/nodes/heartbeat", "/api/nodes/reactivate",
            "/api/pair-request", "/api/pair-request/status",
        ):
            if in_subnet(host, _local_ip):
                request.state.role = None
                request.state.scopes = None
                request.state.tool_scopes = None
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
                request.state.scopes = None
                request.state.tool_scopes = None
                return await call_next(request)
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        token = parse_bearer(request.headers.get("authorization"))
        # Wavr Pass: access_for_scoped is access_for's three-way sibling (Phase 2A /
        # B4) -- it ALSO resolves the caller's effective scopes (auth.effective_
        # scopes) AND, new here, its MCP tool-name allow-list (auth.effective_
        # tool_scopes) in the SAME one-verify pass. A NULL Device.scopes (every
        # pre-existing/default-paired device) resolves to its role's DEFAULT_SCOPES,
        # so `role`/`scopes` are byte-identical to the old access_for()-only
        # decision for every already-paired device; `tool_scopes` is a brand-new
        # third value that resolves to None (unrestricted) for every role except
        # the new 'agent' principal, so this is additive-only for root/central/user.
        role, scopes, tool_scopes = access_for_scoped(host, _local_ip, token, _devices)
        if role is None:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        request.state.role = role
        request.state.scopes = scopes
        request.state.tool_scopes = tool_scopes
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

    # ADR-0008 Slice 1: register the READ-ONLY MCP-over-HTTP route at exactly /mcp (a Route,
    # not a Mount -> no trailing-slash redirect). It sits behind loopback_or_authed +
    # TrustedHostMiddleware like every other route; the guard adds the mcp-http kill-switch +
    # Origin + rate-limit before dispatch. Only present under multidevice with the [mcp] extra.
    if _mcp_http_route is not None:
        app.router.routes.append(_mcp_http_route)

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

    def require_authenticated(request: Request):
        # Like require_local's CSRF rule for loopback "root", but WITHOUT the
        # central-only role restriction: a plain 'user'-role paired companion (the
        # owner's own phone, not a central) must be able to self-register its OWN
        # presence / verify the panel PIN -- can_view allows root/central/user (the
        # same set the global middleware already resolved every non-exempt request
        # to; a denied role never reaches here at all). An authenticated LAN peer
        # already proved possession of a bearer token, so -- same as require_local --
        # it needs no CSRF header; only the loopback root (cookie/session-adjacent,
        # no token) does.
        role = getattr(request.state, "role", None)
        if role == "root":
            if request.headers.get("x-wavr-local") != "1":
                raise HTTPException(status_code=403, detail="missing X-Wavr-Local header")
            return
        if not can_view(role):
            raise HTTPException(status_code=403, detail="authentication required")

    # PUT /api/inventory/name is state-changing (Feature A) -- gated by the same
    # require_local rule as the camera/system/pair-code routes, so registration
    # happens here (after require_local is defined) rather than up near the
    # other include_router calls.
    app.include_router(build_inventory_router(
        _inventory, device_meta=_device_meta,
        name_deps=[Depends(require_local), Depends(require_scope("control"))],
        dhcp_monitor=_dhcp_monitor, gateway_monitor=_gateway_monitor,
        known_store=_known_store, intrusion_log=_intrusion, fall_log=_fall,
        intrusion_house_loud=cfg.watch_intrusion_loud),
        dependencies=[Depends(require_scope("network:read"))])

    # Peer pairing (Phase 1, C1-fix reshape). Mounted here -- AFTER require_local/
    # require_root/require_central are all in scope. Two routers with DIFFERENT gates:
    #  * public (redeem): NO deps -- the middleware exempts it in-subnet, the same
    #    deliberately-unauthenticated onboarding surface as /api/pair.
    #  * admin (discovered/observe/confirm/list/unpair): LOOPBACK-ROOT ONLY --
    #    require_local (root's X-Wavr-Local CSRF header) + require_root (rejects any
    #    non-root, incl. an authenticated central PEER). This mirrors the ARP-block
    #    route (§B): only the LOCAL operator initiates/administers pairing. Plain
    #    require_local would admit a remote central peer (its DEFAULT_SCOPES include
    #    admin), letting it force outbound dials / enumerate / sever mesh links --
    #    exactly the primitive require_root exists to deny.
    #  * /link-back: the ONLY peer-reachable route -- require_central. Called by a
    #    REMOTE peer that JUST authenticated as central with the token this instance
    #    issued it; it needs no X-Wavr-Local header (require_central admits root-or-
    #    central header-independently), which is exactly the reverse-leg caller.
    if cfg.peers_enabled:
        app.include_router(build_peers_public_router(
            _peer_store, _pairing, cfg))
        app.include_router(build_peers_admin_router(
            _peer_store, _pairing, _devices, cfg, cfg.instance_name,
            self_base_url=f"https://{_local_ip}:{cfg.port}", local_ip=_local_ip,
            admin_deps=[Depends(require_local), Depends(require_root)],
            linkback_deps=[Depends(require_central)]))

    # Sensor nodes (design 2026-07-11). Three routers, three DIFFERENT auth boundaries
    # (mirrors the peer-pairing split just above):
    #  * public (enroll): NO deps -- the middleware exempts it in-subnet, same
    #    deliberately-unauthenticated onboarding surface as /api/pair.
    #  * ingest (telemetry/heartbeat/reactivate): NO deps here either -- also
    #    middleware-exempted in-subnet, but every route self-verifies the NODE bearer
    #    token IN-HANDLER (see wavr.api_nodes._auth_node). `_ingest` is the app's
    #    existing async fusion callback -- the SAME seam SourceManager feeds, so a node
    #    frame enters fusion by exactly the path a local source does. The kill-switch
    #    (node.state != active) is enforced at ingest + in node_event, NOT here.
    #  * admin (enroll-code/list/disable/revoke): LOOPBACK-ROOT ONLY -- require_local
    #    (root's X-Wavr-Local CSRF header) + require_root, same as the peers admin
    #    router. There is deliberately NO enable route anywhere (remote-OFF-never-ON).
    if cfg.nodes_enabled:
        app.include_router(build_nodes_public_router(_node_store, _node_enroller))
        app.include_router(build_nodes_ingest_router(_node_store, _ingest))
        app.include_router(build_nodes_admin_router(
            _node_store, _node_enroller,
            admin_deps=[Depends(require_local), Depends(require_root)]))

    # Consent-first identity registry routes. Router-level require_central keeps the
    # person-labelled PII list off a multidevice 'user' (loopback root always passes);
    # the state-changing POST/DELETE additionally carry require_local (CSRF + central-
    # or-root), same gates as the camera + device-management routes. ensure_source
    # brings the BLE source up live when the first BLE device is registered.
    app.include_router(
        build_identity_router(
            _identity_store, bonded_reader=_bonded_reader,
            ensure_source=_ensure_ble_source,
            write_deps=[Depends(require_local), Depends(require_scope("control"))],
            casa_state_provider=lambda: latest.get("casa"),
            device_meta=_device_meta,
            known_store=_known_store, net_service=_inventory),
        dependencies=[Depends(require_central), Depends(require_scope("admin"))])
    # Routines: household config, so router-level control (root+central; never an 'agent'
    # or a plain 'user') + per-write require_local CSRF. The /test button actuates a real
    # device, so it counts as a write. Mounted unconditionally -- a single loopback Core
    # (no multidevice) manages routines too.
    app.include_router(
        build_routines_router(
            _routine_store, run_test=_run_routine_test, ha_entities_fn=_ha_entities,
            write_deps=[Depends(require_local)]),
        dependencies=[Depends(require_scope("control"))])

    def _connector_catalog() -> list[dict]:
        # The built-in connectors surfaced from EXISTING gated features. available/env
        # state are COMPUTED LIVE from cfg (never seeded into the DB). The registry is
        # the permission BROKER: the effective GATE is the persisted admin override when
        # present (a deliberate loopback-admin action), else the env flag decides
        # (effective_active). A row can DISABLE an env-on feature (kill-switch) OR ENABLE
        # an env-off one -- but only a DELIBERATE POST enable writes a row, so an empty
        # registry is byte-identical to today.
        #  * `override` -- "on"/"off"/None: the persisted admin toggle (drives the UI switch).
        #  * `env_active` -- is the env flag alone making it on (independent of override).
        #  * `active` -- the HONEST truth: gate-on AND actually able to run right now. An
        #    enabled-but-unconfigured connector is gate-on but NOT active (egresses nothing).
        #  * `needs` -- None | "restart" | "config": when the admin turned it on but it is
        #    not live yet, WHY -- so the UI says so honestly instead of faking "live".
        #  * enforcement='registry-overlay' -- the UI toggle has real teeth (enable/disable).
        #  * enforcement='env' -- the gate is bound inside the SEPARATE MCP server process
        #    (ADR-0005); an in-app override cannot reach it, so the card REFLECTS the flag +
        #    names the env var to edit rather than faking a switch (TRANSPARENT).
        narr_provider = cfg.narrate_provider
        narr_available = provider_configured(cfg)
        narr_env = cfg.narrate_enabled and narr_available
        narr_scope = ("local, zero egress" if narr_provider == "ollama"
                      else f"outbound-cloud: {narr_provider}")
        ha_configured = bool(cfg.ha_url and cfg.ha_token)
        haimp_env = cfg.ha_import and ha_configured
        hactl_env = cfg.mcp_control and ha_configured

        # Narrator: gate = env-or-override; live only once the provider client is built
        # (that happens at startup, so an override needs a hub restart to take effect).
        narr_gate = _connectors.effective_active("narrator", narr_env)
        narr_active = narr_gate and _narrator is not None
        narr_needs = (None if (not narr_gate or narr_active)
                      else ("config" if not narr_available else "restart"))
        # HA import: gate = env-or-override; live as soon as HA creds are present (the
        # fetch is per-request, so NO restart is needed once configured).
        haimp_gate = _connectors.effective_active("ha-import", haimp_env)
        haimp_active = haimp_gate and ha_configured
        haimp_needs = "config" if (haimp_gate and not haimp_active) else None
        # MCP-over-HTTP: no env flag; the registry IS the gate (per-request is_enabled via
        # effective_active). Live only when the mount is wired (multidevice ON + [mcp]),
        # so an override without the mount needs a hub restart with multidevice enabled.
        mcph_gate = _connectors.effective_active("mcp-http", False)
        mcph_active = mcph_gate and _mcp_http_route is not None
        mcph_needs = "restart" if (mcph_gate and not mcph_active) else None

        return [
            {"id": "narrator", "kind": "builtin", "direction": "outbound",
             "label": "LLM Narrator", "available": narr_available,
             "active": narr_active, "suppressed": _connectors.is_suppressed("narrator"),
             "override": _connectors.override("narrator"), "env_active": narr_env,
             "needs": narr_needs,
             "enforcement": "registry-overlay", "scope": narr_scope,
             "env_flag": "WAVR_NARRATE_ENABLED"},
            {"id": "ha-import", "kind": "builtin", "direction": "inbound",
             "label": "Home Assistant Import", "available": ha_configured,
             "active": haimp_active, "suppressed": _connectors.is_suppressed("ha-import"),
             "override": _connectors.override("ha-import"), "env_active": haimp_env,
             "needs": haimp_needs,
             "enforcement": "registry-overlay", "scope": "local HA registry (LAN)",
             "env_flag": "WAVR_HA_IMPORT"},
            {"id": "ha-control", "kind": "builtin", "direction": "outbound",
             "label": "Home Assistant Control", "available": ha_configured,
             "active": hactl_env, "suppressed": False,
             "override": None, "env_active": hactl_env, "needs": None,
             "enforcement": "env", "scope": "outbound-control: local HA (LAN)",
             "env_flag": "WAVR_MCP_CONTROL"},
            {"id": "mcp-read", "kind": "builtin", "direction": "inbound",
             "label": "MCP Server (read-only)", "available": True,
             "active": False, "suppressed": False,
             "override": None, "env_active": False, "needs": None,
             "enforcement": "env", "scope": "read-only RoomState; runs as a separate MCP server",
             "env_flag": None},
            # ADR-0008 Slice 1: the in-app READ-ONLY MCP-over-HTTP listener. Available only
            # when the mount is actually wired (multidevice ON + [mcp] extra). DEFAULT-OFF;
            # registry-overlay so the toggle is a real per-request enable/kill-switch.
            {"id": "mcp-http", "kind": "builtin", "direction": "inbound",
             "label": "MCP Server (HTTP, read-only)",
             "available": _mcp_http_route is not None,
             "active": mcph_active, "suppressed": _connectors.is_suppressed("mcp-http"),
             "override": _connectors.override("mcp-http"), "env_active": False,
             "needs": mcph_needs, "enforcement": "registry-overlay",
             "scope": "read-only over LAN (paired, cert-pinned), in-app /mcp. DEFAULT agent reach = "
                      "coarse current state only: rooms, room context (bare person_count -- no "
                      "identity/geometry/vitals), house map, house status. Network inventory (minimized "
                      "-- vendor/type/ip/make/model only; no name/hostname/timing/open-ports), occupancy "
                      "history (clamped <=24h, room-level, no identity), the alert stream (kind/severity/"
                      "room/ts only) and the HA entity list each require an EXPLICIT per-agent grant. "
                      "Central/root (local dashboard) unrestricted; 'user'-role devices are denied /mcp",
             "env_flag": None},
        ]

    def _connectors_active() -> int:
        # Honest count for the status header badge: live built-ins + enabled generics.
        n = sum(1 for c in _connector_catalog() if c["active"])
        n += sum(1 for r in _connectors.list()
                 if r["kind"] == "generic" and r["enabled"] == 1)
        return n

    # Single egress surface: the ONLY UI that enumerates/toggles connectors. Router-level
    # central/root (GET reads). M1 (2026-07, appsec): the enable/disable WRITE is the
    # egress-CONTROL plane itself -- a paired peer is minted role=central with the full
    # central DEFAULT_SCOPES (incl. "admin"), so plain require_central+control-scope
    # would let a malicious/compromised peer flip ANY connector (incl. the
    # "assistant-cloud" kill switch) remotely. Tightened to LOOPBACK-ROOT ONLY
    # (require_local CSRF + require_root), the same tier as the peers-admin/nodes-admin/
    # ARP-block routes -- only the local operator may change what egresses.
    app.include_router(
        build_connectors_router(
            _connectors, _connector_catalog,
            write_deps=[Depends(require_local), Depends(require_root)]),
        dependencies=[Depends(require_central), Depends(require_scope("admin"))])

    # Wavr Assistant engine picker + bounded ask (Phase 2B). Router-level gate tier
    # matches identity/connectors (central + admin scope) for the READ routes (engines
    # list, ask, log) -- ask additionally carries require_local CSRF + control scope,
    # mirroring /api/narrate, so an authenticated central peer may still ask a bounded
    # question. M1 (2026-07, appsec): POST /api/assistant/engine is a SEPARATE,
    # stricter EGRESS-CONFIG write -- it picks the active engine and, for "manual",
    # persists a base_url the assistant will call PLUS the *name* of an env var it
    # will read as a bearer key. Left at require_central+control, a paired/compromised
    # peer could point the assistant at an attacker host and name a real secret env
    # var, then use /ask (still peer-reachable) to exfiltrate the resolved secret +
    # coarse house state. Tightened via `engine_deps` to LOOPBACK-ROOT ONLY
    # (require_local CSRF + require_root) -- only the local operator may reconfigure
    # which engine/endpoint the assistant calls.
    app.include_router(
        build_assistant_router(
            cfg, _assistant, _connectors, tool_deps=_assistant_tool_deps,
            write_deps=[Depends(require_local), Depends(require_scope("control"))],
            engine_deps=[Depends(require_local), Depends(require_root)]),
        dependencies=[Depends(require_central), Depends(require_scope("admin"))])

    @app.get("/api/history")
    async def history(limit: int = 200, _=Depends(require_scope("presence:read"))):
        # Clamp: a negative limit means "no limit" to SQLite's `LIMIT ?` (full-table
        # dump), and an unbounded positive value is still a resource-exhaustion risk.
        limit = max(1, min(limit, 1000))
        return await asyncio.to_thread(_storage.recent, limit)

    def _project_all():
        # Snapshot projection for PULL egress (/api/state + the narrator). Mirrors the
        # per-event hub projection in _publish so push and pull agree exactly. `latest` is
        # the FULL internal truth; this returns the Watch-suppressed view when Watch is on
        # (byte-identical to `latest` when off). Intrusion gating mirrors _publish exactly.
        watch_on = _watch.on
        gate = watch_on and cfg.identity_enabled
        known = len(known_present_persons(latest.values())) if gate else 0
        return {r: project_state(d, watch_on, bool(gate and room_unrecognized(d, known)))
                for r, d in latest.items()}

    def _watch_status():
        on = _watch.on
        gate = on and cfg.identity_enabled
        known = len(known_present_persons(latest.values())) if gate else 0
        rooms = sorted(r for r, d in latest.items() if room_unrecognized(d, known)) if gate else []
        # House-level aggregate: True when the honest SUM of per-room counts exceeds the
        # known-present count even if NO single room's does (a spread-out intrusion the
        # per-room `unrecognized_rooms` list alone would miss). Count-only + room-agnostic;
        # a fully-uncounted house (house_person_count None) cannot assert it -> False, never
        # a false "all clear". Gated on identity like every other intrusion signal.
        house_unrec = bool(gate and house_unrecognized(house_person_count(latest.values()), known))
        return {
            "on": on,
            "identity_enabled": cfg.identity_enabled,
            # Honest: with the identity layer off Watch still SUPPRESSES geometry, but it
            # cannot tell known from unknown, so intrusion detection is inert (no alerts).
            "intrusion_detection": bool(gate),
            "known_present": known,
            "unrecognized_rooms": rooms,
            "house_unrecognized": house_unrec,
            # The house-level intrusion is surfaced here regardless; intrusion_loud tells
            # the UI whether it ALSO emits a loud /api/alerts alert (WAVR_WATCH_INTRUSION_LOUD).
            "intrusion_loud": cfg.watch_intrusion_loud,
        }

    @app.get("/api/watch")
    async def get_watch(_=Depends(require_scope("presence:read"))):
        return _watch_status()

    @app.post("/api/watch")
    async def set_watch(on: bool = Body(..., embed=True), _=Depends(require_local),
                        __=Depends(require_scope("control"))):
        # Toggling Watch changes what LEAVES the box (a privacy-affecting control), so it
        # carries the same require_local CSRF + control scope as the camera/system toggles.
        _watch.set(on)
        # Evaluate intrusions IMMEDIATELY on the toggle -- do not wait for the next sensing
        # event or the 5s refuse tick -- so enabling Watch fires an edge alert at once for a
        # room already holding an unknown. Turning Watch OFF re-arms the edges (reset) so a
        # still-present intrusion alerts again on the next enable. Gated on identity_enabled:
        # with identity off there is no honest "known" baseline, so no alert is invented.
        if on:
            if cfg.identity_enabled:
                known = len(known_present_persons(latest.values()))
                for r, d in latest.items():
                    hit = _intrusion.record(r, room_unrecognized(d, known),
                                            d.get("person_count"), known, d.get("ts"))
                    if hit is not None and _notify:
                        _notify("Wavr Vigia: pessoa nao reconhecida em " + str(r))
                    if _rules is not None:
                        _rules.handle_intrusion(r, r in _intrusion.active_rooms())
                # House-level aggregate, evaluated on the same immediate toggle so enabling
                # Watch fires the house-level edge at once too (room=None, room-agnostic +
                # count-only). ts defaults to now (there is no single room).
                house_count = house_person_count(latest.values())
                hhit = _intrusion.record(None, house_unrecognized(house_count, known),
                                         house_count, known)
                if hhit is not None and _notify:
                    _notify("Wavr Vigia: pessoa nao reconhecida em casa")
                if _rules is not None:
                    _rules.handle_intrusion(None, None in _intrusion.active_rooms())
        else:
            # Build C4: Watch turning off must clear any retained ON intrusion topic --
            # otherwise a resolved intrusion stays stuck ON on the broker forever (the
            # SAME staleness `wavr.house_status`'s module docstring warns against),
            # since `_publish`'s own intrusion re-evaluation stops running once
            # `_watch.on` is False. Publish OFF for every currently-active scope BEFORE
            # `reset()` drops the edge state.
            if _rules is not None:
                for r in _intrusion.active_rooms():
                    _rules.handle_intrusion(r, False)
            _intrusion.reset()
        return _watch_status()

    @app.get("/api/state")
    async def state(_=Depends(require_scope("presence:read"))):
        # Watch-projected: when Watch is on, family geometry/identity/vitals are stripped
        # here too, so the dashboard is suppressed exactly like every other egress.
        return _project_all()

    @app.get("/api/house")
    async def house(_=Depends(require_scope("presence:read"))):
        return _house

    @app.put("/api/house")
    async def put_house(doc: dict = Body(...), _=Depends(require_local),
                        __=Depends(require_scope("control"))):
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
    async def put_house_room(body: dict = Body(...), _=Depends(require_local),
                             __=Depends(require_scope("control"))):
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
    async def narrate(_=Depends(require_local), __=Depends(require_scope("control"))):
        # Connectors & Services override (REVOCABLE, read per request). Checked FIRST so a
        # deliberate "off" kill-switch revokes even a live narrator immediately, no restart.
        narr_ov = _connectors.override("narrator")
        if narr_ov == "off":
            raise HTTPException(status_code=503,
                                detail="narrator revoked in Connectors screen")
        # system-toggles egress master: local (ollama) narration is zero-egress and
        # stays unaffected; any cloud provider ANDs in the operator's System-tab
        # egress switch (see /api/system/toggles) on top of the narrator's own gate.
        if cfg.narrate_provider != "ollama" and not _connectors.egress_allowed():
            raise HTTPException(status_code=503,
                                detail="egress disabled by operator (System tab)")
        if _narrator is None:
            # Enabled in the Connectors screen but the provider client isn't built yet:
            # be HONEST about what is missing rather than pretend it's live -- and, crucially,
            # still egress NOTHING (we return 503, we do not narrate). An enable without a
            # provider key needs the key; an enable WITH a key needs a hub restart (the
            # client is built at startup, mirroring the WAVR_NARRATE_ENABLED path).
            if narr_ov == "on":
                if not provider_configured(cfg):
                    raise HTTPException(
                        status_code=503,
                        detail=("narrator enabled in Connectors — configure the "
                                f"'{cfg.narrate_provider}' provider (API key), then restart the hub"))
                raise HTTPException(
                    status_code=503,
                    detail="narrator enabled in Connectors — restart the hub to activate")
            # Default (no override): byte-identical to before.
            raise HTTPException(
                status_code=503,
                detail="narration not configured (set WAVR_NARRATE_ENABLED=1 and configure "
                       f"the '{cfg.narrate_provider}' provider)")
        try:
            rows = await asyncio.to_thread(_storage.recent, 50)
            text = await asyncio.to_thread(_narrator.narrate, _project_all(), rows)
        except Exception:
            logging.exception("narrate failed")
            raise HTTPException(status_code=502, detail="narration backend error")
        return {"narration": text}

    @app.post("/api/ha/import")
    async def ha_import(dry_run: bool = Body(False, embed=True),
                        _=Depends(require_local), __=Depends(require_scope("control"))):
        # A4.1 HA -> Wavr registry import. USER-TRIGGERED ONLY (never a timer),
        # gated by require_local (CSRF), local-HA-only + SSRF-safe (wavr.ha_import
        # only ever contacts the configured ha_url). The HA token is read from
        # config here and passed to the transport only -- it is NEVER in the
        # response or any error string below.
        # Connectors & Services override (REVOCABLE, read per request). A deliberate "off"
        # revokes even with WAVR_HA_IMPORT=1 (kill-switch); a deliberate "on" ENABLES the
        # import when the env flag is unset. Both survive restart; absent row => the env
        # flag alone decides => byte-identical to before. Unlike the narrator this needs
        # NO restart to go live once HA creds are configured (the fetch is per-request).
        ha_ov = _connectors.override("ha-import")
        if ha_ov == "off":
            raise HTTPException(status_code=403,
                                detail="HA import revoked in Connectors screen")
        if not (cfg.ha_import or ha_ov == "on"):
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
                          _=Depends(require_local), __=Depends(require_scope("control"))):
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
                   _=Depends(require_local), __=Depends(require_scope("control"))):
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
                            _=Depends(require_local), __=Depends(require_scope("control"))):
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
        # system-toggles egress master: ANDs on top of the WAVR_NET_SPEEDTEST opt-in
        # (see /api/system/toggles) -- an operator-level block even when speedtest
        # itself stays enabled.
        if not _connectors.egress_allowed():
            raise HTTPException(status_code=503,
                                detail="egress disabled by operator (System tab)")
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
    async def speedtest_info(_=Depends(require_scope("control"))):
        # control, mirroring its own POST /api/speedtest sibling (same egress-
        # diagnostic domain). No UI consumer below central, so gating here costs
        # nothing and keeps 'agent' out of the egress posture.
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

    def _hub_level() -> str:
        # Server mirror of index.html's deriveTier() (frontend/index.html's own
        # TIER_ORDER/TIER_META region): "off" if the system isn't running,
        # "precise" if any REGISTERED camera source is enabled, else "presence".
        # `_cameras.list()` (not manager.status() alone) is the same "which
        # sources are cameras" cross-reference the frontend's own
        # cameraSourceList() does against GET /api/cameras.
        st = manager.status()
        if not st["running"]:
            return "off"
        cam_names = {c["name"] for c in _cameras.list()}
        any_cam_enabled = any(s["enabled"] for s in st["sources"] if s["name"] in cam_names)
        return "precise" if any_cam_enabled else "presence"

    @app.get("/api/status")
    async def status(_=Depends(require_scope("presence:read"))):
        # READ-ONLY, NO SECRETS: sources are name+active only (no rtsp/mac), features
        # are opt-in booleans only (no urls/tokens). Gated on presence:read (the same
        # scope /api/state carries) because house.people below is a live occupancy
        # count -- the identical data class /api/state already denies 'agent', which
        # this route was silently leaking to it. root always bypasses require_scope;
        # 'user'/'central' carry presence:read by default, so this is additive-only
        # for every existing consumer (the loopback dashboard + companion viewers
        # both hold it; no frontend path reads house.people at all). (2026-07-16)
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
                # network-doctor auto-fix (WAVR_NET_DOCTOR_AUTOFIX) -- opt-in,
                # default OFF, two-factor with the route's own auto_fix=true
                # query param (see GET /api/health/doctor). Surfaced here so
                # the Privacy & Egress view stays honest that an auto-fix path
                # exists at all (diagnose-only is the default install).
                "net_doctor_autofix": cfg.net_doctor_autofix,
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
                # A4 house memory (wavr.occupancy_log) -- ON by default (derived-only,
                # zero egress, same disclosure class as the existing room_states table);
                # surfaced so the Privacy & Egress view honestly shows a local history is
                # being kept and can be turned off (WAVR_OCCUPANCY_LOG=0).
                "occupancy_log": _occupancy_log is not None,
                # Watch/Guard ("Vigia") -- in-memory toggle, default OFF. Surfaced so the
                # Privacy & Egress view honestly shows when family geometry is being
                # suppressed and only counts + intrusion room leave.
                "watch": _watch.on,
                # A5.1 hardening posture, surfaced honestly (bool-only, never the
                # secret). `api_token`: a same-machine shared secret (WAVR_LOCAL_TOKEN)
                # is REQUIRED on /api/* even on loopback. `health_gate`: F6 -- the
                # side-effecting GET /api/health now requires the X-Wavr-Local CSRF
                # header, so a drive-by tab can't fire its public-DNS egress (always on).
                "api_token": bool(_local_token),
                "health_gate": True,
                # Connectors & Services: the count of connectors that are actually
                # ACTIVE (live built-ins + enabled generics). A non-egress header
                # badge; the per-connector state/scope lives on GET /api/connectors.
                "connectors_active": _connectors_active(),
                # ADR-0008 Slice 1: honest disclosure of the in-app READ-ONLY
                # MCP-over-HTTP inbound listener -- true only when it is wired
                # (multidevice + [mcp]) AND enabled in the Connectors screen.
                "mcp_http": bool(_mcp_http_route is not None
                                 and _connectors.is_enabled("mcp-http")),
                # Phase-2B re-threat FIX 3 (UX HIGH #1 backend half): the Wavr
                # Assistant's cloud-egress kill switch ("assistant-cloud", upserted
                # above) was previously enumerable ONLY via GET /api/connectors --
                # this trust receipt (GET /api/status.features, what EGRESS_ITEMS
                # in the frontend reads) had no first-class fact for it at all, so
                # it could never be listed there even though it is a genuine
                # egress path (a cloud-classified assistant engine, gated by this
                # same switch -- see engine_catalog's cloud_gate_on). Bool-only,
                # same shape as every other row here: True iff the kill switch is
                # ON, i.e. a cloud-classified engine is currently PERMITTED to
                # answer (an engine still has to be selected + configured for an
                # actual call to happen -- this flag mirrors mcp_http's own
                # "wired AND enabled" honesty, not "a call just happened").
                "assistant_cloud": _connectors.is_enabled("assistant-cloud"),
                # system-toggles: the two System-tab master switches (see
                # GET/POST /api/system/toggles). True = allowed (default,
                # byte-identical to before this feature); False = the operator
                # deliberately blocked that whole class from the System tab.
                "egress_allowed": _connectors.egress_allowed(),
                "sensing_allowed": _connectors.sensing_allowed(),
                # Mobile companion reconciliation (2026-07-11): the server-side
                # mirror of index.html's own deriveTier() (off/presence/precise),
                # so a companion's #sensingLevelTile can show "your home is
                # sensing: <hub_level>" from ONE source of truth instead of
                # re-deriving the same rule twice. off = system not running;
                # precise = at least one registered camera source is enabled;
                # presence = running with no camera enabled.
                "hub_level": _hub_level(),
            },
            "house": {
                "floors": len(_house.get("floors", [])),
                "rooms": len(room_names(_house)),
                # Live house-level person count (additive): sum of per-room person_count
                # where a counting-capable source vouches for a number; None = unknown.
                # The LEAST personal datum (a bare integer, no geometry/identity).
                "people": house_person_count(latest.values()),
            },
            # Feature B: current internet/gateway reachability. Null/null when
            # the monitor is off (or hasn't completed its first check yet).
            "internet": _internet.status() if _internet else {"ok": None, "since": None},
            # Panel-review finding #9/#17: honest unavailable-by-environment
            # signal for the two privileged-bind collectors (dhcp_fp binds
            # UDP/67, rogue_dhcp's DHCPCollector binds UDP/68) -- DISTINCT
            # from a source that is merely off/paused or crashed at runtime.
            # {"available": bool|None, "reason": str|None} per collector;
            # None/None means off or no cycle has run yet (identical to
            # today's silent behavior -- purely additive, no existing key
            # changes shape). available=False only when the raw socket bind
            # itself failed (PermissionError/OSError), e.g. a non-root
            # proot/container lacking CAP_NET_BIND_SERVICE.
            "availability": {
                "dhcp_fp": _inventory.dhcp_fp_status(),
                "rogue_dhcp": (
                    {"available": getattr(_dhcp_monitor, "available", None),
                     "reason": getattr(_dhcp_monitor, "unavailable_reason", None)}
                    if _dhcp_monitor else {"available": None, "reason": None}
                ),
            },
        }

    # System toggles (feature "system-toggles"): the two System-tab master
    # switches (Egress / Network sensing) the receipt-only #egressList/
    # #sensingList cards used to only DESCRIBE ("needs a hub restart -- no
    # in-app switch yet"). Persisted as reserved rows (kind='system') in the
    # SAME ConnectorStore that already backs Connectors & Services --
    # kind='system' rows are excluded from both api_connectors.py's list
    # (`kind == 'generic'` only) and its enable route (`row["kind"] !=
    # "generic"` -> 404), so a paired central peer holding that router's
    # weaker require_local+control tier can never reach these ids there.
    # Default-ABSENT => egress_allowed()/sensing_allowed() => True =>
    # byte-identical to today until an operator deliberately flips one. Write
    # gate is the SAME tier as the ARP-block/nodes-admin primitives
    # (require_local CSRF + require_root): loopback-operator only, a paired
    # central peer 403s (M1).
    _SYS_TOGGLES = {"egress": "sys:egress", "network_sensing": "sys:sensing"}

    def _sys_toggles_state() -> dict:
        return {"egress": _connectors.egress_allowed(),
                "network_sensing": _connectors.sensing_allowed()}

    @app.get("/api/system/toggles")
    async def system_toggles(_=Depends(require_scope("control"))):
        # control: the egress/sensing kill-switch posture, one tier below its
        # root-only write sibling. 'agent' must not read the household's feature
        # posture. root+central pass.
        return _sys_toggles_state()

    @app.post("/api/system/toggles/{name}")
    async def set_system_toggle(name: str, enabled: bool = Body(..., embed=True),
                                _=Depends(require_local), __=Depends(require_root)):
        key = _SYS_TOGGLES.get(name)
        if key is None:
            raise HTTPException(status_code=404, detail=f"unknown toggle: {name}")
        # upsert() preserves `enabled` on conflict (never silently re-arms a
        # kill-switch) -- the row starts at enabled=0 only on first insert, then
        # set_enabled immediately writes the actual requested value.
        _connectors.upsert(key, "system", name)
        _connectors.set_enabled(key, enabled)
        return _sys_toggles_state()

    @app.get("/api/presence/report")
    async def presence_report(_=Depends(require_scope("network:read"))):
        # Pure aggregation of wavr.device_meta's first/last-seen store (Feature
        # A) -- no new scanning, no I/O beyond the existing sqlite read (same
        # synchronous-call convention netinventory_service already uses for
        # this same store). Safe to call on every GET.
        return build_report(_device_meta)

    # A4 house memory (wavr.occupancy_log): DERIVED-ONLY read APIs over the append-only
    # per-room log. Same scope as /api/history and /api/state (presence:read) -- this is
    # the same class of coarse occupancy data, just over time instead of "now". 503 (not
    # 404/empty) when WAVR_OCCUPANCY_LOG=0, mirroring the WoL/PTZ opt-in-feature pattern
    # above, so a caller can tell "disabled" apart from "no data yet".
    def _require_occupancy_log() -> OccupancyLog:
        if _occupancy_log is None:
            raise HTTPException(status_code=503,
                                detail="Occupancy history disabled (set WAVR_OCCUPANCY_LOG=1)")
        return _occupancy_log

    @app.get("/api/occupancy/history")
    async def occupancy_history(room: str | None = None, start: str | None = None,
                                end: str | None = None, limit: int = 500,
                                _=Depends(require_scope("presence:read"))):
        log = _require_occupancy_log()
        return await asyncio.to_thread(log.timeline, room, start=start, end=end, limit=limit)

    @app.get("/api/occupancy/routine")
    async def occupancy_routine(room: str, weeks: float = 4.0,
                                _=Depends(require_scope("presence:read"))):
        log = _require_occupancy_log()
        return await asyncio.to_thread(log.routine, room, weeks=weeks)

    @app.get("/api/occupancy/unusual")
    async def occupancy_unusual(room: str, weeks: float = 4.0,
                                _=Depends(require_scope("presence:read"))):
        log = _require_occupancy_log()
        # Compares the room's CURRENT live occupied reading (the same `latest` seam
        # /api/state serves) against its own routine baseline -- never a second source
        # of truth for "is it occupied right now".
        current = latest.get(room)
        if current is None:
            raise HTTPException(status_code=404, detail=f"unknown room: {room!r}")
        return await asyncio.to_thread(log.is_unusual, room, current["occupied"], weeks=weeks)

    @app.get("/api/house-status")
    async def house_status(window_minutes: float = DEFAULT_NETWORK_WINDOW_MINUTES,
                           _=Depends(require_scope("presence:read"))):
        # `window_minutes` overrides the default network-alert recency window (same
        # override-friendly convention as /api/occupancy/routine's `weeks`). The
        # composition itself lives in `_compute_house_status` above, shared byte-for-
        # byte with the get_house_status MCP tool (wavr.mcp) -- see its docstring.
        return await _compute_house_status(window_minutes)

    def _caller_consent(request: Request) -> str:
        # Resolve the REQUESTING device's own consent tri-color (devices.
        # VALID_CONSENT). Root (the loopback operator) has no Device row -- its
        # participation is the master egress/sensing toggle
        # (POST /api/system/toggle), not this per-device axis, so it always
        # reads "green" (full), byte-identical to this route's pre-consent
        # behaviour. Everything else re-verifies the SAME bearer token the
        # loopback_or_authed middleware already checked (one more hashed
        # lookup) because request.state carries only role/scopes, never the
        # specific Device row this needs.
        if getattr(request.state, "role", None) == "root" or _devices is None:
            return "green"
        token = parse_bearer(request.headers.get("authorization"))
        device = _devices.verify(token) if token else None
        return (device.consent if device else None) or "green"

    @app.post("/api/presence/register-companion")
    async def register_companion(request: Request, label: str = Body(..., embed=True),
                                 _=Depends(require_authenticated),
                                 __=Depends(require_scope("presence:write"))):
        # Companion presence self-registration: the caller's OWN device becomes a
        # named presence signal. The MAC is NEVER client-supplied -- it is derived
        # from the REQUEST'S OWN source IP via the local ARP table (the same seam
        # wavr.sources.network / wavr.netinventory use), so a companion can only
        # ever register the device it is actually calling FROM, never an arbitrary
        # MAC. Persisted into the SAME consent-first registry the admin identity
        # routes use (IdentityStore, origin='companion') -- its as_net_map() is
        # already merged into NetworkSource's live known-device provider
        # (_net_known_provider above), so this takes effect on the network
        # source's very next scan cycle, no restart, and survives one. Only a
        # MAC PREFIX is ever returned (never the full address). 200 (never
        # 4xx/5xx) on a failed resolution -- "Core has no ARP access" is an
        # honest, expected outcome (not rooted / IP not yet in the table), not
        # an error, so the mobile side can show a clear message rather than a
        # generic failure.
        #
        # CONSENT ENFORCEMENT (Augusto sign-off, mobile-as-presence-beacon):
        # "red" was previously a client-side-only promise (the shim just never
        # called this route) -- a patched/compromised client could keep
        # registering after withdrawal. It is now enforced HERE too, so RED is
        # a real server-side guarantee, not just a UI state.
        #
        # This route is only HALF the guarantee, and on its own it was the wrong
        # half (fixed 2026-07-16): it decides what a level does at WRITE time, but
        # the level is changed LATER by POST /api/consent. The durable enforcement
        # is the registry's own read-time gate (IdentityStore.set_consent_lookup),
        # which re-reads each device's live level on every scan cycle -- so a
        # withdrawal applies to an already-written row without needing the client
        # to send its DELETE. What is written here is the DATA-AT-REST half: green
        # stores a name because the owner asked to be named; yellow stores an
        # ANONYMOUS row -- present, never named -- so the name it declined is not
        # sitting in the DB waiting for a gate to hold.
        consent = _caller_consent(request)
        device = _self_device(request)
        # The row MUST carry a link to whatever owns its consent level, or the
        # read-time gate has nothing to ask and fails closed on it forever. Root
        # legitimately has no Device row (_self_device returns None for it), so it
        # gets the reserved sentinel rather than a NULL that would be
        # indistinguishable from a pre-upgrade row. Never client-supplied: this
        # comes from the bearer token the middleware already verified.
        device_id = device.device_id if device else (
            ROOT_DEVICE_ID if getattr(request.state, "role", None) == "root" else None)
        if consent == "red":
            return {"mac_registered": False, "reason": "consent-withdrawn"}
        try:
            who = sanitize_name(label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        host = request.client.host if request.client else None
        mac = await _resolve_companion_mac(host) if host else None
        if not mac:
            return {"mac_registered": False, "reason": "no-arp-resolution"}
        if consent == "yellow":
            # Presence WITHOUT the name: an ANONYMOUS row. This used to skip the
            # write entirely, on the reasoning that the write is what attaches a
            # name -- but the write is ALSO what makes the MAC known, and only a
            # known MAC counts (sources/network.py: `known & seen`). So yellow
            # delivered no name AND no presence: byte-identical to red, while the
            # UI promised "counted as home, without a name". The row is the
            # presence half; ANONYMOUS is the withheld-name half.
            _identity_store.add_anonymous(mac, source="network", origin="companion",
                                          device_id=device_id)
            return {"mac_registered": True, "label": None, "mac_prefix": mac_prefix(mac)}
        _identity_store.add(mac, who, source="network", origin="companion",
                            device_id=device_id)
        return {"mac_registered": True, "label": who, "mac_prefix": mac_prefix(mac)}

    @app.delete("/api/presence/register-companion")
    async def unregister_companion(request: Request, _=Depends(require_authenticated),
                                   __=Depends(require_scope("presence:write"))):
        # Self-service opt-out: resolve the CALLER's own source IP the same way
        # the POST does (never a client-supplied MAC/label) and remove that
        # address from the registry -- "this MAC is me, stop tracking it" is one
        # consistent un-registration regardless of whether the row came from this
        # self-add path or an earlier admin add for the same device.
        host = request.client.host if request.client else None
        mac = await _resolve_companion_mac(host) if host else None
        if not mac:
            return {"mac_unregistered": False, "reason": "no-arp-resolution"}
        removed = _identity_store.delete(mac)
        return {"mac_unregistered": removed, "mac_prefix": mac_prefix(mac)}

    def _self_device(request: Request):
        """Resolve the caller's OWN Device row from its bearer token, or None.
        Root has no row (handled by each caller separately -- some routes 409 on
        root, others treat it as a distinct case). Shared by
        GET/POST /api/consent + GET /api/devices/me so all three self-resolve
        identically (never a body/query device_id -- a device can only ever act
        on itself)."""
        if _devices is None:
            return None
        token = parse_bearer(request.headers.get("authorization"))
        return _devices.verify(token) if token else None

    @app.get("/api/consent")
    async def get_consent(request: Request, _=Depends(require_authenticated),
                          __=Depends(require_scope("presence:read"))):
        # Closes the dangling call the shim has made since day one (wavr-mobile-
        # shim.js's postConsent 404s until this exists) -- self-resolved from the
        # caller's OWN bearer token, never a body device_id. Root has no
        # per-device consent row (it's the box itself); its equivalent lever is
        # the hub-wide POST /api/system/toggle, so this 409s rather than
        # fabricating a value -- tested so the loopback dashboard (which never
        # calls this route) can't accidentally crash if it ever did.
        if getattr(request.state, "role", None) == "root":
            raise HTTPException(status_code=409, detail="use /api/system/toggle")
        device = _self_device(request)
        if device is None:
            raise HTTPException(status_code=403, detail="invalid or revoked token")
        return {"device_id": device.device_id, "level": device.consent or "green"}

    @app.post("/api/consent")
    async def set_consent(request: Request, level: str = Body(..., embed=True),
                          _=Depends(require_authenticated),
                          __=Depends(require_scope("presence:write"))):
        # The write half of the same self-resolved axis. 200 {device_id, level}
        # is what the shim's postConsent already expects on success (it treats
        # anything else as a retry, never a re-pair).
        #
        # Writing the column IS the enforcement, and nothing here needs to mirror
        # into the identity registry: the registry reads this level LIVE on every
        # scan cycle via _consent_of (IdentityStore.set_consent_lookup), keyed by
        # the device_id its rows carry. So a withdrawal takes effect on the next
        # cycle for rows written BEFORE it -- no restart, and no dependence on the
        # client sending its DELETE (an offline or patched one never does).
        # (This comment previously claimed a mirror that did not exist; the levels
        # were in fact only ever applied at register-companion's write. 2026-07-16.)
        if getattr(request.state, "role", None) == "root":
            raise HTTPException(status_code=409, detail="use /api/system/toggle")
        if level not in VALID_CONSENT:
            raise HTTPException(status_code=422,
                                detail=f"invalid consent level: {level!r} (expected one of "
                                       f"{sorted(VALID_CONSENT)})")
        device = _self_device(request)
        if device is None:
            raise HTTPException(status_code=403, detail="invalid or revoked token")
        _devices.set_consent(device.device_id, level)
        return {"device_id": device.device_id, "level": level}

    @app.get("/api/devices/me")
    async def devices_me(request: Request, _=Depends(require_authenticated)):
        # Read-back of the caller's OWN role/name -- lets a paired companion
        # show "Admin device" / "Member device" without the 403-inference hack
        # the shim's detectRole() previously had to do. can_view's role set
        # (root/central/user) is exactly what require_authenticated already
        # gates, so no extra scope needed beyond authentication itself.
        role = getattr(request.state, "role", None)
        if role == "root":
            return {"device_id": None, "role": "root", "name": None}
        device = _self_device(request)
        if device is None:
            raise HTTPException(status_code=403, detail="invalid or revoked token")
        return {"device_id": device.device_id, "role": device.role, "name": device.name}

    @app.get("/api/companion/health")
    async def companion_health(request: Request, _=Depends(require_authenticated),
                               __=Depends(require_scope("presence:read"))):
        # Device+network health check for a companion (item 6): deliberately NOT
        # a reuse of GET /api/health (control-scope, active public-DNS-resolver
        # egress -- wrong trust tier for a plain 'user'). Every field here is a
        # PASSIVE self-report of state this process already holds -- zero new
        # I/O, zero egress, no ping/DNS. `my_presence_registered` resolves the
        # caller's own mac the SAME server-side-ARP way register_companion does
        # and answers ONE question honestly: "is my device contributing presence
        # right now?" -- so it reads the consent-gated presence map, not a raw row
        # lookup. green -> True (counted, named), yellow -> True (counted, never
        # named -- it IS contributing), red -> False (contributing nothing).
        #
        # It used to call _identity_store.get(), which is ungated, and its comment
        # claimed "true only at green -- yellow/red honestly read false". Both
        # halves were wrong once consent became read-time: a device that withdrew
        # to red still had its row, so its OWN screen told its owner presence was
        # registered while the hub counted it for nothing. A withdrawal screen must
        # never be the last place the withdrawal is believed. (2026-07-16)
        st = manager.status()
        active_count = sum(1 for s in st["sources"] if s["active"])
        last_frame_age_s = None
        ts_values = [d["ts"] for d in latest.values() if d.get("ts")]
        if ts_values:
            try:
                newest = max(datetime.fromisoformat(t) for t in ts_values)
                now = datetime.now(newest.tzinfo or timezone.utc)
                last_frame_age_s = max(0.0, (now - newest).total_seconds())
            except ValueError:
                last_frame_age_s = None   # malformed ts -- honestly unknown, never crash
        host = request.client.host if request.client else None
        mac = await _resolve_companion_mac(host) if host else None
        my_presence_registered = bool(mac and mac in _identity_store.as_net_known())
        return {
            "system_running": st["running"],
            "sources_active_count": active_count,
            "core_version": __version__,
            "my_presence_registered": my_presence_registered,
            "ws_clients": _hub.subscriber_count(),
            "last_frame_age_s": last_frame_age_s,
        }

    @app.get("/api/app/manifest")
    async def app_manifest(_=Depends(require_authenticated),
                           __=Depends(require_scope("presence:read"))):
        # OTA (item 9, Augusto sign-off): web-assets-only version pointer. Gated
        # presence:read -- 'agent' (mcp-only scope) can never reach this, exactly
        # like every other presence:read route.
        bundle = _build_ota_bundle()
        return {"version": __version__, "sha256": bundle["sha256"],
                "size": bundle["size"], "url": "/api/app/bundle"}

    @app.get("/api/app/bundle")
    async def app_bundle(_=Depends(require_authenticated),
                         __=Depends(require_scope("presence:read"))):
        # The manifest's `sha256`/`size` are computed over this EXACT byte
        # sequence (same cached build) -- a client that hashes what it downloads
        # will always match. gzip, never a bare tar -- content-encoding-free
        # (media_type IS the encoding here, no extra header needed) so a
        # pinned-TLS native client can size/verify before ever touching disk.
        bundle = _build_ota_bundle()
        return Response(content=bundle["data"], media_type="application/gzip")

    @app.post("/api/core/pin")
    async def set_core_pin(pin: str = Body(..., embed=True), _=Depends(require_local),
                           __=Depends(require_scope("admin"))):
        # Admin-only (require_local: loopback root + CSRF header, or a multidevice
        # 'central'). Sets/replaces the Core Panel unlock PIN -- persisted HASHED
        # (salted pbkdf2, wavr.pin_store) -- never the plaintext, never echoed back.
        if not isinstance(pin, str) or not _PIN_RE.match(pin):
            raise HTTPException(status_code=400,
                                detail="pin must be 4-12 digits")
        _pin_store.set_pin(pin)
        return {"set": True}

    @app.delete("/api/core/pin")
    async def clear_core_pin(_=Depends(require_local), __=Depends(require_scope("admin"))):
        # Remove the Core Panel unlock PIN entirely -- the "no lock" option. Same
        # gate as the setter (loopback root + CSRF header, or a multidevice
        # 'central'): only a local admin can drop the lock. Idempotent.
        _pin_store.clear()
        return {"set": False}

    @app.post("/api/core/pin/verify")
    async def verify_core_pin(pin: str = Body(..., embed=True),
                              _=Depends(require_authenticated)):
        # Reachable by the panel: loopback (CSRF-gated, like every other loopback
        # state check) or any authenticated LAN peer showing the panel -- see
        # require_authenticated. Rate-limited (wavr.pin_ratelimit): checked BEFORE
        # touching the store, so a caller under lockout never reaches the
        # (deliberately slow) pbkdf2 compare. Locked-out or malformed input both
        # degrade to an honest {"ok": false} rather than a distinguishable error
        # code, so a caller learns nothing beyond "not unlocked" either way.
        if _pin_limiter.locked():
            return {"ok": False}
        # Bound the input BEFORE it reaches pbkdf2 (hygiene, not a real amplification
        # vector -- pbkdf2 cost is dominated by the iteration count -- but a huge
        # string has no legitimate reason to reach a numeric-PIN compare).
        if not isinstance(pin, str) or not pin or len(pin) > 128:
            _pin_limiter.record_failure()
            return {"ok": False}
        ok = _pin_store.verify(pin)
        if ok:
            _pin_limiter.record_success()
        else:
            _pin_limiter.record_failure()
        return {"ok": ok}

    @app.get("/api/core/pin/status")
    async def core_pin_status(_=Depends(require_scope("presence:read"))):
        # Read-only, no secret (bool only) -- lets the panel know whether a PIN
        # lock is configured at all. presence:read (NOT admin): its sibling
        # /api/core/pin/verify deliberately widens to any authenticated LAN peer
        # showing the panel, so this bool must stay reachable by root+central+user;
        # presence:read is the codebase idiom for "any real device, not agent" and
        # keeps that floor while closing the leak to 'agent'. (2026-07-16)
        return {"pin_set": _pin_store.is_set()}

    @app.get("/api/health")
    async def health(_=Depends(require_local), __=Depends(require_scope("control"))):
        # On-demand only -- no background task, no new opt-in flag (see the
        # _health_check/_health_resolvers construction above for the
        # LOCAL-ONLY rationale). 5-tier severity ladder (defensive-inventory #12):
        # gateway + public-resolver reachability + optional operator-extra
        # targets, rolled into one severity verdict (wavr.health_check).
        # system-toggles egress master: the gateway leg stays LAN-only regardless
        # (guess_gateway/internet_check_host, zero egress); only the resolver legs
        # (the one real public-internet egress in this route) fall back to {} when
        # the operator has blocked egress from the System tab -- same {} shape as
        # the existing WAVR_HEALTH_RESOLVERS-off default, so severity is computed
        # from gateway + extra targets only, honestly.
        resolver_checks = _health_resolvers if _connectors.egress_allowed() else {}
        result = await check_health(
            gateway_check=_health_check, gateway_host=_health_host,
            resolver_checks=resolver_checks, extra_checks=_health_extra,
        )
        result["internet_monitor"] = _internet.status() if _internet else None
        return result

    # network-doctor (GET /api/health/doctor): read-only diagnosis by default,
    # a narrow auto-fix layer only when BOTH WAVR_NET_DOCTOR_AUTOFIX is set AND
    # the caller passes auto_fix=true. Every fix dispatched below is a call to
    # an already-existing, already-authenticated-route-reachable primitive
    # (set_enabled, scan_once, advertise_self) -- this module adds no new
    # low-level capability. See wavr.net_doctor's module docstring for the
    # SAFE-AUTO allowlist enforced in code.
    async def _doctor_restart_source(name: str) -> None:
        # Only ever CYCLES a source SourceManager already reports enabled=True
        # (net_doctor.diagnose only proposes this for such sources) -- never
        # flips a disabled/privacy-off camera on. Mirrors POST
        # /api/sources/{name}/toggle called twice.
        await manager.set_enabled(name, False)
        await manager.set_enabled(name, True)

    async def _doctor_reprobe_inventory() -> None:
        if cfg.net_inventory:
            await _inventory.scan_once()

    def _doctor_reannounce_mdns() -> None:
        if not cfg.peers_enabled:
            return
        from wavr.mdns_peers import advertise_self
        old = _mdns_state.get("handle")
        if old is not None:
            with suppress(Exception):
                old.stop()
        try:
            _mdns_state["handle"] = advertise_self(cfg.instance_name, cfg.port, role="desktop")
        except Exception:
            logging.warning("doctor: mDNS re-announce failed", exc_info=True)
            _mdns_state["handle"] = None

    @app.get("/api/health/doctor")
    async def health_doctor(auto_fix: bool = False,
                            _=Depends(require_local), __=Depends(require_scope("control"))):
        result = await check_health(
            gateway_check=_health_check, gateway_host=_health_host,
            # system-toggles egress master: gate the public-resolver leg exactly like
            # /api/health (~line 2476) so "Egress: blocked" actually blocks the doctor's
            # resolver egress too -- the gateway/extra legs stay LAN-only regardless.
            resolver_checks=_health_resolvers if _connectors.egress_allowed() else {},
            extra_checks=_health_extra,
        )
        room_sources = {r: ((s.sources if (s := _fusion.state(r)) else []))
                        for r in _fusion.rooms()}
        checks, fixable = diagnose(
            health=result,
            gateway_status=_gateway_monitor.status() if _gateway_monitor else None,
            gateway_alerts=[a.to_dict() for a in
                           (_gateway_monitor.recent_alerts(5) if _gateway_monitor else [])],
            dhcp_status=_dhcp_monitor.status() if _dhcp_monitor else None,
            dhcp_alerts=[a.to_dict() for a in
                        (_dhcp_monitor.recent_alerts(5) if _dhcp_monitor else [])],
            source_status=manager.status(),
            camera_down=_camera_health.down(), camera_privacy=_camera_health.privacy(),
            room_sources=room_sources,
            last_inventory_scan_ts=_inventory.last_scan_ts(), net_scan_interval=cfg.net_scan_interval,
            mdns_expected=cfg.peers_enabled, mdns_alive=_mdns_state.get("handle") is not None,
        )
        fixed, suggestions = await apply_fixes(
            fixable, enabled=(auto_fix and cfg.net_doctor_autofix),
            restart_source=_doctor_restart_source, reprobe_inventory=_doctor_reprobe_inventory,
            reannounce_mdns=_doctor_reannounce_mdns, log=_doctor_log,
        )
        return {"checks": [c.to_dict() for c in checks],
                "auto_fixed": [a.to_dict() for a in fixed],
                "suggestions": [s.to_dict() for s in suggestions],
                "recent_auto_fixes": [a.to_dict() for a in _doctor_log.recent(20)]}

    @app.get("/api/system")
    async def system(_=Depends(require_scope("control"))):
        # control, mirroring its own POST /api/system/toggle sibling: per-source
        # enabled/active is system-plane posture an 'agent' (scope {mcp}) must not
        # enumerate over plain HTTP, outside the MCP audit trail. root+central pass.
        return manager.status()

    @app.post("/api/system/toggle")
    async def system_toggle(on: bool = Body(..., embed=True), _=Depends(require_local),
                            __=Depends(require_scope("control"))):
        await manager.set_running(on)
        return manager.status()

    @app.post("/api/sources/{name}/toggle")
    async def source_toggle(name: str, enabled: bool = Body(..., embed=True),
                            _=Depends(require_local), __=Depends(require_scope("control"))):
        try:
            await manager.set_enabled(name, enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown source: {name}")
        return manager.status()

    @app.get("/api/cameras")
    async def cameras(_=Depends(require_scope("camera:view"))):
        return _masked_cameras()

    @app.post("/api/cameras")
    async def add_camera(
        name: str = Body(...), room: str = Body(...),
        rtsp_url: str = Body(...), confidence: float = Body(cfg.cam_confidence),
        mac: str | None = Body(None), level: int | None = Body(None),
        _=Depends(require_local), __=Depends(require_scope("control")),
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
        # Geometry fix (HIGH-1): optional per-camera floor level, so a multi-floor
        # house with a same-named room on two floors (e.g. two "quarto"s) can be
        # disambiguated by housemap.room_polygon(..., level=...). Validated against
        # the CURRENT house map's known levels -- never persisted if it doesn't
        # exist, so a typo can't silently strand the camera room-centred forever.
        if level is not None:
            known_levels = {f.get("level") for f in _house.get("floors", [])}
            if level not in known_levels:
                raise HTTPException(status_code=400,
                                    detail=f"level {level} does not exist in the house map")
        if name in {s["name"] for s in manager.status()["sources"]}:
            raise HTTPException(status_code=409, detail=f"source name in use: {name}")
        try:
            _cameras.add(name, room, rtsp_url, confidence, mac=clean_mac, level=level)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"camera exists: {name}")
        manager.register(name, _camera_factory(_cameras.get(name), cfg, _camera_health.report, _calib, _house,
                                            on_privacy=_camera_health.report_privacy), False)  # boots OFF
        return _masked_cameras()

    @app.post("/api/onvif/probe")
    async def onvif_probe(targets: list[str] | None = Body(None, embed=True),
                          username: str | None = Body(None, embed=True),
                          password: str | None = Body(None, embed=True),
                          timeout: float = Body(3.0, embed=True),
                          _=Depends(require_local), __=Depends(require_scope("control"))):
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
    async def delete_camera(name: str, _=Depends(require_local),
                            __=Depends(require_scope("control"))):
        if not _cameras.delete(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            await manager.unregister(name)
        except KeyError:
            pass   # not registered (e.g. removed before a restart re-registered it)
        _camera_health.clear(name)   # drop any stale drift suggestion for the removed cam
        return _masked_cameras()

    def _calib_view(name: str) -> dict:
        # Read-only calibration view. Carries NO credentials (calibration is pure
        # geometry: a mount prior + a homography matrix + the pixel size it was marked
        # at). A corrupt stored blob degrades to null (store.get never raises).
        c = _calib.get(name)
        if c is None:
            return {"camera": name, "mount": None, "homography": None,
                    "img_w": None, "img_h": None, "quality": None,
                    "updated": None, "localizes": False}
        localizes = bool(c.get("homography") or c.get("mount"))
        return {"camera": name,
                "mount": c["mount"].to_dict() if c.get("mount") else None,
                "homography": c.get("homography"),
                "img_w": c.get("img_w"), "img_h": c.get("img_h"),
                "quality": c.get("quality"),
                "updated": c.get("updated"), "localizes": localizes}

    async def _reregister_camera(name: str) -> None:
        # Rebuild the source factory so it picks up the new calibration (mirrors
        # set_url/rebind exactly). unregister() KILLS a running source, then we
        # re-register boot-OFF -- so a calibration change stops a live camera and the
        # operator re-enables it, at which point the new localizer takes effect. A
        # calibration change never auto-enables a camera (ADR-0002).
        cam = _cameras.get(name)
        if not cam:
            return
        with suppress(KeyError):
            await manager.unregister(name)   # kill before re-register (mirror rebind)
        manager.register(name,
                         _camera_factory(cam, cfg, _camera_health.report, _calib, _house,
                                        on_privacy=_camera_health.report_privacy),
                         False)

    @app.get("/api/cameras/{name}/calibration")
    async def get_calibration(name: str, _=Depends(require_scope("camera:view"))):
        # Read-only (loopback middleware is the gate, like GET /api/inventory). 404 for
        # an unknown camera so the UI can't probe arbitrary names.
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        return _calib_view(name)

    @app.put("/api/cameras/{name}/calibration")
    async def put_calibration(
        name: str,
        mount: dict | None = Body(None),
        image_points: list | None = Body(None),
        floor_points: list | None = Body(None),
        img_w: int | None = Body(None),
        img_h: int | None = Body(None),
        use_session: bool = Body(False),
        _=Depends(require_local), __=Depends(require_scope("control")),
    ):
        # Spec A. Two independent, composable calibrations, both state-changing
        # (require_local / CSRF): a MONOCULAR mount prior (approximate immediate
        # estimate) and/or an accurate 4-POINT homography. The homography is ALWAYS
        # solved server-side from image<->floor correspondences via
        # localize.homography_from_points (via calib_refine.solve_progressive), so the
        # degeneracy guard (collinear/coincident) runs before anything is persisted --
        # the client never hands us a raw matrix. No frame is involved: image_points
        # are the operator's marks (pixels), never a stored image (ADR-0002).
        #
        # `use_session=true` (guided-calib, server-driven session): when the caller
        # supplies NO explicit image_points/floor_points, pull them from this camera's
        # completed CalibSession instead (409 if there is none, or it isn't READY --
        # every spot must be captured first). The EXPLICIT image_points/floor_points
        # body path below is completely UNCHANGED either way -- full backward
        # compatibility with the browser wizard, which still posts its own points.
        used_session = False
        sess = None
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        if use_session and image_points is None and floor_points is None:
            sess = _calib_session.get(name)
            if sess is None or sess.state != SessionState.READY:
                raise HTTPException(
                    status_code=409,
                    detail="no completed calibration walk for this camera -- "
                           "capture every spot first")
            image_points = [list(p[0]) for p in sess.pairs]
            floor_points = [list(p[1]) for p in sess.pairs]
            img_w, img_h = sess.img_size
            used_session = True
        wrote = False
        if mount is not None:
            try:
                pose = validate_mount(mount)
            except CalibrationError as exc:
                raise HTTPException(status_code=422, detail=f"mount: {exc}")
            _calib.set_mount(name, pose)
            wrote = True
        if image_points is not None or floor_points is not None:
            if not isinstance(image_points, list) or not isinstance(floor_points, list):
                raise HTTPException(status_code=422,
                                    detail="image_points and floor_points must both be arrays")
            if len(image_points) != len(floor_points):
                raise HTTPException(status_code=422,
                                    detail="image_points and floor_points must be the same length")
            if len(image_points) < 4:
                raise HTTPException(status_code=422,
                                    detail="need >= 4 point correspondences")
            if len(image_points) > 1000:
                # Bound the DLT input (defence-in-depth: even a local operator shouldn't
                # be able to hand us a pathologically large SVD).
                raise HTTPException(status_code=422,
                                    detail="too many point correspondences (max 1000)")
            if not (isinstance(img_w, int) and isinstance(img_h, int)
                    and 0 < img_w <= 100_000 and 0 < img_h <= 100_000):
                raise HTTPException(status_code=422,
                                    detail="img_w and img_h must be positive integers")
            # calib_refine.solve_progressive merges these points with any PRIOR walk's
            # stored correspondences (same camera, same pixel size) before solving --
            # a camera calibrated for the first time (or at a new resolution) sees
            # merge_points return its points unchanged, so this is byte-identical to
            # the old inline one-shot solve for every existing caller/test.
            # CalibrationError is a ValueError subclass (calib_store.py), so this one
            # except also catches the store's own persistence-shape guards.
            try:
                solve_progressive(_calib, name, image_points, floor_points, img_w, img_h)
            except ValueError as exc:
                # Degenerate / non-finite / malformed correspondences, or an
                # out-of-range persisted size -> 422, never a silently near-singular
                # matrix that mislocates every later projection.
                raise HTTPException(status_code=422, detail=f"homography: {exc}")
            wrote = True
        if not wrote:
            raise HTTPException(status_code=400,
                                detail="provide a mount and/or image_points+floor_points")
        if used_session and sess is not None:
            sess.mark_solved()
            _calib_session.end(name)
        await _reregister_camera(name)
        return _calib_view(name)

    @app.delete("/api/cameras/{name}/calibration")
    async def delete_calibration(name: str, _=Depends(require_local),
                                 __=Depends(require_scope("control"))):
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        removed = _calib.delete(name)
        await _reregister_camera(name)   # source reverts to room-centred on next start
        return {"camera": name, "removed": removed}

    @app.get("/api/cameras/{name}/calib-spots")
    async def get_calib_spots(name: str, _=Depends(require_central),
                              __=Depends(require_scope("control"))):
        # Walk-to-calibrate KNOWN floor spots (FLOOR metres): room centroid + polygon
        # corners the wizard guides the person to, one at a time. Pure geometry from the
        # house map -- no frame, no credential (ADR-0002). Read-only but central+loopback
        # gated (require_central) like the live sample below. 404 for an unknown camera.
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        cam = _cameras.get(name)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        poly = room_polygon(_house, cam["room"], level=cam.get("level"))
        spots = floor_spots_for_room(poly) if poly else []
        out = [{"x": x, "y": y, "label": ("centre" if i == 0 else f"corner-{i}")}
               for i, (x, y) in enumerate(spots)]
        return {"camera": name, "room": cam["room"], "spots": out}

    @app.get("/api/cameras/{name}/calib-sample")
    async def get_calib_sample(name: str, _=Depends(require_central),
                               __=Depends(require_scope("control"))):
        # Walk-to-calibrate READ path. Returns ONLY the latest detected person's FEET
        # PIXEL (a coordinate) + image dims + detection confidence for this camera, or
        # nulls when there is no FRESH detection (no session, no person, or a stale
        # sample -> `person: false`). ADR-0002: a pixel coordinate is NOT image data --
        # NO frame/crop/image is ever read here or returned. Central+loopback gated
        # (require_central), so a multidevice 'user' can't read live positions.
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        s = _calib_sample.latest(name)
        if s is None:
            return {"camera": name, "person": False, "feet_px": None,
                    "img_w": None, "img_h": None, "confidence": None}
        return {"camera": name, "person": True,
                "feet_px": [s["feet_px"][0], s["feet_px"][1]],
                "img_w": s["img_w"], "img_h": s["img_h"],
                "confidence": s["confidence"]}

    @app.post("/api/cameras/{name}/calib-session")
    async def calib_session(name: str, active: bool = Body(..., embed=True),
                            _=Depends(require_local), __=Depends(require_scope("control"))):
        # Start/stop a walk-to-calibrate SAMPLING session. ACTIVE: re-register the camera
        # so its pose pass records the walker's FEET PIXEL (coordinate only, ADR-0002)
        # into the sample store, and START it running so frames flow -- the operator is
        # about to walk the room. This is an explicit, operator-initiated maintenance
        # action, so it MAY run the camera (the only way to see the walker); ENDING the
        # session STOPS it again (cameras off by default). No calibration is written here
        # -- the wizard PUTs the collected (feet_px, floor_point) pairs to /calibration
        # when done. require_local (CSRF + central role).
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        cam = _cameras.get(name)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        with suppress(KeyError):
            await manager.unregister(name)   # kill before re-register (mirror rebind)
        if active:
            # Guided-calib session: also start the server-side CalibSession so the
            # 'stand here -> capture -> repeat -> solve' walk survives a frontend
            # reload and a non-browser driver (MCP/voice) can run it. Reuses the
            # existing "needs 4+ corner floor plan" guard -- today only enforced
            # client-side (index.html) -- server-side too, so a non-browser caller
            # gets the same honest refusal instead of a session with too few spots
            # to ever solve.
            poly = room_polygon(_house, cam["room"], level=cam.get("level"))
            spots = floor_spots_for_room(poly) if poly else []
            if len(spots) < 4:
                manager.register(name, _camera_factory(
                    cam, cfg, _camera_health.report, _calib, _house,
                    on_privacy=_camera_health.report_privacy), False)  # leave camera OFF
                raise HTTPException(
                    status_code=422,
                    detail="camera's room needs a 4+ corner floor plan first")
            _calib_session.start(name, spots)
            manager.register(name, _camera_factory(
                cam, cfg, _camera_health.report, _calib, _house,
                sample_store=_calib_sample, sampling=True,
                on_privacy=_camera_health.report_privacy), True)   # boots ON to walk
        else:
            _calib_sample.clear(name)         # drop any lingering feet pixel
            _calib_session.end(name)          # drop any in-progress/finished walk state
            manager.register(name, _camera_factory(
                cam, cfg, _camera_health.report, _calib, _house,
                on_privacy=_camera_health.report_privacy), False)  # back OFF
        active_now = {s["name"]: s["active"] for s in manager.status()["sources"]}
        return {"camera": name, "sampling": active,
                "active": bool(active_now.get(name))}

    @app.post("/api/cameras/{name}/calib-capture")
    async def calib_capture(name: str, _=Depends(require_local),
                            __=Depends(require_scope("control"))):
        # Guided-calib: pair the session's CURRENT known floor spot with the camera's
        # latest FEET PIXEL (a coordinate, ADR-0002) and advance the walk. 409 mirrors
        # the browser wizard's own "no active session" / "no person detected" states --
        # a session-state conflict, never a 500. Nothing is written to CalibrationStore
        # here (change-gated: only a completed PUT .../calibration solve ever touches
        # disk -- an aborted or abandoned walk leaves zero trace, SD-wear).
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        sess = _calib_session.get(name)
        if sess is None:
            raise HTTPException(status_code=409, detail="no active calibration session")
        s = _calib_sample.latest(name)
        if s is None:
            raise HTTPException(
                status_code=409,
                detail="no person detected -- stand where the camera can see you, "
                       "then capture")
        try:
            sess.capture(s["feet_px"], s["img_w"], s["img_h"])
        except CalibSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        current = sess.spots[sess.spot_idx] if sess.spot_idx < len(sess.spots) else None
        return {"camera": name, "state": sess.state.value, "spot_idx": sess.spot_idx,
                "spots_total": len(sess.spots),
                "current_spot": (list(current) if current is not None else None)}

    @app.post("/api/cameras/{name}/calib-retry")
    async def calib_retry(name: str, _=Depends(require_local),
                          __=Depends(require_scope("control"))):
        # Guided-calib: undo the last capture and step back to re-try that spot -- a
        # capability the browser-only wizard doesn't have today (it only ever cancels
        # the whole walk). Nothing is persisted either way (same change-gated write
        # boundary as calib-capture).
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        if not _cameras.get(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        sess = _calib_session.get(name)
        if sess is None:
            raise HTTPException(status_code=409, detail="no active calibration session")
        try:
            sess.retry_current()
        except CalibSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        current = sess.spots[sess.spot_idx] if sess.spot_idx < len(sess.spots) else None
        return {"camera": name, "state": sess.state.value, "spot_idx": sess.spot_idx,
                "spots_total": len(sess.spots),
                "current_spot": (list(current) if current is not None else None)}

    @app.get("/api/cameras/suggestions")
    async def camera_suggestions(_=Depends(require_scope("network:read"))):
        # F3 read-only IP-drift suggestions (loopback middleware is the gate, like
        # GET /api/inventory -- no CSRF). Each: {camera, mac, current_ip, suggested_ip,
        # vendor, ts}. IP+MAC+vendor only (already non-sensitive per /api/inventory);
        # the rtsp_url (creds) is NEVER included. Empty when there is no drift, no
        # inventory, or no stored MAC. NOT authoritative -- a MAC-spoofing LAN attacker
        # can manufacture one, so the UI must require explicit confirmation before /rebind.
        return {"suggestions": _camera_health.suggestions()}

    @app.post("/api/cameras/{name}/rebind")
    async def rebind_camera(name: str, ip: str = Body(..., embed=True),
                            _=Depends(require_local), __=Depends(require_scope("control"))):
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
        manager.register(name, _camera_factory(_cameras.get(name), cfg, _camera_health.report, _calib, _house,
                                            on_privacy=_camera_health.report_privacy), False)
        _camera_health.clear(name)
        return _masked_cameras()

    @app.post("/api/cameras/{name}/privacy-mode")
    async def set_camera_privacy_mode(name: str, enabled: bool = Body(..., embed=True),
                                      _=Depends(require_local),
                                      __=Depends(require_scope("control"))):
        # STUB -- always 501. Wavr can DETECT Tapo privacy mode (GET /api/cameras'
        # liveness='privacy'), but CONTROLLING it locally has no documented ONVIF or
        # local-API path (TP-Link's own docs say ONVIF-compliant software cannot toggle
        # it; the only known path is an undocumented, proprietary encrypted protocol --
        # see wavr.camera_privacy for the full reasoning). This route exists so the gap
        # is honest and discoverable in the API, not silently missing, and gives the
        # frontend a real endpoint to call for a disabled/"not yet available" control.
        # Gated identically to /rebind (require_local CSRF + control scope) so it is
        # never reachable from an unauthenticated/remote caller even once implemented.
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="camera name must be alphanumeric/_/-")
        cam = _cameras.get(name)
        if not cam:
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            set_privacy_mode(name, cam["rtsp_url"], enabled)
        except PrivacyControlNotImplemented as e:
            raise HTTPException(status_code=501, detail=str(e)) from None

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
                       zoom: float = Body(0.0), _=Depends(require_local),
                       __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        if not _camera_active(camera_id):
            # Camera off -> no ONVIF call at all (kill-switch dominates PTZ).
            return {"ok": False, "reason": "camera off"}
        ok = await _ptz.continuous_move(camera_id, cam["rtsp_url"], pan, tilt, zoom)
        return {"ok": ok}

    @app.post("/api/ptz/{camera_id}/stop")
    async def ptz_stop(camera_id: str, _=Depends(require_local),
                       __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        # Stop is always allowed (safety): even a just-disabled camera should halt.
        return {"ok": await _ptz.stop(camera_id, cam["rtsp_url"])}

    @app.get("/api/ptz/{camera_id}/presets")
    async def ptz_presets(camera_id: str, _=Depends(require_scope("camera:view"))):
        cam = _ptz_cam(camera_id)
        return await _ptz.get_presets(camera_id, cam["rtsp_url"])

    @app.post("/api/ptz/{camera_id}/preset/{token}")
    async def ptz_goto_preset(camera_id: str, token: str, _=Depends(require_local),
                              __=Depends(require_scope("control"))):
        cam = _ptz_cam(camera_id)
        if not _PRESET_RE.match(token):
            raise HTTPException(status_code=400, detail="invalid preset token")
        if not _camera_active(camera_id):
            return {"ok": False, "reason": "camera off"}
        return {"ok": await _ptz.goto_preset(camera_id, cam["rtsp_url"], token)}

    @app.get("/api/ptz/{camera_id}/capabilities")
    async def ptz_capabilities(camera_id: str, _=Depends(require_scope("camera:view"))):
        cam = _ptz_cam(camera_id)
        return await _ptz.capabilities(camera_id, cam["rtsp_url"])

    @app.get("/api/ptz/{camera_id}/status")
    async def ptz_status(camera_id: str, _=Depends(require_scope("camera:view"))):
        # Read-only PTZ position (pan/tilt/zoom) -- the BEARING SEAM for person
        # localization on a pan/tilt camera. Same gate/pattern as capabilities:
        # WAVR_PTZ + loopback; reads ONLY ONVIF control metadata, NEVER a frame
        # (ADR-0002). Creds come from the stored rtsp_url and never reach the response.
        # None (non-PTZ/offline/faulting camera) surfaces as {"status": null}.
        cam = _ptz_cam(camera_id)
        return {"status": await _ptz.get_status(camera_id, cam["rtsp_url"])}

    if cfg.multidevice:
        @app.post("/api/pair-code")
        async def pair_code(role: str = Body("user", embed=True), _=Depends(require_local),
                            __=Depends(require_scope("admin"))):
            # Operator (loopback root / central) mints a one-time pairing code that a
            # companion then redeems at POST /api/pair. Gated by require_local.
            if role not in ("central", "user"):
                raise HTTPException(status_code=400, detail="role must be central or user")
            # Out-of-band MitM defense (audit blocking #1): return the SHA-256 fingerprint
            # of the LIVE serving cert, read off this TRUSTED loopback response, so the
            # operator can verify it against the fingerprint the phone's browser shows in
            # its certificate warning BEFORE accepting. A pairing-time TLS MitM presents a
            # different self-signed cert -> different fingerprint -> the operator sees the
            # mismatch and stops. `cryptography` is not imported (pure-stdlib fingerprint).
            from wavr.tls import cert_fingerprint, resolved_cert_path, verification_code
            fingerprint = cert_fingerprint(resolved_cert_path(cfg.tls_cert))
            code = _pairing.mint_code(role)
            # Convenience-tier 6-digit, bound to THIS code so it rotates with it
            # (pinned derivation, see wavr.tls.verification_code). None if the cert
            # fingerprint itself couldn't be read (e.g. no cert on disk yet) --
            # same fail-open-to-None shape cert_fingerprint already uses, so the
            # shim can fall back to the (still-shown) full fingerprint compare.
            verify6 = verification_code(fingerprint, code) if fingerprint else None
            # LAN-reachable base for the QR builder (P2 self-contained QR): when this panel is
            # viewed on the hub itself (kiosk/loopback), location.origin is 127.0.0.1/localhost --
            # useless to a phone that scans the code cold. _local_ip is the SAME LAN address
            # self_base_url already uses for the peers-admin router above; TLS is coupled 1:1 to
            # multidevice (see serve.py), so "https" here is exactly as safe as line ~1675.
            lan_url = f"https://{_local_ip}:{cfg.port}"
            return {"code": code, "cert_fingerprint": fingerprint, "verify6": verify6,
                    "lan_url": lan_url}

        # "Approve on the Core" (design 2026-07-11). Two routers, two DIFFERENT auth
        # boundaries -- the exact split api_peers.py/api_nodes.py already use:
        #  * public (create/poll, mounted here as build_pair_request_router): NO deps --
        #    the middleware exempts BOTH exact paths in-subnet, same deliberately-
        #    unauthenticated onboarding surface as /api/pair above. Mints nothing.
        #  * admin (list/approve/deny, build_pending_pairings_router): LOOPBACK-ROOT
        #    ONLY -- require_local (X-Wavr-Local CSRF header) + require_root, the SAME
        #    tier as the peers-admin/nodes-admin routers, so require_root rejects even
        #    an authenticated remote 'central' peer -- a stolen/paired central token can
        #    never approve its own (or any other) pending request. Approve is the ONLY
        #    mint site and calls the SAME _devices.add(name, role) /api/pair-code does.
        # _live_cert_fp reads the SAME live-cert source as pair_code above (never a
        # companion-reported fp) -- injected so neither router touches TLS/filesystem
        # itself, and reused for both the create() response and the admin list's fp
        # (so the Core operator banner can show its own fingerprint for the eyeball
        # compare without minting a pairing code as a side effect).
        def _live_cert_fp() -> str:
            from wavr.tls import cert_fingerprint, resolved_cert_path
            return cert_fingerprint(resolved_cert_path(cfg.tls_cert))

        app.include_router(build_pair_request_router(_pair_approvals, _live_cert_fp))
        app.include_router(build_pending_pairings_router(
            _pair_approvals, _live_cert_fp,
            admin_deps=[Depends(require_local), Depends(require_root)]))

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
            # A WS carries no scope through the http middleware, so gate it here for
            # the SAME reason ws-ticket is gated at include-time: the stream is the
            # per-person geometry + vitals class, and an 'agent' (scopes = {mcp})
            # must never reach it even if it somehow obtained a ticket. Belt AND
            # braces -- the ticket mint is already scope-gated, but the socket must
            # not depend on that being the only door. (2026-07-16)
            if not has_scope(effective_scopes(dev.role, dev.scopes), "presence:read"):
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
        # M1 (revoke latency): the stream loop re-checks the revoked flag on a wall-clock
        # cadence (see _stream_live). did is None for loopback root -> no check; _devices is
        # always present when did is set (multidevice on), but guard the attribute read anyway.
        get_device = _devices.get if _devices is not None else None
        try:
            await _stream_live(ws, q, did, get_device, _WS_REVOKE_RECHECK_S)
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


# F1 (appsec re-audit, 2026-07, MEDIUM-HIGH -- originally audit HIGH: pre-auth
# resource exhaustion): global request-body-size cap, wrapped around the MODULE-
# LEVEL `app` singleton below rather than only inside wavr.serve.main(). Every
# test builds its OWN unwrapped instance via create_app() directly (transport-
# agnostic, never binds a socket) -- but this singleton is what EVERY real
# uvicorn entry point imports: wavr.serve's launcher (which sets local TLS) AND
# the Dockerfile/docker-compose/scripts/wavr.ps1 invocations that run
# `uvicorn wavr.app:app` DIRECTLY. That direct path bypassed serve.py's wrapper
# entirely -- serve.py's old docstring claim that it was "the ONE place a
# listening uvicorn socket actually opens" was false. Wrapping HERE means every
# entry point carries the cap by construction; wavr.serve.main() no longer wraps
# a second time (see its own docstring) -- it only re-reads WAVR_MAX_BODY_BYTES
# at call time and updates this SAME instance's `_max_bytes` in place, so a
# same-process env override (e.g. in a test) still takes effect without a
# double-wrapped ASGI chain.
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024  # 8 MiB


class MaxBodySizeMiddleware:
    """Pure-ASGI wrapper -- deliberately NOT installed via FastAPI's
    ``app.add_middleware()`` (Starlette's ``ServerErrorMiddleware`` sits OUTSIDE every
    ``add_middleware()`` entry; an exception raised from inside our own guard would
    still propagate through it, which sends its OWN 500 response before re-raising --
    a double `send()`). Wrapping the ASGI callable directly here instead puts this
    guard entirely OUTSIDE that stack, so it can hand back one clean response itself.

    Two checks, cheapest first:
      1. ``Content-Length``: an honest client's declared size is checked BEFORE any
         body is read -- the common case, zero bytes consumed for an oversized request.
      2. Streamed drain-and-replay: covers a client that omits ``Content-Length``
         (chunked transfer) or under-declares it. Reads at most ``max_bytes + 1`` bytes
         before either rejecting (413, nothing forwarded to the app) or replaying the
         buffered chunks verbatim to the wrapped app (the same trick
         ``wavr.mcp_http._buffer_body`` already uses) -- a within-budget request is
         byte-identical to today; an over-budget one never reaches the app at all, so
         it can never partially consume memory/CPU parsing it.
    """

    def __init__(self, asgi_app, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self._app = asgi_app
        self._max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        # Only HTTP requests carry a body this way; websocket/lifespan scopes pass
        # straight through untouched (e.g. /ws/live streaming is unaffected).
        if scope.get("type") != "http" or self._max_bytes <= 0:
            await self._app(scope, receive, send)
            return

        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None  # malformed header -- fall through to the drain guard
                if declared is not None and declared > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
                break

        total = 0
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            total += len(message.get("body", b"") or b"")
            if total > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break

        it = iter(messages)

        async def _replay():
            try:
                return next(it)
            except StopIteration:
                return await receive()

        await self._app(scope, _replay, send)

    async def _reject(self, scope, receive, send) -> None:
        await JSONResponse({"detail": "request body too large"}, status_code=413)(
            scope, receive, send)


def _max_body_bytes_from_env() -> int:
    return int(os.getenv("WAVR_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))


app = MaxBodySizeMiddleware(create_app(), max_bytes=_max_body_bytes_from_env())
