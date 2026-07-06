"""Wavr ONVIF PTZ actuator (A4.3) -- OPT-IN, DEFAULT-OFF pan/tilt/zoom control of a
STORED camera over ONVIF ver20 PTZ.

This is the FIRST camera *actuator* in Wavr (everything else camera-side only reads
discovery/description metadata). It moves a camera the operator has explicitly added
(`POST /api/cameras`) and explicitly turned ON. It reads NO video frame, opens NO RTSP
stream, decodes/persists NOTHING -- it exchanges only ONVIF SOAP control metadata
(GetServices / GetProfiles / ContinuousMove / Stop / GetPresets / GotoPreset).

HARD INVARIANTS (each is load-bearing; a violation is a defect):

  * OPT-IN / DEFAULT-OFF. Every route is gated by `WAVR_PTZ` (config.ptz, default OFF)
    + `require_local` CSRF + the master camera kill-switch (a move is refused unless the
    target camera source is currently ACTIVE in the SourceManager -- so killing cameras
    atomically neuters PTZ).

  * SSRF-HARD. The ONVIF device-service URL and the discovered PTZ-service URL are both
    validated to a LITERAL LAN IP (`_xaddr_ok`/`_is_lan_ip`, reused verbatim from
    sources.onvif) BEFORE any connection; HTTP redirects are blocked
    (`_NO_REDIRECT_OPENER`) so a camera cannot 302 the SOAP call off-LAN; a non-LAN
    camera is refused and NEVER contacted.

  * CREDENTIALS STAY LOCAL. PTZ creds come ONLY from the stored camera's `rtsp_url`
    (proven: the Tapo's ONVIF account == its RTSP account). They are parsed into method
    locals, used to build the WS-UsernameToken digest (password never even reaches the
    SOAP body -- WS-Security digest scheme), and are NEVER accepted over the PTZ API,
    NEVER logged, NEVER echoed in a response or a traceback surfaced to the client.

  * NO CRASH ON HOSTILE XML. Every SOAP response is parsed with `_safe_root`
    (DOCTYPE-reject / size-bounded / never-raise); every transport error degrades to
    `False`/`[]`, never an exception that reaches the route.

  * RUNAWAY-SLEW GUARD. A ContinuousMove slews until a Stop. If the client tab crashes
    or the network drops between move and stop, this arms a server-authoritative
    auto-stop (`_AUTOSTOP_S`, re-armed on each move, cancelled on explicit stop) so the
    camera cannot slew forever -- holds even for a raw curl client that forgets to stop.

The SOAP transport is INJECTABLE (`PtzSoap`), the same seam as sources.onvif: tests
drive canned responses with zero real sockets.

Port note (honesty): `_ONVIF_PORT = 2020` is PROVEN for the owner's Tapo C210. Other
ONVIF cameras commonly use 80; a per-camera ONVIF-port override is a follow-up, NOT in
this cut. "Works on non-Tapo cameras" is NOT VERIFIED.
"""
from __future__ import annotations

import asyncio
import logging
import math
import urllib.parse
import urllib.request as _urllib_request
from typing import Awaitable, Callable

from .sources.onvif import (
    _NO_REDIRECT_OPENER,
    _NS_SCHEMA,
    _SOAP_READ_CAP,
    _envelope,
    _first_text,
    _host_of,
    _is_lan_ip,
    _iter_local,
    _safe_root,
    _xaddr_ok,
    _xml_escape,
)

_LOG = logging.getLogger(__name__)

# ONVIF namespaces (for building requests only; parsing stays namespace-agnostic).
_NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
_NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"  # GetServices lives on the device svc

_ONVIF_PORT = 2020            # proven for the Tapo C210 (see module docstring)
_DEVICE_PATH = "/onvif/device_service"
_PTZ_FALLBACK = "/onvif/service"   # used when GetServices is unparsed / lists no PTZ svc
_AUTOSTOP_S = 2.0             # server-side runaway-slew guard (re-armed each move)

# Injectable transport seam: soap(url, body, action, timeout) -> response xml str.
PtzSoap = Callable[[str, str, str, float], Awaitable[str]]

