"""Wavr reverse-DNS hostname resolver -- gateway-anchored PTR lookups.

Turns each LAN device IP into a hostname by sending a reverse-DNS (PTR,
in-addr.arpa) query EXPLICITLY to the LAN gateway own DNS server -- never
socket.gethostbyaddr() / the OS default resolver. On a machine with a VPN
tunnel active, the OS resolver is routed through the VPN and answers almost
nothing about the physical LAN (competitive analysis found ~1/8 vs ~5/8 hit
rate); pinning the query at the router the ARP scan already lives behind is
what makes the already-tested hostname classifier (wavr.data.deviceclass
hostname_type) actually fire on real devices.

The resolved names feed the EXISTING hostnames= parameter of
wavr.netinventory.build_inventory (mac -> hostname), so the recog.py hostname
signal (weight 0.65) gets real input with zero change to the fusion engine.

LOCAL-ONLY / ZERO CLOUD EGRESS: every query is a single UDP datagram to a LAN
address -- the gateway (internet_monitor.guess_gateway, the ".1" of the local
/24). Wavr itself never talks to a public resolver here; whether the gateway
recursively forwards a miss upstream is the router behaviour, not Wavr egress
(contrast wavr.health_check, which deliberately DOES ping public resolvers and
says so). Unlike the NetBIOS/SNMP active probes, this touches no neighbour host
at all -- only the router the operator already owns -- so there is no
shared-subnet host-probing footprint to mitigate.

Zero new dependencies (stdlib socket/struct only) and zero elevated privileges
(a plain unprivileged UDP socket, no raw sockets). Every network touch is
behind an injectable transport (QueryFn) so the whole module is mock-tested
with plain bytes and zero real network. All parsing is defensive: a
malformed/truncated/hostile response yields None, never raises.
"""
from __future__ import annotations

import asyncio
import random
import socket
import struct
from typing import Awaitable, Callable

from wavr.internet_monitor import guess_gateway

_DNS_PORT = 53
_TYPE_PTR = 12
_CLASS_IN = 1
# DNS header flags for a standard query with recursion desired (QR=0, opcode=0,
# RD=1) -- the gateway is a forwarding resolver, so ask it to recurse.
_FLAGS_RD = 0x0100
# Defensive bound on a returned hostname (a DNS name is <=255 octets by spec;
# mirrors recog._MAX_FIELD_LEN: truncate hostile input, never reject).
_MAX_HOSTNAME_LEN = 255

# Injectable transport: (ip, server, timeout) -> hostname|None. The default
# sends a real UDP PTR query to `server`; tests inject a canned async function
# so no socket is ever opened.
QueryFn = Callable[[str, str, float], Awaitable["str | None"]]


def _reverse_name(ip: str) -> str | None:
    """The in-addr.arpa reverse-lookup name for a dotted IPv4 address, e.g.
    ``192.168.1.42`` -> ``42.1.168.192.in-addr.arpa``. None if `ip` is not a
    valid dotted IPv4 quad. Pure/offline."""
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    for octet in parts:
        if not octet.isdigit() or not 0 <= int(octet) <= 255:
            return None
    return ".".join(reversed(parts)) + ".in-addr.arpa"


def _encode_qname(name: str) -> bytes:
    """Encode a dotted DNS name as length-prefixed labels + a terminating zero.
    Over-long labels are truncated to the 63-octet DNS limit rather than
    rejected (the reverse names we build are always well within it)."""
    out = bytearray()
    for label in name.split("."):
        label_b = label.encode("ascii", errors="ignore")[:63]
        out.append(len(label_b))
        out.extend(label_b)
    out.append(0)
    return bytes(out)


