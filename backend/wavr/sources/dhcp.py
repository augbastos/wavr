"""Wavr DHCP passive/active collector -- feeds the rogue/multiple-DHCP-server
detector (`wavr.dhcp_monitor`).

Standard BOOTP/DHCP wire format (RFC 2131/2132) only, stdlib sockets, no
third-party dependency. Two modes, both OPT-IN at the integration layer
(`WAVR_NET_DHCP_MONITOR` gates whether this is ever constructed at all):

  * PASSIVE (default): sniff for DHCPOFFER/ACK a real client's own broadcast
    exchange already produces on the LAN -- Wavr sends nothing. Weakness: if
    no real client happens to renew a lease during the listen window, zero
    servers are observed that cycle (silently under-counts, never
    over-counts). The transport is a raw AF_PACKET Ethernet sniff when
    available, UDP/68-bind otherwise -- see `_default_listen` and
    `wavr.sources._dhcp_raw`'s module docstring for why (an appliance that
    already runs its own DHCP client, e.g. the G9 Core on Android, can make a
    UDP/68 bind conflict/stall; raw capture needs no bind to that port at
    all).
  * ACTIVE PROBE (`probe=True`, its own opt-in on top -- same "active probing
    is opt-in on top of opt-in" rule as sources.ssdp's LOC-XML fetch): before
    listening, broadcast ONE DHCPDISCOVER (with the broadcast flag set so
    every replying server answers via broadcast, not unicast to an address we
    don't have yet). This reliably elicits a DHCPOFFER from EVERY DHCP server
    on the broadcast domain in one window, which is what makes "count distinct
    servers" actually trustworthy. The crafted DISCOVER uses a throwaway
    locally-administered MAC (never the host's real MAC) so no real lease is
    ever claimed for this host's actual hardware address. Probing always uses
    the UDP/68 socket (it needs to send on the same socket it reads replies
    from) -- see `_default_listen`.

Only DHCPOFFER packets (option 53 = 2) are counted as "a server is offering
here" -- DHCPACK/other message types are ignored (an ACK is a reply to a
specific client's REQUEST, not a fresh "I am a DHCP server on this LAN"
announcement, and counting it too would double-count the same server per
lease renewal).

Never raises on malformed/hostile input -- one bad packet is dropped, exactly
like sources.mdns/sources.ssdp.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
from typing import AsyncIterator, Callable

from wavr.sources._dhcp_raw import open_with_timeout, raw_af_packet_supported, raw_dhcp_listen

_LOG = logging.getLogger(__name__)

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
MAGIC_COOKIE = b"\x63\x82\x53\x63"

MSG_DISCOVER = 1
MSG_OFFER = 2

# Injectable transport: a zero-arg factory returning a FRESH async iterator of
# (raw_packet_bytes, source_ip) pairs each time it's called -- identical seam
# to sources.mdns/sources.ssdp so tests inject a canned async generator.
PacketSource = Callable[[], AsyncIterator[tuple[bytes, str]]]

# Defensive cap on packets processed in one collect() window -- same
# rationale as mdns/ssdp (bounds a flooding LAN host's worst-case CPU/memory).
_MAX_PACKETS_PER_WINDOW = 2000


def parse_dhcp_packet(data: bytes) -> dict | None:
    """Decode one BOOTP/DHCP datagram. Returns
    {"op": int, "msg_type": int|None, "server_id": str|None,
     "yiaddr": str|None, "mac": str|None}
    or None for anything too short / missing the DHCP magic cookie (i.e. not
    a DHCP packet at all -- plain BOOTP has no options). Never raises -- a
    truncated/hostile datagram yields None rather than propagating."""
    try:
        if len(data) < 240 or data[236:240] != MAGIC_COOKIE:
            return None
        op = data[0]
        yiaddr_raw = data[16:20]
        yiaddr = socket.inet_ntoa(yiaddr_raw) if any(yiaddr_raw) else None
        chaddr = data[28:34]
        mac = ":".join(f"{b:02x}" for b in chaddr) if any(chaddr) else None

        options: dict[int, bytes] = {}
        pos = 240
        while pos < len(data):
            tag = data[pos]
            if tag == 255:            # End
                break
            if tag == 0:               # Pad
                pos += 1
                continue
            if pos + 1 >= len(data):
                break
            length = data[pos + 1]
            val = data[pos + 2:pos + 2 + length]
            if len(val) < length:       # truncated option -- stop, keep what we have
                break
            options[tag] = val
            pos += 2 + length

        msg_type_raw = options.get(53)
        msg_type = msg_type_raw[0] if msg_type_raw else None
        server_id_raw = options.get(54)
        server_id = (socket.inet_ntoa(server_id_raw)
                     if server_id_raw and len(server_id_raw) == 4 else None)
        return {"op": op, "msg_type": msg_type, "server_id": server_id,
                "yiaddr": yiaddr, "mac": mac}
    except Exception:
        return None


def build_discover_packet(xid: int | None = None) -> bytes:
    """Build one minimal DHCPDISCOVER datagram (RFC 2131), broadcast flag SET
    (so every replying server answers via broadcast -- we may not have an IP
    yet) and a throwaway locally-administered MAC in `chaddr` (never the
    host's real hardware address, so no real lease is ever claimed for this
    machine)."""
    xid = xid if xid is not None else int.from_bytes(os.urandom(4), "big")
    # Locally-administered, unicast MAC (the U/L bit set, multicast bit clear)
    # -- a well-known "this is not a real assigned address" convention, plus
    # random bytes so concurrent probes don't collide on the same xid/chaddr.
    mac = bytes([0x02]) + os.urandom(5)
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,           # op: BOOTREQUEST
        1,           # htype: Ethernet
        6,           # hlen
        0,           # hops
        xid,
        0,           # secs
        0x8000,      # flags: broadcast bit set
        b"\x00\x00\x00\x00",  # ciaddr
        b"\x00\x00\x00\x00",  # yiaddr
        b"\x00\x00\x00\x00",  # siaddr
        b"\x00\x00\x00\x00",  # giaddr
        mac.ljust(16, b"\x00"),
        b"\x00" * 64,   # sname
        b"\x00" * 128,  # file
    )
    options = MAGIC_COOKIE
    options += bytes([53, 1, MSG_DISCOVER])   # DHCP Message Type = DISCOVER
    options += bytes([55, 3, 1, 3, 6])        # Parameter Request List: subnet/router/DNS
    options += bytes([255])                    # End
    return header + options


def _open_client_socket() -> socket.socket:
    """Synchronous UDP/68 bind -- callers MUST run this through
    `_dhcp_raw.open_with_timeout`, never directly on the event loop (this
    exact bind has been observed to STALL, not just fail fast, against
    Android's own DHCP client on the G9 Core -- see
    `wavr.sources._dhcp_raw` module docstring)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", DHCP_CLIENT_PORT))
    return sock


