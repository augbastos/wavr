"""Wavr passive DHCP-fingerprint collector -- OS/device-class hint from a
client's own DISCOVER/REQUEST broadcast.

Unlike sources.snmp/sources.netbios (targeted unicast probes), this is a pure
PASSIVE listener, same ethos as sources.mdns/sources.ssdp: Wavr sends nothing,
it only reads the BOOTP/DHCP (RFC 2131) broadcast frames every DHCP client
already sends to 255.255.255.255:67 when it joins the network or renews its
lease. This module binds a UDP socket to port 67 (the DHCP *server* port --
that is where client broadcasts land) with SO_REUSEADDR/SO_REUSEPORT so it can
coexist with a real DHCP server already listening there; if the real DHCP
server binds exclusively first (OS/permission dependent), binding here will
fail and the collector simply never starts -- a known, honestly-documented
limitation of passive UDP snooping without a raw/promiscuous socket (which
would need elevated privileges this module deliberately does not request).

Only DISCOVER (option 53 = 1) and REQUEST (option 53 = 3) messages are kept --
these are the CLIENT's own self-description; OFFER/ACK/NAK/etc. are the
server's reply and are ignored (no self-description content to fingerprint).
Two options are parsed:
    option 55 (Parameter Request List) -- the raw list of option numbers the
        client asked for, carried in the signal for a future explain view.
        NOT used for OS/device inference here beyond one narrow, publicly
        documented case (see `_infer_os`): building a broader "option-55
        signature -> OS" table would require a verified public fingerprint
        catalog this module does not have access to, and guessing one risks
        silently reproducing exactly the kind of proprietary recognition
        database Wavr must never ship (flag to
        privacy-compliance-license-auditor before adding one).
    option 60 (Vendor Class Identifier) -- a free-form ASCII string the
        client volunteers about itself (RFC 2132 9.14). A handful of
        widely-documented, self-identifying prefixes are recognized (`MSFT`
        = every Windows DHCP client since Windows 2000, `android-dhcp` =
        Android, `udhcp`/`dhcpcd` = the common embedded/Linux DHCP clients);
        the same conservative hostname_type() regex table every other
        collector uses is also run against it, since option 60 was designed
        for exactly this self-identification and some IoT/consumer devices
        put their own product name there.

OPT-IN, default OFF -- like every Wavr source, this module does not read the
environment itself; the integration step wires a `WAVR_NET_DHCP_FP` flag
gating whether `DHCPFingerprintCollector` is ever constructed/started.

Keying: unlike the IP-addressed collectors, a DHCP DISCOVER is sent BEFORE
the client has any IP (ciaddr=0.0.0.0), so there is no useful source IP to
map through an `ip_to_mac` table. The BOOTP header's own `chaddr` field
already carries the client's hardware address directly -- this module keys
its output by THAT MAC instead, with no separate ARP correlation step.

Produces a per-MAC dict shaped for wavr.recog's `dhcp` hook:
    {"device_type": taxonomy?, "os": str?}
(recog.py only ever reads those two keys for `dhcp` -- see recog.py's module
docstring.) `vendor_class`/`param_request_list` ride along as extra evidence
for a future richer inventory/explain view.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import AsyncIterator, Callable

from wavr.data.deviceclass import hostname_type

DHCP_SERVER_PORT = 67

# Injectable transport: a zero-arg factory returning a FRESH async iterator of
# (raw_packet_bytes, source_ip) pairs each time it's called -- identical seam
# to sources.mdns.PacketSource. The default binds the real broadcast socket;
# tests inject a canned async generator function.
PacketSource = Callable[[], AsyncIterator[tuple[bytes, str]]]

# Defensive cap on packets processed in one collect() window -- see
# sources.mdns's identical rationale (bounds a broadcast-flood LAN host).
_MAX_PACKETS_PER_WINDOW = 2000

_BOOTREQUEST = 1
_MAGIC_COOKIE = b"\x63\x82\x53\x63"
_OPT_PARAM_REQUEST_LIST = 55
_OPT_MSG_TYPE = 53
_OPT_VENDOR_CLASS = 60
_MSGTYPE_DISCOVER = 1
_MSGTYPE_REQUEST = 3

# Option 252: Microsoft's own DHCP client is documented to request the WPAD
# (proxy auto-discovery) option -- a well-known, publicly documented
# Microsoft client quirk, usable as an OS fallback when option 60 is absent
# or too generic.
_OPT_WPAD = 252


def parse_dhcp_packet(data: bytes) -> dict | None:
    """Parse one BOOTP/DHCP datagram. Returns None for anything that is not a
    client DISCOVER/REQUEST self-description worth acting on (wrong op code,
    not a DHCP packet at all, or a server-originated OFFER/ACK/NAK/etc.).
    Never raises -- malformed/truncated/hostile datagrams yield None."""
    try:
        if len(data) < 240 or data[236:240] != _MAGIC_COOKIE:
            return None
        op, _htype, hlen, _hops = data[0], data[1], data[2], data[3]
        if op != _BOOTREQUEST:
            return None
        chaddr = data[28:28 + 16]
        mac_len = hlen if 0 < hlen <= 16 else 6
        mac_bytes = chaddr[:mac_len][:6]
        if len(mac_bytes) < 6 or not any(mac_bytes):
            return None
        mac = ":".join(f"{b:02x}" for b in mac_bytes)

        pos = 240
        msg_type: int | None = None
        vendor_class: str | None = None
        param_request_list: list[int] = []
        while pos < len(data):
            code = data[pos]
            if code == 0xFF:  # END
                break
            if code == 0x00:  # PAD
                pos += 1
                continue
            if pos + 1 >= len(data):
                break
            length = data[pos + 1]
            value = data[pos + 2:pos + 2 + length]
            if len(value) < length:
                break  # truncated option -- stop parsing, keep what we have
            if code == _OPT_MSG_TYPE and len(value) == 1:
                msg_type = value[0]
            elif code == _OPT_VENDOR_CLASS:
                vendor_class = value.decode("utf-8", errors="replace")
            elif code == _OPT_PARAM_REQUEST_LIST:
                param_request_list = list(value)
            pos += 2 + length

        if msg_type not in (_MSGTYPE_DISCOVER, _MSGTYPE_REQUEST):
            return None
        return {"mac": mac, "vendor_class": vendor_class, "param_request_list": param_request_list}
    except Exception:
        return None


def _infer_os(vendor_class: str | None, param_request_list: list[int]) -> str | None:
    if vendor_class:
        low = vendor_class.lower()
        if low.startswith("msft"):
            return "Windows"
        if low.startswith("android-dhcp"):
            return "Android"
        if low.startswith("udhcp") or low.startswith("dhcpcd"):
            return "Linux"
    if _OPT_WPAD in param_request_list:
        return "Windows"
    return None


def _open_broadcast_socket(port: int) -> socket.socket:
    """Bind UDP `port` for RECEIVE only. SO_BROADCAST is deliberately NOT set
    here -- that option only governs permission to SEND to a broadcast
    address, which this read-only listener never does; the OS delivers
    inbound broadcast datagrams to a bound socket without it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    return sock


