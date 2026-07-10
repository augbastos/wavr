"""Shared passive raw-Ethernet (AF_PACKET) DHCP frame sniff -- the preferred
transport for BOTH `sources.dhcp.DHCPCollector` (rogue/multi-server
detection, `WAVR_NET_DHCP_MONITOR`) and
`sources.dhcp_fp.DHCPFingerprintCollector` (OS fingerprint,
`WAVR_NET_DHCP_FP`).

WHY THIS EXISTS (G9 Core field bug): both collectors' original transport
opened a UDP socket bound to port 68 or 67 respectively. On a normal Linux
box that is usually fine (SO_REUSEADDR/SO_REUSEPORT let a second listener
coexist with anything else that also sets those options). On the G9 Core
appliance (Android + Magisk-rooted proot-debian) it is NOT: Android's own
DHCP client already holds UDP/68, and in practice `bind()` against that
conflict has been observed to STALL rather than fail fast on that device --
and because the stall happened inside a plain synchronous socket call, it
blocked the asyncio event loop thread entirely, hanging the whole backend at
startup (every other request/health-check stopped responding too, not just
the DHCP feature). Two independent fixes, both applied here:

  1. Every blocking socket call (this module's AF_PACKET open+recv, AND the
     UDP bind()s in `sources.dhcp`/`sources.dhcp_fp`) now runs in the
     default executor (`loop.run_in_executor`), never on the event-loop
     thread -- so even if one genuinely never returns, the app keeps serving
     every other request; only a single bounded background thread is tied
     up. `open_with_timeout` additionally bounds the WAIT for that (the open
     step should be near-instant when it works at all) and turns a timeout
     into a plain `OSError`, so it flows through each collector's existing
     `except (PermissionError, OSError)` "unavailable in this environment"
     handling unchanged. A genuine timeout for a given `what` label is
     remembered process-wide (`_timed_out_openers`) so THAT SAME opener is
     never retried again -- the stdlib gives no way to kill an executor
     thread that truly never returns, so retrying it every periodic cycle
     would leak one more stuck thread each time and could eventually
     exhaust the shared default executor (used by every other Wavr
     collector too). This guard covers every opener that goes through
     `open_with_timeout`, not just the raw AF_PACKET path -- including the
     UDP `bind()` fallbacks in `sources.dhcp`/`sources.dhcp_fp`, which is
     exactly the call that has actually been observed to stall on the G9.
  2. This module gives both collectors a way to sniff DHCP traffic WITHOUT
     ever binding UDP/67 or UDP/68 at all: a raw AF_PACKET socket reads
     Ethernet frames directly off the wire, and this module's own tiny
     Ethernet/IPv4/UDP parser picks out the DHCP ones (source or destination
     port 67 or 68) -- so it cannot conflict with any other process's DHCP
     socket, no matter what socket options that process did or didn't set.
     This needs CAP_NET_RAW (confirmed available in the G9's Magisk-rooted
     proot) and only exists on Linux (`socket.AF_PACKET`); both collectors'
     `_default_listen` try this FIRST and fall back to the classic
     SO_REUSEADDR/SO_REUSEPORT UDP bind (unchanged) when raw capture is
     unavailable -- e.g. plain dev/test machines (Windows/macOS), or a
     hardened container granted CAP_NET_BIND_SERVICE but not CAP_NET_RAW.
     Either transport yields the identical raw BOOTP/DHCP payload bytes each
     module's own `parse_dhcp_packet` already parses -- zero change to any
     wire-format/parsing logic, only to how the bytes are obtained.

Active probing (`WAVR_NET_DHCP_PROBE`, `sources.dhcp` only) still needs to
BROADCAST one DHCPDISCOVER and is deliberately left on the classic UDP/68
socket -- crafting a full raw Ethernet+IP+UDP DHCPDISCOVER frame by hand is
out of scope here, and probing is already a second, smaller-blast-radius
opt-in layered on top of the (now-robust) passive default.
"""
from __future__ import annotations

import asyncio
import socket
import struct
from typing import AsyncIterator, Callable