async def _udp_listen(probe: bool = False) -> AsyncIterator[tuple[bytes, str]]:
    """Classic UDP/68-bind transport (SO_REUSEADDR/SO_REUSEPORT so it can
    coexist with another well-behaved listener that also sets them) -- the
    fallback used by `_default_listen` when raw AF_PACKET capture is
    unavailable, and unconditionally used for `probe=True` (needs to
    broadcast the DISCOVER on the same socket it listens for replies on).
    The open+bind itself runs off the event loop with a bounded wait via
    `open_with_timeout` -- see `wavr.sources._dhcp_raw` module docstring for
    why that matters. READ-ONLY when `probe` is False: passive mode never
    transmits anything."""
    loop = asyncio.get_event_loop()
    sock = await open_with_timeout(_open_client_socket, f"UDP/{DHCP_CLIENT_PORT} bind")
    sock.settimeout(1.0)
    try:
        if probe:
            with contextlib.suppress(OSError):
                sock.sendto(build_discover_packet(), ("255.255.255.255", DHCP_SERVER_PORT))
        while True:
            try:
                data, (ip, _port) = await loop.run_in_executor(None, sock.recvfrom, 65535)
            except socket.timeout:
                continue
            yield data, ip
    finally:
        sock.close()


async def _default_listen(probe: bool = False) -> AsyncIterator[tuple[bytes, str]]:
    """Default real transport. PASSIVE mode (`probe=False`) tries the raw
    AF_PACKET sniff FIRST -- no UDP/68 bind at all, so it cannot conflict
    with a DHCP client the host OS already runs (see
    `wavr.sources._dhcp_raw` module docstring) -- falling back to the
    classic UDP/68-bind listener (`_udp_listen`) when raw capture is
    unavailable (non-Linux, or CAP_NET_RAW missing) or its open times out.
    `probe=True` always uses the UDP path: it needs to broadcast the
    DISCOVER on the very socket it reads replies from, which the read-only
    raw sniff does not provide."""
    if not probe and raw_af_packet_supported():
        agen = raw_dhcp_listen()
        try:
            first = await agen.__anext__()
        except StopAsyncIteration:
            return
        except (AttributeError, PermissionError, OSError):
            _LOG.info("raw AF_PACKET DHCP sniff unavailable, falling back to UDP/%d bind",
                      DHCP_CLIENT_PORT, exc_info=True)
        else:
            yield first
            async for item in agen:
                yield item
            return
    async for item in _udp_listen(probe=probe):
        yield item


class DHCPCollector:
    """Passive (default) or active-probe DHCP collector. `listen` is the
    injectable packet transport (default: the real UDP/68 socket, optionally
    probing first when `probe=True`). Only DHCPOFFER packets are counted --
    see module docstring for why ACKs are excluded.

    `collect()` returns {server_key: {"ip": str, "yiaddr": str|None,
    "offers": int}} where `server_key` is the DHCP option-54 Server
    Identifier when present, else the packet's source IP (some minimal DHCP
    servers omit option 54) -- so every distinct real-world server still gets
    its own key even without that option."""

    def __init__(self, listen: PacketSource | None = None, probe: bool = False):
        self._probe = probe
        self._listen = listen or (lambda: _default_listen(probe=probe))

    async def collect(self, duration: float = 3.0) -> dict[str, dict]:
        packets = await self._drain(duration)
        servers: dict[str, dict] = {}
        for data, ip in packets:
            parsed = parse_dhcp_packet(data)
            if not parsed or parsed["msg_type"] != MSG_OFFER:
                continue
            key = parsed["server_id"] or ip
            entry = servers.setdefault(key, {"ip": ip, "yiaddr": parsed["yiaddr"], "offers": 0})
            entry["offers"] += 1
            if parsed["yiaddr"]:
                entry["yiaddr"] = parsed["yiaddr"]
        return servers

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
