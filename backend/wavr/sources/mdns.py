"""Wavr mDNS/Bonjour passive collector -- richest IoT/Apple identity signal.

Passively LISTENS on the standard mDNS multicast group (224.0.0.251:5353,
RFC 6762) for the service announcements devices already broadcast on their
own -- Wavr sends nothing (no queries, no probes), it only joins the
multicast group every mDNS responder on the LAN already talks to and reads.

Parses PTR/SRV/TXT records with a minimal stdlib DNS-message parser (no
`zeroconf`/third-party mDNS library -- keeps the transport 100% injectable
with plain bytes, so tests need zero real sockets) into a per-host dict
shaped for wavr.recog's `bonjour` self-description hook:
    {"device_type": taxonomy?, "make": str?, "model": str?, "os": str?}
(recog.py only ever reads those four keys; everything else in the dict --
hostname/services/txt/ip -- is extra evidence carried along for a future
richer inventory/explain view, ignored today but free to add later with no
recog change needed, same as every other signal key.)

OPT-IN, default OFF -- this module does not read the environment itself
(same injectable-everything seam as every other Wavr source); the
integration step wires a `WAVR_NET_MDNS` config flag that gates whether
`MDNSCollector` is ever constructed/started at all.

Device-type inference is DELIBERATELY conservative -- an "unknown" bonjour
signal is honest, a wrongly-confident self-description is a real inventory
bug (recog treats protocol self-description as HIGH confidence). Precedence:
  1. A TXT model string ("model"/"md"/"am") run through the SAME hostname
     regex table wavr.data.deviceclass already uses (`hostname_type`) --
     catches "Chromecast", "HomePod", "AppleTV", printer model names, etc.
  2. HomeKit's own public "ci" (Accessory Category) TXT field on `_hap._tcp`
     services -- a small, Apple-published enum (HAP spec), mapped only to
     the handful of Wavr taxonomy values it cleanly corresponds to.
  3. A tiny table of genuinely single-purpose service types (`_ipp._tcp` ->
     printer, `_raop._tcp` -> speaker).
  4. Otherwise left unset. A bare `_googlecast._tcp` (Chromecast dongle AND
     Google Home speaker both advertise it) is a known example of a case we
     deliberately do NOT guess past -- see test_ambiguous_googlecast_alone.

HARD LIMITS: read-only multicast socket, standard library only, per-packet
parsing is defensive (any malformed/hostile packet is skipped, never raises),
and the receive window is bounded per collection call (both in time via
`duration` and in packet COUNT via `_MAX_PACKETS_PER_WINDOW`, so a LAN host
flooding 5353 cannot pin the CPU parsing an unbounded backlog).
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from wavr.data.deviceclass import hostname_type

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

# Injectable transport: a zero-arg factory returning a FRESH async iterator of
# (raw_packet_bytes, source_ip) pairs each time it's called. The default binds
# the real multicast socket; tests inject a canned async generator function.
PacketSource = Callable[[], AsyncIterator[tuple[bytes, str]]]

# Defensive cap on packets processed in one collect() window -- bounds worst
# case CPU/memory if a LAN host floods the mDNS multicast group.
_MAX_PACKETS_PER_WINDOW = 2000

_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33

# HomeKit Accessory Protocol "ci" (Accessory Category) codes -- small, public,
# Apple-documented enum (HAP-Specification) -- mapped only where it lines up
# cleanly with a Wavr taxonomy value; anything else (fan, lock, thermostat,
# etc. -- no matching taxonomy slot yet) is left unmapped rather than forced
# into the nearest neighbour.
_HAP_CATEGORY_TYPE: dict[int, str] = {
    7: "smart_plug",        # Outlet
    8: "smart_plug",        # Switch
    10: "iot_sensor",       # Sensor
    17: "camera",           # IP Camera
    18: "camera",           # Video Doorbell
    32: "tv",               # Television
    34: "streaming_stick",  # Television Set-Top Box
}

# Service types whose PURPOSE is unambiguous regardless of model/vendor.
_SERVICE_DEVICE_TYPE: dict[str, str] = {
    "_ipp._tcp": "printer",
    "_ipps._tcp": "printer",
    "_printer._tcp": "printer",
    "_pdl-datastream._tcp": "printer",
    "_raop._tcp": "speaker",   # AirPlay audio-only receivers
}


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name starting at `offset`. Returns
    (dotted name, resume-offset) where resume-offset is where the CALLER's
    cursor should continue -- i.e. right after the terminating zero byte, OR
    right after the first compression pointer encountered, whichever comes
    first in the original stream (pointers may be chased further for the
    name itself without moving the caller's cursor past that point)."""
    labels: list[bytes] = []
    pos = offset
    jumped = False
    resume_at = offset
    hops = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos += 1
            if not jumped:
                resume_at = pos
            break
        if (length & 0xC0) == 0xC0:
            if pos + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                resume_at = pos + 2
            jumped = True
            hops += 1
            if hops > 64 or pointer >= len(data):   # never loop on a malformed packet
                break
            pos = pointer
            continue
        pos += 1
        labels.append(data[pos:pos + length])
        pos += length
        if not jumped:
            resume_at = pos
    name = ".".join(label.decode("utf-8", errors="replace") for label in labels)
    return name, resume_at