# Generous bound for a syscall that should be near-instant when it works at
# all -- if socket()/bind() hasn't returned within this many seconds, treat
# it as a hard failure (the G9 field bug: bind() against a same-port
# conflict has been observed to STALL, not just fail fast) instead of
# silently eating the whole collect() window waiting on it.
OPEN_TIMEOUT = 3.0

_ETH_HEADER_LEN = 14
_ETH_TYPE_IPV4 = 0x0800
_IP_PROTO_UDP = 17
_ETH_P_ALL = 0x0003
_DHCP_PORTS = (67, 68)


def raw_af_packet_supported() -> bool:
    """True only on platforms exposing AF_PACKET (Linux). Checked up front so
    non-Linux dev/test machines skip straight to the UDP-bind fallback
    instead of paying for a doomed socket() call every cycle."""
    return hasattr(socket, "AF_PACKET")


# `what` labels that have PREVIOUSLY genuinely timed out (as opposed to failing fast
# with Permission/AttributeError/"port in use") in this process -- see the guard
# rationale in the module docstring. Keyed by the caller-supplied `what` string so
# e.g. "raw AF_PACKET DHCP sniff", "UDP/68 bind", and "UDP/67 bind" are tracked (and
# given up on) independently of one another.
_timed_out_openers: set[str] = set()


def reset_open_guards() -> None:
    """Test-only: clear every "genuinely timed out once, don't retry" guard -- both
    the generic per-`what` `_timed_out_openers` set here AND the legacy raw-socket-
    specific `_raw_open_timed_out` flag below. A module-global sticky guard persists
    across the whole test session by design (that's the point in production), so any
    test that can trigger a real timeout MUST reset it first (e.g. via an autouse
    fixture) or it will silently block a later, unrelated test that happens to reuse
    the same `what` label."""
    global _raw_open_timed_out
    _timed_out_openers.clear()
    _raw_open_timed_out = False


async def open_with_timeout(opener: Callable[[], socket.socket], what: str) -> socket.socket:
    """Run a blocking socket-open callable (socket()+setsockopt()+bind(), all
    synchronous) in the default executor and bound its wall-clock time to
    `OPEN_TIMEOUT` -- see module docstring. A timeout is re-raised as
    `OSError` so every caller's existing `except (PermissionError, OSError)`
    handling (already covers "port in use") also covers "open never
    returned" without any new except clause at the call site. The executor
    thread itself cannot be forcibly killed if `opener` truly never returns
    (stdlib limitation) -- but the event loop is never blocked by it, which
    is the actual hang this fixes.

    If `what` has already timed out once in this process, this raises OSError
    immediately WITHOUT calling `run_in_executor` again -- retrying an opener
    already known to hang would leak yet another unreclaimable background
    thread (see `_timed_out_openers`)."""
    if what in _timed_out_openers:
        raise OSError(
            f"{what} previously timed out opening in this process; not retrying "
            "(would leak another background thread)")
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, opener), timeout=OPEN_TIMEOUT)
    except asyncio.TimeoutError as exc:
        _timed_out_openers.add(what)
        raise OSError(
            f"{what} did not complete within {OPEN_TIMEOUT}s "
            "(a conflicting process likely already holds this socket/port)"
        ) from exc


def _open_raw_socket() -> socket.socket:
    """Open (synchronous -- callers MUST run this via `open_with_timeout`,
    never directly on the event loop) an AF_PACKET raw socket that observes
    every local interface. No bind to any UDP port -- see module
    docstring."""
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(_ETH_P_ALL))
    sock.settimeout(1.0)
    return sock


