"""Wavr ONVIF camera probe (A4.2) -- auto-discover LAN cameras + pre-fill their
RTSP URL for the rung-2 "add camera" form.

Today rung-2 camera setup is manual IP/RTSP entry. This module does ONVIF
WS-Discovery (SOAP `Probe` over UDP multicast 239.255.255.250:3702 for
`NetworkVideoTransmitter`) to enumerate the cameras on the local network, then --
for each discovered device -- unicast ONVIF SOAP `GetProfiles` + `GetStreamUri`
(optionally WS-UsernameToken-authed with per-request camera creds) to read the
`rtsp://` stream URI. It returns candidates; **it never auto-adds a camera** --
the user confirms via the existing `POST /api/cameras`, and cameras always boot OFF.

HARD INVARIANTS (this module holds all of them -- the guards mirror sources.ssdp
1:1, the proven in-tree reference):

  * SSRF-HARD. Both the device-service `XAddrs` URL (chosen by whatever LAN host
    answered the multicast Probe -- attacker-controllable) AND the `rtsp://` URL a
    camera returns from GetStreamUri (also attacker-controllable) are validated to a
    literal PRIVATE/loopback/link-local IP BEFORE any connection / before being
    surfaced. A DNS hostname is refused (no scoped local resolver -> no guarantee it
    resolves on-LAN, same policy as `ssdp._is_lan_location`). The cloud-metadata IPs
    169.254.169.254 / fd00:ec2::254 are explicitly denied on top (they are link-local
    and would otherwise pass). HTTP redirects are blocked (`_NoRedirect`) so a camera
    cannot 302 Wavr off-LAN after the host check. A returned URL that is not
    rtsp(s):// or whose host is not LAN is dropped, never returned.

  * NO CRASH ON HOSTILE XML. Every SOAP/ProbeMatch document is parsed
    defensively: any `<!DOCTYPE` is rejected OUTRIGHT before parsing (the OWASP
    "disallow DTDs entirely" XXE / billion-laughs mitigation -- stdlib ElementTree
    does not sandbox DTDs), input size is bounded, and ANY parse failure degrades to
    `[]`/`None`, never an exception that reaches the route.

  * CREDENTIALS STAY LOCAL. Camera user/pass arrive in the request body for THIS
    probe only, are used in-memory to build the WS-UsernameToken digest, and are
    NEVER persisted, NEVER logged, NEVER echoed in any response or error string. Any
    creds a camera embeds in its returned rtsp URL are masked via `_mask_rtsp` before
    the URL is surfaced.

  * OPT-IN. Active multicast probe + unicast SOAP => opt-in on top of opt-in. The
    route is gated by `WAVR_ONVIF_PROBE` (default OFF) + `require_local` CSRF.

The discovery + SOAP transports are INJECTABLE (`discover`/`soap`), the same seam as
sources.ssdp's `listen`/`fetcher`: tests drive canned ProbeMatch + SOAP bytes with
zero real sockets. No camera frame is ever fetched or persisted here -- this module
only ever reads discovery/description metadata, never a video frame.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import logging
import os
import re
import socket
import struct
import urllib.parse
import urllib.request as _urllib_request
import uuid
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable
from xml.sax.saxutils import escape as _xml_escape

from wavr.netaddr import is_lan_ip as _is_lan_ip

_LOG = logging.getLogger(__name__)

WS_DISCOVERY_GROUP = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

# Injectable transport seams (mirror sources.ssdp).
#   discover(targets, timeout) -> list[(datagram_bytes, src_ip)]
#   soap(url, body, timeout)   -> response xml str
Discoverer = Callable[[list[str] | None, float], Awaitable[list[tuple[bytes, str]]]]
SoapCaller = Callable[[str, str, float], Awaitable[str]]

# Defensive caps -- bound what a flooding / hostile LAN host can make us do.
_MAX_MATCHES = 64          # ProbeMatch datagrams processed per probe window
_MAX_PROFILES = 16         # GetStreamUri calls per camera
_MAX_DEVICES = 32          # unique LAN devices SOAP-probed per call (flood cap)
_PROBE_BUDGET_S = 30.0     # overall wall-clock ceiling for one probe() call
_MAX_XML_BYTES = 1_000_000  # reject oversized SOAP bodies before parsing
_SOAP_READ_CAP = 262_144   # bytes read from a SOAP HTTP response

# Same restriction as app._URL_SHAPE_RE: the URL is handed toward cv2.VideoCapture
# downstream, so only rtsp(s):// is ever surfaced (no http/file/etc. SSRF/LFI).
_RTSP_SHAPE_RE = re.compile(r"^rtsps?://.+", re.IGNORECASE)

# ONVIF / WS-Discovery / WS-Security namespaces (for building requests only; parsing
# is namespace-AGNOSTIC via local-tag matching, so a device's odd xmlns never breaks it).
_NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_NS_DISC = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_NS_ONVIF_NET = "http://www.onvif.org/ver10/network/wsdl"
_NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_NS_SCHEMA = "http://www.onvif.org/ver10/schema"
_NS_WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-wssecurity-secext-1.0.xsd")
_NS_WSU = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-wssecurity-utility-1.0.xsd")
_PW_DIGEST_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                   "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
_B64_ENCODING = ("http://docs.oasis-open.org/wss/2004/01/"
                 "oasis-200401-wss-soap-message-security-1.0#Base64Binary")


def onvif_probe_enabled() -> bool:
    """True only if WAVR_ONVIF_PROBE is explicitly enabled. OFF by default."""
    return os.getenv("WAVR_ONVIF_PROBE", "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# SSRF guards (reuse the ssdp policy verbatim; add the metadata-IP denylist).
# --------------------------------------------------------------------------- #

# `_is_lan_ip` is the shared wavr.netaddr.is_lan_ip (imported above). It lives in a
# shared module so there is ONE hardened implementation (literal-only + cloud-metadata
# denylist + IPv4-mapped-IPv6 normalization) reused by ONVIF, PTZ and the F3 camera-
# rebind route -- a second hand-maintained copy could drift and reopen the SSRF bypass.


def _host_of(url: str) -> str | None:
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def _xaddr_ok(url: str) -> bool:
    """The device-service URL is safe to contact: http(s) scheme + LAN-IP host."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return _is_lan_ip(parsed.hostname)