def parse_mdns_packet(data: bytes) -> dict:
    """Parse one mDNS response datagram into
    {"hostname": str|None, "services": set[str], "txt": dict[str, str]}.
    Never raises -- any malformed/truncated/hostile packet yields whatever
    was successfully parsed before the problem (defaults to all-empty)."""
    hostname: str | None = None
    services: set[str] = set()
    instances: set[str] = set()
    txt: dict[str, str] = {}

    if len(data) < 12:
        return {"hostname": None, "services": services, "txt": txt}

    try:
        qdcount, ancount, nscount, arcount = struct.unpack(">HHHH", data[4:12])
    except struct.error:
        return {"hostname": None, "services": services, "txt": txt}
    pos = 12

    for _ in range(qdcount):
        if pos >= len(data):
            return {"hostname": hostname, "services": services, "txt": txt}
        _, pos = _read_name(data, pos)
        pos += 4  # QTYPE + QCLASS
        if pos > len(data):
            return {"hostname": hostname, "services": services, "txt": txt}

    for _ in range(ancount + nscount + arcount):
        if pos >= len(data):
            break
        owner, pos = _read_name(data, pos)
        if pos + 10 > len(data):
            break
        try:
            rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[pos:pos + 10])
        except struct.error:
            break
        pos += 10
        rdata_offset = pos
        if rdata_offset + rdlength > len(data):
            break
        rdata = data[rdata_offset:rdata_offset + rdlength]
        owner_clean = owner.rstrip(".")

        if rtype == _TYPE_PTR:
            service = owner_clean.lower()
            if service.endswith(".local"):
                service = service[: -len(".local")]
            if service:
                services.add(service)
            target, _ = _read_name(data, rdata_offset)
            target_clean = target.rstrip(".")
            suffix = "." + owner_clean
            if owner_clean and target_clean.lower().endswith(suffix.lower()):
                instance = target_clean[: -len(suffix)]
                if instance:
                    instances.add(instance)
        elif rtype == _TYPE_SRV:
            if len(rdata) >= 6:
                target, _ = _read_name(data, rdata_offset + 6)
                target_clean = target.rstrip(".")
                if target_clean.lower().endswith(".local"):
                    hostname = target_clean[: -len(".local")]
                elif target_clean:
                    hostname = target_clean
        elif rtype == _TYPE_TXT:
            tpos = 0
            while tpos < len(rdata):
                length = rdata[tpos]
                tpos += 1
                chunk = rdata[tpos:tpos + length]
                tpos += length
                if not chunk:
                    continue
                key, sep, val = chunk.partition(b"=")
                key_s = key.decode("utf-8", errors="replace").lower()
                txt[key_s] = val.decode("utf-8", errors="replace") if sep else ""
        elif rtype == _TYPE_A:
            pass  # ip comes from the UDP sender address instead; no-op here

        pos = rdata_offset + rdlength

    if hostname is None and instances:
        hostname = sorted(instances)[0]

    return {"hostname": hostname, "services": services, "txt": txt}