def _parse_udp_frame(frame: bytes) -> tuple[bytes, int, int, str] | None:
    """Pick the UDP payload + ports + source IP out of one raw Ethernet
    frame. Returns None for anything not plain (untagged) IPv4/UDP or too
    short to safely parse -- 802.1Q VLAN-tagged frames are a known,
    honestly-documented limitation: home-network DHCP traffic is essentially
    never tagged at the client, and handling the tag would need a second
    header-offset branch this module doesn't have evidence it needs yet.
    Never raises -- a truncated/hostile frame yields None exactly like
    `sources.dhcp/dhcp_fp.parse_dhcp_packet`."""
    try:
        if len(frame) < _ETH_HEADER_LEN + 20 + 8:
            return None
        eth_type = struct.unpack("!H", frame[12:14])[0]
        if eth_type != _ETH_TYPE_IPV4:
            return None
        ip_start = _ETH_HEADER_LEN
        ver_ihl = frame[ip_start]
        if (ver_ihl >> 4) != 4:
            return None
        ihl = (ver_ihl & 0x0F) * 4
        if ihl < 20 or len(frame) < ip_start + ihl + 8:
            return None
        proto = frame[ip_start + 9]
        if proto != _IP_PROTO_UDP:
            return None
        src_ip = socket.inet_ntoa(frame[ip_start + 12:ip_start + 16])
        udp_start = ip_start + ihl
        src_port, dst_port, udp_len = struct.unpack("!HHH", frame[udp_start:udp_start + 6])
        payload_start = udp_start + 8
        payload_end = udp_start + udp_len if udp_len >= 8 else len(frame)
        payload = frame[payload_start:min(payload_end, len(frame))]
        return payload, src_port, dst_port, src_ip
    except Exception:
        return None


# Set once a raw-socket open genuinely TIMES OUT (as opposed to failing fast
# with Permission/AttributeError) -- a timeout means the offending
# `_open_raw_socket()` call is still running in a background executor thread
# (stdlib gives no way to kill it) that will never be reclaimed for the rest
# of the process's life. A fast-fail (no CAP_NET_RAW, no AF_PACKET) costs
# nothing and is safe to retry every cycle forever; a genuine hang is NOT --
# retrying it every periodic cycle (e.g. every 30s) would leak one more
# thread each time and could eventually exhaust the shared default executor
# (used by every other Wavr collector too), even though the event loop
# itself stays responsive. So: try the raw path at most once per process:
# after the first timeout, permanently prefer the UDP fallback instead of
# repeating a call already known not to return. Unverified on real hardware
# (the field bug that motivated this module is specifically the UDP/68
# *bind* stalling, not an AF_PACKET open) -- this is a defensive bound for
# an untested-but-plausible failure mode, not a confirmed one.
#
# This flag is checked BEFORE even calling `open_with_timeout` (see
# `raw_dhcp_listen` below) and is now REDUNDANT with (but kept alongside) the
# generic `_timed_out_openers` guard inside `open_with_timeout` itself, which
# covers this same raw path (keyed by the "raw AF_PACKET DHCP sniff" `what`
# string) as well as every UDP-bind opener -- the gap this flag alone left
# open. Redundant guards here are cheap and harmless; removing this one would
# only save a few lines.
_raw_open_timed_out = False


async def raw_dhcp_listen() -> AsyncIterator[tuple[bytes, str]]:
    """Async generator yielding (bootp_payload, src_ip) for every observed
    Ethernet frame whose UDP source OR destination port is 67 or 68 --
    shared transport for both collectors; each keeps only the message types
    it cares about downstream exactly like it already does for the UDP-bind
    path (DHCPCollector: OFFER only; DHCPFingerprintCollector: DISCOVER/
    REQUEST only). Every blocking step (open, recv) runs off the event-loop
    thread -- see module docstring."""
    global _raw_open_timed_out
    if _raw_open_timed_out:
        raise OSError(
            "raw AF_PACKET DHCP sniff previously timed out opening in this process; "
            "not retrying (would leak another background thread) -- using UDP fallback")
    try:
        sock = await open_with_timeout(_open_raw_socket, "raw AF_PACKET DHCP sniff")
    except OSError as exc:
        if isinstance(exc.__cause__, asyncio.TimeoutError):
            _raw_open_timed_out = True
        raise
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                frame = await loop.run_in_executor(None, sock.recv, 65535)
            except socket.timeout:
                continue
            parsed = _parse_udp_frame(frame)
            if parsed is None:
                continue
            payload, src_port, dst_port, src_ip = parsed
            if src_port in _DHCP_PORTS or dst_port in _DHCP_PORTS:
                yield payload, src_ip
    finally:
        sock.close()