# SOAPAction values (passed via the Content-Type `action` param, as the Tapo requires).
_ACT_CONTINUOUS = f"{_NS_PTZ}/ContinuousMove"
_ACT_STOP = f"{_NS_PTZ}/Stop"
_ACT_GET_PRESETS = f"{_NS_PTZ}/GetPresets"
_ACT_GOTO_PRESET = f"{_NS_PTZ}/GotoPreset"
_ACT_GET_STATUS = f"{_NS_PTZ}/GetStatus"
_ACT_GET_SERVICES = f"{_NS_DEVICE}/GetServices"


# --------------------------------------------------------------------------- #
# Clamp / format helpers.
# --------------------------------------------------------------------------- #

def _clamp_unit(value) -> float:
    """Coerce to a float in [-1.0, 1.0]. NaN/inf/garbage -> 0.0 (fail to 'no motion').
    This is the AUTHORITATIVE clamp -- the route's own guard is belt-and-suspenders."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(-1.0, min(1.0, f))


def _fmt(v: float) -> str:
    # Fixed 4-decimal notation so no locale decimal comma / `e` notation reaches the XML.
    return f"{v:.4f}"


# --------------------------------------------------------------------------- #
# SOAP body builders (reuse _envelope + WS-Security; every token XML-escaped).
# --------------------------------------------------------------------------- #

def build_get_services(user: str | None, pw: str | None) -> str:
    body = (f'<tds:GetServices xmlns:tds="{_NS_DEVICE}">'
            "<tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
    return _envelope(body, user, pw)


def build_continuous_move(token: str, x: float, y: float, zoom: float,
                          user: str | None, pw: str | None) -> str:
    tok = _xml_escape(token)
    zoom_xml = f'<tt:Zoom x="{_fmt(zoom)}"/>' if zoom != 0.0 else ""
    body = (
        f'<tptz:ContinuousMove xmlns:tptz="{_NS_PTZ}" xmlns:tt="{_NS_SCHEMA}">'
        f"<tptz:ProfileToken>{tok}</tptz:ProfileToken><tptz:Velocity>"
        f'<tt:PanTilt x="{_fmt(x)}" y="{_fmt(y)}"/>{zoom_xml}'
        "</tptz:Velocity></tptz:ContinuousMove>"
    )
    return _envelope(body, user, pw)


def build_stop(token: str, user: str | None, pw: str | None) -> str:
    tok = _xml_escape(token)
    body = (
        f'<tptz:Stop xmlns:tptz="{_NS_PTZ}"><tptz:ProfileToken>{tok}</tptz:ProfileToken>'
        "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
    )
    return _envelope(body, user, pw)


def build_get_presets(token: str, user: str | None, pw: str | None) -> str:
    tok = _xml_escape(token)
    body = (f'<tptz:GetPresets xmlns:tptz="{_NS_PTZ}">'
            f"<tptz:ProfileToken>{tok}</tptz:ProfileToken></tptz:GetPresets>")
    return _envelope(body, user, pw)


def build_goto_preset(token: str, preset: str, user: str | None, pw: str | None) -> str:
    tok = _xml_escape(token)
    pre = _xml_escape(preset)
    body = (
        f'<tptz:GotoPreset xmlns:tptz="{_NS_PTZ}"><tptz:ProfileToken>{tok}</tptz:ProfileToken>'
        f"<tptz:PresetToken>{pre}</tptz:PresetToken></tptz:GotoPreset>"
    )
    return _envelope(body, user, pw)


def build_get_status(token: str, user: str | None, pw: str | None) -> str:
    """GetStatus for a profile -- reads the camera's CURRENT pan/tilt/zoom position (the
    bearing seam for localize.ptz_bearing_floor_point). Control metadata only, no frame."""
    tok = _xml_escape(token)
    body = (f'<tptz:GetStatus xmlns:tptz="{_NS_PTZ}">'
            f"<tptz:ProfileToken>{tok}</tptz:ProfileToken></tptz:GetStatus>")
    return _envelope(body, user, pw)


# --------------------------------------------------------------------------- #
# Response parsers (namespace-agnostic, XXE-safe -- never raise).
# --------------------------------------------------------------------------- #

def parse_ptz_service(xml_text: str) -> str | None:
    """From a GetServicesResponse, return the XAddr of the service whose Namespace
    contains 'ptz/wsdl', else None (caller applies the fallback path). Length-capped."""
    root = _safe_root(xml_text)
    if root is None:
        return None
    for svc in _iter_local(root, "Service"):
        ns = _first_text(svc, "Namespace") or ""
        if "ptz/wsdl" in ns:
            xaddr = _first_text(svc, "XAddr")
            if xaddr:
                return xaddr[:2048]
    return None


def parse_presets(xml_text: str) -> list[dict]:
    """From a GetPresetsResponse, return [{token, name}]. Tokenless entries skipped;
    token/name truncated to 100; capped at 64 entries. Namespace-agnostic; never raises."""
    root = _safe_root(xml_text)
    if root is None:
        return []
    out: list[dict] = []
    for el in _iter_local(root, "Preset"):
        token = el.get("token") or el.get("Token")
        if not token:
            continue
        name = _first_text(el, "Name")
        out.append({"token": token[:100], "name": (name[:100] if name else None)})
        if len(out) >= 64:
            break
    return out


def _finite_attr(el, name: str) -> float | None:
    """A finite float attribute value, or None (missing / non-numeric / NaN / inf)."""
    v = el.get(name)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_ptz_status(xml_text: str) -> dict | None:
    """GetStatusResponse -> {'pan','tilt'[,'zoom']} NORMALIZED ONVIF position (PanTilt
    x,y + Zoom x attrs), or None. Namespace/XXE-safe; never raises; non-finite refused.
    Range is per-model; radian conversion (`normalized_pan_tilt_to_radians`) NOT VERIFIED."""
    root = _safe_root(xml_text)
    if root is None:
        return None
    pan = tilt = None
    for pt in _iter_local(root, "PanTilt"):        # skips a MoveStatus PanTilt (no x/y)
        px, py = _finite_attr(pt, "x"), _finite_attr(pt, "y")
        if px is not None and py is not None:
            pan, tilt = px, py
            break
    if pan is None or tilt is None:
        return None
    out = {"pan": pan, "tilt": tilt}
    for z in _iter_local(root, "Zoom"):
        zv = _finite_attr(z, "x")
        if zv is not None:
            out["zoom"] = zv
        break
    return out


def _is_fault(xml_text: str) -> bool:
    """True if the SOAP response carries a <Fault>. A fault means the move/stop/goto
    did not take -- treated as a False result (never raised, never surfaced verbatim)."""
    root = _safe_root(xml_text)
    if root is None:
        return True   # unparseable response -> treat as failure, do not claim success
    for _ in _iter_local(root, "Fault"):
        return True
    return False


# --------------------------------------------------------------------------- #
# Default SSRF-guarded transport (mirrors onvif._default_soap, adds the SOAPAction).
# --------------------------------------------------------------------------- #

async def _default_ptz_soap(url: str, body: str, action: str,
                            timeout: float) -> str:  # pragma: no cover
    """Real ONVIF PTZ SOAP POST with the SOAPAction in the Content-Type (the Tapo
    requires it -- onvif._default_soap omits it). Redirects blocked; re-checks
    `_xaddr_ok(url)` as defence-in-depth; bounded read; never contacts a non-LAN host."""
    if not _xaddr_ok(url):
        raise ValueError("refusing PTZ SOAP to non-LAN host")
    loop = asyncio.get_event_loop()
    ct = f'application/soap+xml; charset=utf-8; action="{action}"'

    def _post() -> str:
        req = _urllib_request.Request(
            url, data=body.encode("utf-8"),
            headers={"Content-Type": ct}, method="POST")
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(_SOAP_READ_CAP).decode("utf-8", errors="replace")

    return await loop.run_in_executor(None, _post)


# --------------------------------------------------------------------------- #
# The actuator.
# --------------------------------------------------------------------------- #

class CameraPTZ:
    """Resolve + actuate ONVIF PTZ for a STORED camera. `soap` is the injectable
    transport (default: the real HTTP POST); tests inject canned responses.

    Credentials live ONLY in method locals (parsed from the passed rtsp_url) -- this
    object never stores or logs a url/user/pass. The discovery cache holds only the
    (already LAN-validated) PTZ-service URL + a profile token, both non-secret."""

    def __init__(self, soap: PtzSoap | None = None, autostop_s: float = _AUTOSTOP_S):
        self._soap = soap or _default_ptz_soap
        self._autostop_s = autostop_s
        # name -> (ptz_service_url, profile_token). No creds cached.
        self._cache: dict[str, tuple[str, str]] = {}
        # name -> pending auto-stop task (runaway-slew guard).
        self._timers: dict[str, asyncio.Task] = {}

    # -- credential/endpoint parsing (returns creds for in-method use ONLY) -- #
    @staticmethod
    def _endpoints(rtsp_url: str) -> tuple[str, str | None, str | None] | None:
        """Parse (host, user, pw) from the stored rtsp_url. Returns None if the host is
        not a LAN IP literal (SSRF: refuse -- never contact a non-LAN camera)."""
        try:
            p = urllib.parse.urlsplit(rtsp_url)
        except ValueError:
            return None
        host = p.hostname
        if not host or not _is_lan_ip(host):
            return None
        return host, p.username, p.password

    async def discover(self, name: str, rtsp_url: str,
                       timeout: float = 4.0) -> tuple[str, str] | None:
        """Resolve (ptz_service_url, profile_token) for `name`, cached. Returns None for
        a non-LAN camera, a camera with no ONVIF profile, or any transport failure."""
        if name in self._cache:
            return self._cache[name]
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return None
        host, user, pw = ep
        dev_url = f"http://{host}:{_ONVIF_PORT}{_DEVICE_PATH}"
        if not _xaddr_ok(dev_url):
            return None
        try:
            # Profile token (reuse onvif.build_get_profiles + parse_profiles).
            from .sources.onvif import build_get_profiles, parse_profiles
            prof_xml = await self._soap(
                dev_url, build_get_profiles(user, pw), f"{_NS_DEVICE}/GetProfiles", timeout)
            profiles = parse_profiles(prof_xml)
            if not profiles:
                return None
            token = profiles[0]["token"]
            # PTZ service URL via GetServices, else the proven fallback path.
            svc_xml = await self._soap(
                dev_url, build_get_services(user, pw), _ACT_GET_SERVICES, timeout)
            ptz_url = parse_ptz_service(svc_xml)
            if not ptz_url or not _xaddr_ok(ptz_url):
                ptz_url = f"http://{host}:{_ONVIF_PORT}{_PTZ_FALLBACK}"
            if not _xaddr_ok(ptz_url):
                return None
        except Exception:
            _LOG.warning("ptz: discovery SOAP failed for a LAN camera")
            return None
        self._cache[name] = (ptz_url, token)
        return self._cache[name]

    async def continuous_move(self, name: str, rtsp_url: str,
                              x: float, y: float, zoom: float = 0.0,
                              timeout: float = 4.0) -> bool:
        """Slew the camera (server clamps x/y/zoom to [-1,1]) and arm the auto-stop
        guard. Returns True only on a non-fault response."""
        x, y, zoom = _clamp_unit(x), _clamp_unit(y), _clamp_unit(zoom)
        resolved = await self.discover(name, rtsp_url, timeout)
        if resolved is None:
            return False
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return False
        _host, user, pw = ep
        try:
            resp = await self._soap(
                ptz_url, build_continuous_move(token, x, y, zoom, user, pw),
                _ACT_CONTINUOUS, timeout)
        except Exception:
            _LOG.warning("ptz: ContinuousMove SOAP failed for a LAN camera")
            return False
        if _is_fault(resp):
            return False
        self._arm_autostop(name, rtsp_url)
        return True

    async def stop(self, name: str, rtsp_url: str, timeout: float = 4.0) -> bool:
        """Stop all PTZ motion and cancel the auto-stop guard. Idempotent."""
        self._cancel_autostop(name)
        resolved = await self.discover(name, rtsp_url, timeout)
        if resolved is None:
            return False
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return False
        _host, user, pw = ep
        try:
            resp = await self._soap(
                ptz_url, build_stop(token, user, pw), _ACT_STOP, timeout)
        except Exception:
            _LOG.warning("ptz: Stop SOAP failed for a LAN camera")
            return False
        return not _is_fault(resp)

    async def get_presets(self, name: str, rtsp_url: str,
                          timeout: float = 4.0) -> list[dict]:
        """Return the camera's stored PTZ presets [{token, name}], or [] on any failure."""
        resolved = await self.discover(name, rtsp_url, timeout)
        if resolved is None:
            return []
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return []
        _host, user, pw = ep
        try:
            resp = await self._soap(
                ptz_url, build_get_presets(token, user, pw), _ACT_GET_PRESETS, timeout)
        except Exception:
            _LOG.warning("ptz: GetPresets SOAP failed for a LAN camera")
            return []
        return parse_presets(resp)

    async def goto_preset(self, name: str, rtsp_url: str, preset: str,
                          timeout: float = 4.0) -> bool:
        """Recall a stored preset. Returns True only on a non-fault response."""
        resolved = await self.discover(name, rtsp_url, timeout)
        if resolved is None:
            return False
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return False
        _host, user, pw = ep
        try:
            resp = await self._soap(
                ptz_url, build_goto_preset(token, preset, user, pw),
                _ACT_GOTO_PRESET, timeout)
        except Exception:
            _LOG.warning("ptz: GotoPreset SOAP failed for a LAN camera")
            return False
        return not _is_fault(resp)

    async def capabilities(self, name: str, rtsp_url: str) -> dict:
        """{'ptz': bool} -- does this camera expose PTZ. A non-PTZ/offline/non-LAN
        camera is simply False, never a 500 (all exceptions swallowed)."""
        try:
            return {"ptz": await self.discover(name, rtsp_url) is not None}
        except Exception:
            _LOG.warning("ptz: capability probe failed for a camera")
            return {"ptz": False}

    async def get_status(self, name: str, rtsp_url: str,
                         timeout: float = 4.0) -> dict | None:
        """Read the camera's current PTZ position -> {'pan','tilt'[,'zoom']} (normalized
        ONVIF units), or None on a non-LAN/offline/faulting/non-PTZ camera. The BEARING
        SEAM for person-localization: a centred auto-track's pan/tilt is the bearing to
        the person. Reads ONLY ONVIF control metadata -- NO frame (ADR-0002); creds come
        only from the stored rtsp_url and never appear in the result."""
        resolved = await self.discover(name, rtsp_url, timeout)
        if resolved is None:
            return None
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return None
        _host, user, pw = ep
        try:
            resp = await self._soap(
                ptz_url, build_get_status(token, user, pw), _ACT_GET_STATUS, timeout)
        except Exception:
            _LOG.warning("ptz: GetStatus SOAP failed for a LAN camera")
            return None
        if _is_fault(resp):
            return None
        return parse_ptz_status(resp)

    # -- runaway-slew guard -- #
    def _arm_autostop(self, name: str, rtsp_url: str) -> None:
        self._cancel_autostop(name)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timers[name] = loop.create_task(self._autostop_after(name, rtsp_url))

    def _cancel_autostop(self, name: str) -> None:
        task = self._timers.pop(name, None)
        if task is not None:
            task.cancel()

    async def _autostop_after(self, name: str, rtsp_url: str) -> None:
        try:
            await asyncio.sleep(self._autostop_s)
        except asyncio.CancelledError:
            return
        # Fire a stop directly (do NOT re-cancel a timer we just consumed).
        self._timers.pop(name, None)
        resolved = self._cache.get(name) or await self.discover(name, rtsp_url)
        if resolved is None:
            return
        ptz_url, token = resolved
        ep = self._endpoints(rtsp_url)
        if ep is None:
            return
        _host, user, pw = ep
        try:
            await self._soap(ptz_url, build_stop(token, user, pw), _ACT_STOP, 4.0)
        except Exception:
            _LOG.warning("ptz: auto-stop Stop SOAP failed for a LAN camera")