def _infer_device_type(services: set[str], txt: dict[str, str]) -> str | None:
    model = txt.get("model") or txt.get("md") or txt.get("am")
    if model:
        guess = hostname_type(model)
        if guess:
            return guess
    if "_hap._tcp" in services:
        ci = txt.get("ci")
        if ci and ci.isdigit():
            guess = _HAP_CATEGORY_TYPE.get(int(ci))
            if guess:
                return guess
    for svc in services:
        if svc in _SERVICE_DEVICE_TYPE:
            return _SERVICE_DEVICE_TYPE[svc]
    return None


@dataclass
class _HostAgg:
    hostname: str | None = None
    services: set = field(default_factory=set)
    txt: dict = field(default_factory=dict)


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
    """Default real transport: join the mDNS multicast group and yield every
    datagram received (READ-ONLY -- never transmits a query). Uses a blocking
    socket handed to a thread executor (portable across platforms/Python
    versions, unlike relying on ProactorEventLoop UDP support on Windows). A
    1s recv timeout lets the loop notice cancellation (aclose()) promptly."""
    loop = asyncio.get_event_loop()
    sock = _open_multicast_socket(MDNS_GROUP, MDNS_PORT)
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


class MDNSCollector:
    """Passive mDNS/Bonjour collector. `listen` is the injectable packet
    transport (default: the real multicast socket). `ip_to_mac` optionally
    maps source IP -> MAC (e.g. from the ARP inventory) so results key by MAC
    like every other recog signal; hosts with no mapping key by their IP
    instead (still usable -- the integration step can re-key once ARP catches
    up, or the fusion layer can look up by whichever key is available)."""

    def __init__(self, listen: PacketSource | None = None,
                 ip_to_mac: dict[str, str] | None = None):
        self._listen = listen or _default_listen
        self._ip_to_mac = {
            ip: mac.replace("-", ":").lower()
            for ip, mac in (ip_to_mac or {}).items()
        }

    async def collect(self, duration: float = 5.0) -> dict[str, dict]:
        """Listen for up to `duration` seconds (returns sooner if the
        injected transport is finite, e.g. in tests), aggregate every parsed
        packet per source IP, and return {mac_or_ip: bonjour_signal}."""
        packets = await self._drain(duration)
        hosts: dict[str, _HostAgg] = {}
        for data, ip in packets:
            try:
                parsed = parse_mdns_packet(data)
            except Exception:
                continue  # one malformed packet must never kill the collector
            if not (parsed["hostname"] or parsed["services"] or parsed["txt"]):
                continue
            agg = hosts.setdefault(ip, _HostAgg())
            if parsed["hostname"]:
                agg.hostname = parsed["hostname"]
            agg.services |= parsed["services"]
            agg.txt.update(parsed["txt"])
        return self._to_signals(hosts)

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

    def _to_signals(self, hosts: dict[str, _HostAgg]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for ip, agg in hosts.items():
            model = agg.txt.get("model") or agg.txt.get("md") or agg.txt.get("am")
            # TXT keys are lowercased by the parser (DNS-SD keys are
            # case-insensitive by convention) -- look up lowercase here too.
            make = agg.txt.get("manufacturer") or agg.txt.get("usb_mfg")
            if not model:
                model = agg.txt.get("usb_mdl")
            signal: dict = {
                "ip": ip,
                "hostname": agg.hostname,
                "services": sorted(agg.services),
                "txt": dict(agg.txt),
                "make": make,
                "model": model,
                "os": None,   # honest -- mDNS TXT rarely gives a clean OS string
            }
            dtype = _infer_device_type(agg.services, agg.txt)
            if dtype:
                signal["device_type"] = dtype
            key = self._ip_to_mac.get(ip, ip)
            out[key] = signal
        return out