def _rtsp_ok(url: str) -> bool:
    """A camera-returned stream URL is safe to surface: rtsp(s):// + LAN-IP host.
    Rejects rtsp://attacker.example / file:// / http://169.254.169.254 / DNS hosts."""
    if not _RTSP_SHAPE_RE.match(url or ""):
        return False
    return _is_lan_ip(_host_of(url))


def _mask_rtsp(url: str) -> str:
    """Redact the password in an rtsp URL: rtsp://user:pw@host -> rtsp://user:***@host.
    Never raises: an unexpected shape is returned unchanged (self-contained copy of
    app._mask_rtsp so this source module has no dependency on app.py)."""
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


class _NoRedirect(_urllib_request.HTTPRedirectHandler):
    """Blocks HTTP redirects entirely (identical to ssdp._NoRedirect): urlopen()
    follows redirects by default, which would let a camera 302 the SOAP call to an
    external URL, bypassing the ORIGINAL XAddrs host check. Returning None makes
    urllib treat the redirect as terminal (no follow, no external request)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = _urllib_request.build_opener(_NoRedirect)


# --------------------------------------------------------------------------- #
# XML parsing -- namespace-agnostic, DOCTYPE-reject, never-raise.
# --------------------------------------------------------------------------- #

def _safe_root(xml_text: str) -> ET.Element | None:
    """Parse SOAP/XML defensively: reject any DOCTYPE (XXE / billion-laughs) and
    oversized bodies BEFORE parsing; return None on anything unparsable. Never
    raises -- a hostile/truncated camera response must never crash the probe."""
    if not xml_text or len(xml_text) > _MAX_XML_BYTES:
        return None
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return None
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _iter_local(root: ET.Element, name: str):
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def _first_text(root: ET.Element, name: str) -> str | None:
    for el in _iter_local(root, name):
        if el.text and el.text.strip():
            return el.text.strip()
    return None


def _scope_value(scopes: str | None, kind: str) -> str | None:
    """Pull the value of an ONVIF scope, e.g. kind='hardware' from
    'onvif://www.onvif.org/hardware/DS-2CD2032 onvif://www.onvif.org/name/HIK'."""
    if not scopes:
        return None
    needle = f"/{kind}/"
    for token in scopes.split():
        idx = token.find(needle)
        if idx != -1:
            raw = token[idx + len(needle):]
            if raw:
                return urllib.parse.unquote(raw)[:200]
    return None


def parse_probe_matches(data: bytes) -> list[dict]:
    """Parse one WS-Discovery ProbeMatches datagram into a list of candidate
    dicts {xaddr, name, hardware, location, types}. Namespace-agnostic; XXE-safe;
    never raises. `xaddr` is the FIRST url in the (possibly space-separated)
    XAddrs field -- host validation happens at the call site, not here."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return []
    root = _safe_root(text)
    if root is None:
        return []
    out: list[dict] = []
    for match in _iter_local(root, "ProbeMatch"):
        xaddrs = _first_text(match, "XAddrs")
        scopes = _first_text(match, "Scopes")
        types = _first_text(match, "Types")
        xaddr = xaddrs.split()[0] if xaddrs else None
        out.append({
            "xaddr": xaddr,
            "name": _scope_value(scopes, "name"),
            "hardware": _scope_value(scopes, "hardware"),
            "location": _scope_value(scopes, "location"),
            "types": types,
        })
        if len(out) >= _MAX_MATCHES:
            break
    return out


