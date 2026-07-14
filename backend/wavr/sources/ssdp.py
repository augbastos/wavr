"""Wavr SSDP/UPnP passive collector -- richest make/model/serial source.

Passively LISTENS on the standard SSDP multicast group (239.255.255.250:1900)
for `NOTIFY ssdp:alive` announcements devices already broadcast on their own
(routers, smart TVs, printers, media renderers/servers, IGDs...). Optionally
(separately opt-in, see `fetch_location`) issues ONE same-LAN unicast HTTP GET
of the device's own advertised LOCATION URL to read its UPnP device
description XML -- the single richest local make/model/serial source in the
whole discovery pipeline. No cloud, ever: the LOC-XML fetch is guarded to
LAN-local IP literals only (`_is_lan_location`), so a hostile/misconfigured
LAN device cannot use the LOCATION header to make Wavr call out to the
internet.

Produces a per-host dict shaped for wavr.recog's `upnp` self-description hook:
    {"device_type": taxonomy?, "make": str?, "model": str?, "os": str?}
(recog.py itself only ever reads those four keys off this dict; `friendly_name`
-- the device's own advertised UPnP name -- is instead consumed one layer up,
by wavr.netinventory.apply_recognition, to fill Device.hostname when no
DHCP-fp/PTR-resolved name is already known, same convention as sources.mdns's
`hostname` field. `location`/`server`/`usn`/`target`/`serial` remain extra
evidence for a future richer inventory/explain view.)

OPT-IN, default OFF -- this module does not read the environment itself (same
seam as every Wavr source); the integration step wires a `WAVR_NET_SSDP` flag
gating whether `SSDPCollector` is ever constructed, and SHOULD wire the
LOC-XML fetch behind its OWN separate flag (recommended: `WAVR_NET_SSDP_LOCATION`,
default off) since it is a strictly more active probe than passive listening
alone (domain-knowledge rule: active probing is opt-in on top of opt-in).

Device-type inference precedence (conservative -- same ethos as sources.mdns):
  1. hostname_type() (wavr.data.deviceclass's existing regex table) run
     against friendlyName, then modelName, then the SERVER header -- catches
     "webOS"/"Tizen" smart TVs, `Bravia`, printer models, router brand names.
  2. A tiny table of device-type URNs whose PURPOSE is unambiguous regardless
     of vendor (InternetGatewayDevice family -> router, Printer -> printer,
     MediaServer -> nas).
  3. Otherwise left unset -- e.g. a bare MediaRenderer URN (TVs, AVRs, and
     DLNA speakers all implement it) is NOT guessed past without a stronger
     signal, matching the "never overclaim" rule.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import ipaddress
import re
import socket
import struct
import urllib.parse
import urllib.request as _urllib_request
from typing import AsyncIterator, Awaitable, Callable

from wavr.data.deviceclass import hostname_type
from wavr.sources._dhcp_raw import open_with_timeout

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900

PacketSource = Callable[[], AsyncIterator[tuple[bytes, str]]]
LocationFetcher = Callable[[str], Awaitable[str]]

# Defensive cap on packets processed in one collect() window -- see
# sources.mdns's identical rationale (bounds a multicast-flood LAN host).
_MAX_PACKETS_PER_WINDOW = 2000

_URN_RE = re.compile(r"device:([A-Za-z0-9_-]+):\d+", re.IGNORECASE)

# Device-type URNs whose real-world purpose is unambiguous regardless of
# vendor/model. Anything not listed here (MediaRenderer, BasicDevice, ...) is
# deliberately left unmapped -- see module docstring.
_URN_DEVICE_TYPE: dict[str, str] = {
    "internetgatewaydevice": "router",
    "wandevice": "router",
    "wanconnectiondevice": "router",
    "printer": "printer",
    "mediaserver": "nas",
}

# SERVER header commonly embeds "<OS>/<ver> UPnP/<ver> <product>/<ver>" per
# UPnP convention, but the OS token itself is free-form text -- rather than
# trying to split on "/" (fragile: "Windows 10/10.0..." breaks that), just
# look for a small set of recognizable OS names anywhere in the string.
_OS_HINTS: tuple[tuple[str, str], ...] = (
    ("windows", "Windows"), ("linux", "Linux"), ("darwin", "Darwin"),
    ("freebsd", "FreeBSD"), ("tizen", "Tizen"), ("webos", "webOS"),
    ("android", "Android"),
)


def _os_from_server(server: str | None) -> str | None:
    if not server:
        return None
    low = server.lower()
    for needle, name in _OS_HINTS:
        if needle in low:
            return name
    return None


def _device_type_from_urn(target: str | None) -> str | None:
    if not target:
        return None
    m = _URN_RE.search(target)
    key = (m.group(1) if m else target).strip().lower()
    return _URN_DEVICE_TYPE.get(key)


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            headers[key.upper()] = value.strip()
    return headers


def parse_ssdp_packet(data: bytes) -> dict | None:
    """Parse one SSDP datagram. Returns None for anything that is not a
    device-identity announcement worth acting on:
      - M-SEARCH REQUESTS (a query, not a description -- passive means we
        never even send these, but other LAN clients' queries also cross the
        multicast group and must be ignored, not mistaken for a device).
      - NOTIFY ssdp:byebye (the device is announcing it is LEAVING -- no
        positive identity value; treating it as a fresh sighting would be
        misleading).
    Never raises -- malformed/truncated/hostile datagrams yield None."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0].strip():
        return None
    start_line = lines[0].strip().upper()
    if start_line.startswith("M-SEARCH"):
        return None
    headers = _parse_headers(text)
    if headers.get("NTS", "").strip().lower() == "ssdp:byebye":
        return None
    location = headers.get("LOCATION") or None
    server = headers.get("SERVER") or None
    target = headers.get("ST") or headers.get("NT") or None
    usn = headers.get("USN") or None
    if not (location or server or target or usn):
        return None
    return {"location": location, "server": server, "target": target, "usn": usn}