async def _default_listen() -> AsyncIterator[tuple[bytes, str]]:
    """Default real transport: bind UDP port 67 and yield every datagram
    received (READ-ONLY -- never transmits). See sources.mdns._default_listen
    for the identical executor-thread rationale."""
    loop = asyncio.get_event_loop()
    sock = _open_broadcast_socket(DHCP_SERVER_PORT)
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


class DHCPFingerprintCollector:
    """Passive DHCP-fingerprint collector. `listen` is the injectable packet
    transport (default: the real broadcast socket). Output keys by the MAC
    parsed directly from each packet's own `chaddr` field -- see module
    docstring for why (no usable source IP exists yet at DISCOVER time)."""

    def __init__(self, listen: PacketSource | None = None):
        self._listen = listen or _default_listen
        # Tri-state honest-availability signal (panel-review finding #9/#17):
        # None = never attempted a bind yet (feature off, or no scan cycle has
        # run since startup); True = the UDP/67 bind succeeded at least once
        # (this cycle may still have observed zero packets -- that's a normal
        # quiet LAN, not unavailability); False = the raw bind itself failed
        # with a permission/OS error -- e.g. a non-root proot/container
        # lacking CAP_NET_BIND_SERVICE, or a real DHCP server already holding
        # the port exclusively (see module docstring). Distinct from a
        # transient runtime crash: this is "this environment can't grant the
        # capability," not an error to alarm on.
        self.available: bool | None = None
        self.unavailable_reason: str | None = None

    async def collect(self, duration: float = 5.0) -> dict[str, dict]:
        """Listen for up to `duration` seconds (returns sooner if the
        injected transport is finite, e.g. in tests), and return
        {mac: dhcp_signal} -- one entry per distinct client MAC observed."""
        packets = await self._drain(duration)
        out: dict[str, dict] = {}
        for data, _src_ip in packets:
            try:
                parsed = parse_dhcp_packet(data)
            except Exception:
                continue  # one malformed packet must never kill the collector
            if not parsed:
                continue
            mac = parsed["mac"]
            signal = self._to_signal(parsed)
            existing = out.get(mac)
            if existing is None:
                out[mac] = signal
            else:
                # A REQUEST later in the window often carries a fuller option
                # set than the client's initial DISCOVER -- enrich, don't
                # duplicate or overwrite already-known non-empty fields.
                for k, v in signal.items():
                    if v and not existing.get(k):
                        existing[k] = v
        return out

    async def _drain(self, duration: float) -> list[tuple[bytes, str]]:
        packets: list[tuple[bytes, str]] = []
        agen = self._listen()

        async def _read_all() -> None:
            async for item in agen:
                if len(packets) >= _MAX_PACKETS_PER_WINDOW:
                    break
                packets.append(item)

        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_read_all(), timeout=duration)
            self.available = True
            self.unavailable_reason = None
        except (PermissionError, OSError) as exc:
            # The bind (the first thing the transport does, before any yield)
            # failed -- environment can't grant this, not a runtime crash.
            # Recorded so callers can show an honest "unavailable on this
            # device" instead of a silently-empty collect() result.
            self.available = False
            self.unavailable_reason = f"{type(exc).__name__}: {exc}"
        aclose = getattr(agen, "aclose", None)
        if aclose:
            with contextlib.suppress(Exception):
                await aclose()
        return packets

    def _to_signal(self, parsed: dict) -> dict:
        vendor_class = parsed.get("vendor_class")
        prl = parsed.get("param_request_list") or []
        signal: dict = {
            "vendor_class": vendor_class,
            "param_request_list": prl,
            "os": _infer_os(vendor_class, prl),
        }
        dtype = hostname_type(vendor_class)
        if dtype:
            signal["device_type"] = dtype
        return signal