def parse_profiles(xml_text: str) -> list[dict]:
    """Parse a GetProfilesResponse into [{token, resolution}]. Namespace-agnostic;
    XXE-safe; never raises. `token` is required (skips tokenless entries)."""
    root = _safe_root(xml_text)
    if root is None:
        return []
    out: list[dict] = []
    for prof in _iter_local(root, "Profiles"):
        token = prof.get("token") or prof.get("Token")
        if not token:
            continue
        resolution = None
        for res in _iter_local(prof, "Resolution"):
            w = _first_text(res, "Width")
            h = _first_text(res, "Height")
            if w and h:
                resolution = f"{w}x{h}"
            break
        out.append({"token": token[:100], "resolution": resolution})
        if len(out) >= _MAX_PROFILES:
            break
    return out


def parse_stream_uri(xml_text: str) -> str | None:
    """Parse a GetStreamUriResponse -> the `<...:Uri>` text, or None. XXE-safe,
    never raises. The returned URL is NOT trusted here -- the caller re-validates
    it with `_rtsp_ok` before surfacing."""
    root = _safe_root(xml_text)
    if root is None:
        return None
    uri = _first_text(root, "Uri")
    return uri[:2048] if uri else None


# --------------------------------------------------------------------------- #
# SOAP request builders (WS-UsernameToken digest auth when creds supplied).
# --------------------------------------------------------------------------- #

def _security_header(username: str | None, password: str | None) -> str:
    """Build a WS-Security UsernameToken (PasswordDigest) header, or '' when no
    creds. The password is used ONLY to compute the SHA1 digest and never appears
    in the produced XML (WS-Security digest scheme). Username is XML-escaped."""
    if not username:
        return ""
    pwd = password or ""
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        f'<wsse:Security xmlns:wsse="{_NS_WSSE}" xmlns:wsu="{_NS_WSU}" '
        f's:mustUnderstand="1"><wsse:UsernameToken>'
        f"<wsse:Username>{_xml_escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{_PW_DIGEST_TYPE}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_B64_ENCODING}">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        f"</wsse:UsernameToken></wsse:Security>"
    )