def parse_upnp_description(xml_text: str) -> dict:
    """Parse a UPnP device-description XML document (the LOCATION target) --
    namespace-agnostic tag matching so this works regardless of the default
    xmlns the device declares. Returns {} on anything unparsable/suspicious
    -- never raises; a malformed or hostile response from a LAN device must
    never crash the collector.

    Defensive (XXE/billion-laughs -- stdlib ElementTree does not sandbox
    DTDs): any DOCTYPE declaration is rejected OUTRIGHT before parsing, the
    OWASP XXE-Prevention "disallow DTDs entirely" mitigation -- a real UPnP
    device-description document never legitimately has one, and with no
    DOCTYPE at all there is no way to declare an ENTITY (internal or
    external), so this is a strict superset of "block external entities"
    with zero new runtime dependency (`defusedxml` was considered and
    rejected here: this task is scoped to new source files only, and the
    DOCTYPE-reject rule already closes the same hole defusedxml targets)."""
    if "<!DOCTYPE" in xml_text:
        return {}
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    wanted = {"friendlyName", "manufacturer", "modelName", "modelNumber",
              "serialNumber", "deviceType"}
    found: dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.rpartition("}")[2]
        if tag in wanted and tag not in found and el.text and el.text.strip():
            found[tag] = el.text.strip()
    return {
        "friendly_name": found.get("friendlyName"),
        "manufacturer": found.get("manufacturer"),
        "model_name": found.get("modelName"),
        "model_number": found.get("modelNumber"),
        "serial_number": found.get("serialNumber"),
        "device_type_urn": found.get("deviceType"),
    }


_TUNNELED_PUBLIC_IPV6 = (
    ipaddress.ip_network("2002::/16"),   # 6to4 -- embeds a public IPv4 host
    ipaddress.ip_network("2001::/32"),   # Teredo -- embeds a public IPv4 host
)


def _is_lan_location(url: str) -> bool:
    """True only if `url`'s host is a literal private/loopback/link-local IP
    address -- the SSRF guard for the optional LOC-XML fetch. A DNS hostname
    (not a bare IP literal) is treated as UNSAFE and refused: without a
    scoped local resolver there is no guarantee it resolves on-LAN, and
    Wavr's zero-cloud-egress invariant means never taking that risk on a
    string an untrusted LAN device chose.

    6to4 (2002::/16) and Teredo (2001::/32) are explicitly rejected even
    though stdlib `ipaddress` reports `is_private is True` for both: each
    encodes/tunnels an arbitrary PUBLIC IPv4 endpoint, so accepting them
    would let an unauthenticated LAN SSDP NOTIFY point Wavr's LOC-XML GET at
    a real off-LAN host -- a zero-egress bypass despite passing the literal
    `is_private` check."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http":
            return False
        host = parsed.hostname
        if not host:
            return False
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if any(ip in net for net in _TUNNELED_PUBLIC_IPV6):
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


class _NoRedirect(_urllib_request.HTTPRedirectHandler):
    """Blocks HTTP redirects entirely. Hostile self-review fix: urlopen()
    follows redirects by DEFAULT, which would let a malicious/misconfigured
    LAN device's description.xml response 302 us to an external URL --
    completely bypassing `_is_lan_location`'s check of the ORIGINAL LOCATION.
    Returning None here tells urllib to treat the redirect response as
    terminal (no follow, no exception, no external request ever made)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = _urllib_request.build_opener(_NoRedirect)


async def _default_fetch(location: str) -> str:
    """Default real transport: ONE unicast HTTP GET of the device's own
    published LOCATION URL, with redirects disabled (see `_NoRedirect`).
    Callers must already have checked `_is_lan_location`. Stdlib urllib only,
    capped read size + short timeout so one slow/unresponsive device can't
    hang the collector."""
    loop = asyncio.get_event_loop()

    def _get() -> str:
        with _NO_REDIRECT_OPENER.open(location, timeout=2.0) as resp:  # noqa: S310
            return resp.read(65536).decode("utf-8", errors="replace")

    return await loop.run_in_executor(None, _get)