def build_ptr_query(ip: str, txid: int = 0) -> bytes | None:
    """Build a DNS PTR (reverse-lookup) query datagram for `ip`, or None if
    `ip` is not a valid dotted IPv4 address. `txid` is the 16-bit transaction
    id echoed back in the response (used to reject a mismatched reply).
    Pure/offline."""
    name = _reverse_name(ip)
    if name is None:
        return None
    header = struct.pack(">HHHHHH", txid & 0xFFFF, _FLAGS_RD, 1, 0, 0, 0)
    question = _encode_qname(name) + struct.pack(">HH", _TYPE_PTR, _CLASS_IN)
    return header + question


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name at `offset`, returning
    (dotted name, resume-offset). Compression-pointer safe and loop-safe on a
    malformed packet -- mirrors wavr.sources.mdns._read_name."""
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
            if hops > 64 or pointer >= len(data):   # never loop on a hostile packet
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


def parse_ptr_response(data: bytes, txid: int = 0) -> str | None:
    """Extract the first PTR hostname from a DNS response datagram, or None.

    Returns None for a wrong transaction id, a non-answer (NXDOMAIN/NODATA,
    ancount 0), or any malformed/truncated/hostile packet -- it never raises.
    The trailing root dot is stripped; the result is length-bounded."""
    if len(data) < 12:
        return None
    try:
        rid, _flags, qdcount, ancount, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    except struct.error:
        return None
    if rid != (txid & 0xFFFF):
        return None   # reply for a different query -- ignore
    pos = 12
    for _ in range(qdcount):
        _, pos = _read_name(data, pos)
        pos += 4      # QTYPE + QCLASS
        if pos > len(data):
            return None
    for _ in range(ancount):
        if pos >= len(data):
            break
        _owner, pos = _read_name(data, pos)
        if pos + 10 > len(data):
            break
        try:
            rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[pos:pos + 10])
        except struct.error:
            break
        pos += 10
        if pos + rdlength > len(data):
            break
        if rtype == _TYPE_PTR:
            name, _ = _read_name(data, pos)
            host = name.rstrip(".")
            if host:
                return host[:_MAX_HOSTNAME_LEN]
        pos += rdlength
    return None


async def _default_query(ip: str, server: str, timeout: float) -> str | None:
    """Default real transport: send ONE UDP PTR query for `ip` to `server:53`
    and return the resolved hostname, or None. Read-only single datagram; never
    raises (any socket error / timeout / parse failure -> None). Runs the
    blocking socket in a thread executor for cross-platform portability (same
    approach as wavr.sources.mdns._default_listen)."""
    txid = random.getrandbits(16)
    query = build_ptr_query(ip, txid)
    if query is None:
        return None

    def _blocking() -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(query, (server, _DNS_PORT))
            data, _addr = sock.recvfrom(4096)
        except OSError:
            return None
        finally:
            sock.close()
        return parse_ptr_response(data, txid)

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _blocking)
    except Exception:
        return None


async def resolve_hostnames(entries: "list[tuple[str, str]]", server: str | None = None,
                            query: QueryFn | None = None, timeout: float = 1.0,
                            concurrency: int = 16) -> dict[str, str]:
    """Reverse-resolve each ``(ip, mac)`` entry IP to a hostname and return
    ``{mac: hostname}`` for the ones that resolved (unresolved IPs are simply
    omitted -- an absent hostname is honest, never a guessed one).

    Every PTR query is sent to the LAN gateway DNS `server` (default:
    ``guess_gateway()`` -- the ".1" of the local /24). If the gateway cannot be
    determined, returns ``{}`` (no hostnames rather than a wrong resolver).
    `query` is the injectable transport (default: a real UDP query); tests hand
    in a canned async function. Concurrency is bounded so a large /24 cannot
    open hundreds of sockets at once. MAC keys are normalized to lowercase colon
    form so they line up with the wavr.netinventory inventory keys.

    Shape matches the `resolve` hook of wavr.netinventory.scan_inventory, whose
    output feeds straight into the existing `hostnames=` build parameter."""
    server = server or guess_gateway()
    if not server:
        return {}
    query = query or _default_query
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(ip: str, mac: str) -> tuple[str, "str | None"]:
        async with sem:
            try:
                host = await query(ip, server, timeout)
            except Exception:
                host = None
        return mac.replace("-", ":").lower(), host

    pairs = [(ip, mac) for ip, mac in entries if ip]
    results = await asyncio.gather(*(_one(ip, mac) for ip, mac in pairs))
    return {mac: host for mac, host in results if host}