def _envelope(body: str, username: str | None, password: str | None) -> str:
    header = _security_header(username, password)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{_NS_SOAP}">'
        f"<s:Header>{header}</s:Header>"
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


def build_probe() -> bytes:
    """WS-Discovery Probe for ONVIF NetworkVideoTransmitters (multicast payload)."""
    msg_id = f"uuid:{uuid.uuid4()}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{_NS_SOAP}" xmlns:a="{_NS_WSA}" '
        f'xmlns:d="{_NS_DISC}" xmlns:dn="{_NS_ONVIF_NET}">'
        f"<s:Header><a:MessageID>{msg_id}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        "</s:Header><s:Body><d:Probe>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></s:Body></s:Envelope>"
    ).encode()


def build_get_profiles(username: str | None, password: str | None) -> str:
    return _envelope(f'<trt:GetProfiles xmlns:trt="{_NS_MEDIA}"/>', username, password)


def build_get_stream_uri(token: str, username: str | None, password: str | None) -> str:
    # `token` comes from the camera's GetProfilesResponse -> XML-escape it so a
    # hostile token string cannot inject into the SOAP body.
    tok = _xml_escape(token)
    body = (
        f'<trt:GetStreamUri xmlns:trt="{_NS_MEDIA}" xmlns:tt="{_NS_SCHEMA}">'
        "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        "</trt:StreamSetup>"
        f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>"
    )
    return _envelope(body, username, password)


# --------------------------------------------------------------------------- #
# Default real transports (exercised only against real hardware; CI injects).
# --------------------------------------------------------------------------- #

async def _default_discover(targets: list[str] | None,
                            timeout: float) -> list[tuple[bytes, str]]:  # pragma: no cover
    """Real WS-Discovery: send a Probe to the multicast group (or unicast to each
    validated LAN target) and collect responses for up to `timeout`s. Any target
    that is not a LAN IP literal is dropped (never contacted). CI injects a canned
    `discover`, so this opens no socket in tests."""
    loop = asyncio.get_event_loop()

    def _run() -> list[tuple[bytes, str]]:
        probe = build_probe()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.settimeout(0.5)
        results: list[tuple[bytes, str]] = []
        try:
            dests = [(t, WS_DISCOVERY_PORT) for t in (targets or []) if _is_lan_ip(t)] \
                or [(WS_DISCOVERY_GROUP, WS_DISCOVERY_PORT)]
            for dest in dests:
                with contextlib.suppress(OSError):
                    sock.sendto(probe, dest)
            deadline = loop.time() + max(0.2, timeout)
            while loop.time() < deadline and len(results) < _MAX_MATCHES:
                try:
                    data, (ip, _p) = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                results.append((data, ip))
        finally:
            sock.close()
        return results

    return await loop.run_in_executor(None, _run)