def _open_multicast_socket(group: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = struct.pack("4sl", socket.inet_aton(group), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


async def _default_listen() -> AsyncIterator[tuple[bytes, str]]:
    """Default real transport: join the SSDP multicast group and yield every
    datagram received (READ-ONLY -- never sends an M-SEARCH). See
    sources.mdns._default_listen for the identical executor-thread rationale.

    The socket()/bind()/IP_ADD_MEMBERSHIP open itself runs off the event loop
    via `_dhcp_raw.open_with_timeout` (same seam as dhcp/dhcp_fp/camera) --
    unlike the bounded per-recv executor call below, an unguarded synchronous
    bind() here could stall the event loop thread outright on a device where
    that call hangs instead of failing fast (see `_dhcp_raw` module
    docstring for the observed G9 field bug this pattern fixes)."""
    loop = asyncio.get_event_loop()
    sock = await open_with_timeout(
        functools.partial(_open_multicast_socket, SSDP_GROUP, SSDP_PORT),
        f"UDP/{SSDP_PORT} multicast bind (SSDP)")
    sock.settimeout(1.0)
    try:
        while True:
            try:
                data, (ip, _port) = await loop.run_in_executor(None, sock.recvfrom, 65535)
            except socket.timeout:
                continue
            yield data, ip
    finally:
        sock.close()


class SSDPCollector:
    """Passive SSDP/UPnP collector. `listen` is the injectable packet
    transport (default: the real multicast socket). `ip_to_mac` optionally
    maps source IP -> MAC so results key by MAC like every other recog
    signal (unmapped hosts key by IP instead). `fetch_location` (default
    OFF) additionally issues the same-LAN LOC-XML GET per discovered host;
    `fetcher` is its injectable transport (default: `_default_fetch`)."""

    def __init__(self, listen: PacketSource | None = None,
                 ip_to_mac: dict[str, str] | None = None,
                 fetch_location: bool = False,
                 fetcher: LocationFetcher | None = None):
        self._listen = listen or _default_listen
        self._ip_to_mac = {
            ip: mac.replace("-", ":").lower()
            for ip, mac in (ip_to_mac or {}).items()
        }
        self._fetch_location = fetch_location
        self._fetcher = fetcher or _default_fetch

    async def collect(self, duration: float = 5.0) -> dict[str, dict]:
        """Listen for up to `duration` seconds (returns sooner if the
        injected transport is finite, e.g. in tests), aggregate every parsed
        packet per source IP (optionally enriched by the LOC-XML fetch), and
        return {mac_or_ip: upnp_signal}."""
        packets = await self._drain(duration)
        hosts: dict[str, list[dict]] = {}
        for data, ip in packets:
            try:
                parsed = parse_ssdp_packet(data)
            except Exception:
                parsed = None
            if not parsed:
                continue
            hosts.setdefault(ip, []).append(parsed)

        out: dict[str, dict] = {}
        for ip, entries in hosts.items():
            merged = self._merge(entries)
            if self._fetch_location:
                await self._enrich_with_location(merged)
            key = self._ip_to_mac.get(ip, ip)
            out[key] = self._to_signal(merged)
        return out

    async def _drain(self, duration: float) -> list[tuple[bytes, str]]:
        packets: list[tuple[bytes, str]] = []
        agen = self._listen()

        async def _read_all() -> None:
            async for item in agen:
                if len(packets) >= _MAX_PACKETS_PER_WINDOW:
                    break
                packets.append(item)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_read_all(), timeout=duration)
        aclose = getattr(agen, "aclose", None)
        if aclose:
            with contextlib.suppress(Exception):
                await aclose()
        return packets

    def _merge(self, entries: list[dict]) -> dict:
        merged: dict = {}
        for entry in entries:
            for key, value in entry.items():
                if value and not merged.get(key):
                    merged[key] = value
        return merged

    async def _enrich_with_location(self, merged: dict) -> None:
        location = merged.get("location")
        if not location or not _is_lan_location(location):
            return
        try:
            xml_text = await self._fetcher(location)
            desc = parse_upnp_description(xml_text)
        except Exception:
            return  # LOC-XML fetch is best-effort; never break the collector
        for key, value in desc.items():
            if value:
                merged[key] = value

    def _to_signal(self, merged: dict) -> dict:
        friendly = merged.get("friendly_name")
        model = merged.get("model_name")
        server = merged.get("server")
        dtype = (hostname_type(friendly) or hostname_type(model)
                 or hostname_type(server) or _device_type_from_urn(merged.get("target")))
        signal: dict = {
            "location": merged.get("location"),
            "server": server,
            "usn": merged.get("usn"),
            "target": merged.get("target"),
            "friendly_name": friendly,
            "make": merged.get("manufacturer"),
            "model": model,
            "serial": merged.get("serial_number"),
            "os": _os_from_server(server),
        }
        if dtype:
            signal["device_type"] = dtype
        return signal