async def _default_soap(url: str, body: str, timeout: float) -> str:  # pragma: no cover
    """Real unicast ONVIF SOAP POST, redirects disabled (`_NoRedirect`). Re-checks
    `_xaddr_ok(url)` as defence-in-depth (the caller already validated it). Stdlib
    urllib only; bounded read + short timeout. Never contacts a non-LAN host."""
    if not _xaddr_ok(url):
        raise ValueError("refusing SOAP call to non-LAN host")
    loop = asyncio.get_event_loop()

    def _post() -> str:
        req = _urllib_request.Request(
            url, data=body.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST")
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(_SOAP_READ_CAP).decode("utf-8", errors="replace")

    return await loop.run_in_executor(None, _post)


# --------------------------------------------------------------------------- #
# The probe.
# --------------------------------------------------------------------------- #

class ONVIFProbe:
    """Discover ONVIF cameras on the LAN + fetch their RTSP stream URI. `discover`
    and `soap` are injectable transports (defaults: the real socket / HTTP calls);
    tests pass canned ones for zero-network runs."""

    def __init__(self, discover: Discoverer | None = None,
                 soap: SoapCaller | None = None):
        self._discover = discover or _default_discover
        self._soap = soap or _default_soap

    async def probe(self, targets: list[str] | None = None,
                    username: str | None = None, password: str | None = None,
                    timeout: float = 3.0) -> dict:
        """Return {'cameras': [...], 'errors': [...]}. Each camera is
        {ip, name, make, model, rtsp_url (masked), profiles:[{token, resolution,
        rtsp_url (masked)}]}. Credentials never appear in the result or in any
        error entry. A non-LAN XAddrs or a non-LAN/non-rtsp stream URL is dropped,
        never contacted/surfaced."""
        # Validate any caller-supplied unicast targets up front (defence in depth;
        # the default transport also filters, but an injected one might not).
        safe_targets = [t for t in (targets or []) if _is_lan_ip(t)] or None
        try:
            datagrams = await self._discover(safe_targets, timeout)
        except Exception:
            _LOG.warning("onvif: discovery transport failed")
            return {"cameras": [], "errors": []}

        cameras: list[dict] = []
        errors: list[dict] = []
        seen: set[str] = set()
        probed = 0
        # Wall-clock ceiling: a hostile host can advertise thousands of unique
        # LAN-IP XAddrs, each fanning out to sequential GetProfiles/GetStreamUri
        # awaits. Cap both the number of devices SOAP-probed (_MAX_DEVICES) and
        # the total time; on budget exhaustion return the partial results.
        deadline = asyncio.get_event_loop().time() + _PROBE_BUDGET_S
        for data, _src_ip in list(datagrams)[:_MAX_MATCHES]:
            if probed >= _MAX_DEVICES or asyncio.get_event_loop().time() >= deadline:
                break
            for match in parse_probe_matches(data):
                if probed >= _MAX_DEVICES or asyncio.get_event_loop().time() >= deadline:
                    break
                xaddr = match.get("xaddr")
                if not xaddr or xaddr in seen:
                    continue
                seen.add(xaddr)
                host = _host_of(xaddr)
                if not _xaddr_ok(xaddr):
                    # SSRF: the device-service host is not on the local LAN (public /
                    # DNS / cloud-metadata). Report WITHOUT ever contacting it.
                    errors.append({"host": host or "?",
                                   "reason": "XAddrs host not on local LAN -- refused"})
                    continue
                probed += 1
                try:
                    cam = await self._probe_device(xaddr, match, username, password, timeout)
                except Exception:
                    # Never leak creds/exception detail; a bad camera is just an error row.
                    _LOG.warning("onvif: SOAP probe failed for a LAN host")
                    errors.append({"host": host, "reason": "SOAP probe failed"})
                    continue
                if cam:
                    cameras.append(cam)
                else:
                    errors.append({"host": host, "reason": "no usable RTSP profile"})
        return {"cameras": cameras, "errors": errors}

    async def _probe_device(self, xaddr: str, match: dict,
                            username: str | None, password: str | None,
                            timeout: float) -> dict | None:
        profiles_xml = await self._soap(xaddr, build_get_profiles(username, password), timeout)
        profiles = parse_profiles(profiles_xml)
        out_profiles: list[dict] = []
        for prof in profiles[:_MAX_PROFILES]:
            uri_xml = await self._soap(
                xaddr, build_get_stream_uri(prof["token"], username, password), timeout)
            uri = parse_stream_uri(uri_xml)
            if not uri or not _rtsp_ok(uri):
                # SSRF result-validation: drop a non-rtsp / non-LAN / DNS-host URL.
                continue
            out_profiles.append({
                "token": prof["token"],
                "resolution": prof.get("resolution"),
                "rtsp_url": _mask_rtsp(uri),   # creds (if any) masked before surfacing
            })
        if not out_profiles:
            return None
        return {
            "ip": _host_of(xaddr),
            "name": match.get("name"),
            "make": match.get("name"),        # ONVIF `name` scope = advisory make/label
            "model": match.get("hardware"),   # ONVIF `hardware` scope = advisory model
            "location": match.get("location"),
            "rtsp_url": out_profiles[0]["rtsp_url"],
            "profiles": out_profiles,
        }
