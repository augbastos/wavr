"""Wavr Net extras — defensive network-inventory utilities for the defensive inventory.

Everything here is an OPT-IN add-on to the wavr.netinventory core: each feature
is a focused, config-gated function so the inventory core stays clean. Pure
Python standard library only (sockets / urllib) — no scapy, nmap, or speedtest
deps. Every network touch is behind an injectable transport so the whole module
is mock-tested with zero real network.

Config flags (all default OFF):
  WAVR_NET_PORTSCAN   -> enable risky-port awareness (port_scan_enabled())
  WAVR_NET_SPEEDTEST  -> enable the internet-health probe (speedtest_enabled())

PRIVACY (ADR-0002): presence history is MAC-level join/leave only, kept
in-memory. No per-person / per-target location data is ever recorded here.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

from wavr.netinventory import Device


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")


def port_scan_enabled() -> bool:
    """True only if WAVR_NET_PORTSCAN is explicitly enabled. OFF by default."""
    return _env_flag("WAVR_NET_PORTSCAN")


def speedtest_enabled() -> bool:
    """True only if WAVR_NET_SPEEDTEST is explicitly enabled. OFF by default."""
    return _env_flag("WAVR_NET_SPEEDTEST")


# ---------------------------------------------------------------------------
# 1) PORT / VULN AWARENESS  — local, connect-only, report-only
# ---------------------------------------------------------------------------
# TCP connect-check of a SMALL set of commonly-risky ports. If the port accepts
# a connection we note it as a risk on the device — nothing more. HARD LIMITS:
# connect-only (no banner grab), no auth attempts, no payloads, no external CVE
# lookup (that would need egress). OFF by default (gate on WAVR_NET_PORTSCAN).
RISKY_PORTS: dict[int, str] = {
    21: "FTP open — often cleartext credentials",
    23: "Telnet open — cleartext remote login, insecure",
    445: "SMB open — Windows file sharing exposed",
    554: "RTSP open — camera/video stream exposed",
    3389: "RDP open — remote desktop exposed",
    5900: "VNC open — remote desktop exposed",
}

# Injectable open-port probe: (ip, port, timeout) -> bool (True if it connects).
PortProbe = Callable[[str, int, float], Awaitable[bool]]


async def _tcp_open_probe(ip: str, port: int, timeout: float) -> bool:
    """Default probe: a plain TCP connect. Succeeds => port is open. Immediately
    closes the socket — no data sent or read (no banner grab)."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def scan_risky_ports(ip: str, ports=tuple(RISKY_PORTS), timeout: float = 0.5,
                           probe: PortProbe | None = None) -> list[int]:
    """Connect-check `ports` on one host, returning the open ones (sorted).
    Report-only. Inject `probe` to test without a socket."""
    probe = probe or _tcp_open_probe
    results = await asyncio.gather(*(probe(ip, p, timeout) for p in ports))
    return sorted(p for p, is_open in zip(ports, results) if is_open)


async def annotate_risks(devices, ports=tuple(RISKY_PORTS), timeout: float = 0.5,
                         probe: PortProbe | None = None) -> list[Device]:
    """Return NEW Device records with `risks` notes for any open risky ports.
    OPT-IN: call only when port_scan_enabled(). Devices without an IP are passed
    through untouched. Never mutates the input."""
    out: list[Device] = []
    for d in devices:
        open_ports = (await scan_risky_ports(d.ip, ports, timeout, probe)
                      if d.ip else [])
        notes = tuple(RISKY_PORTS[p] for p in open_ports if p in RISKY_PORTS)
        out.append(replace(d, risks=notes))
    return out


# ---------------------------------------------------------------------------
# 2) SPEED / INTERNET HEALTH  — the DELIBERATE zero-egress exception
# ---------------------------------------------------------------------------
# !!! DELIBERATE EXCEPTION to Wavr's no-external-calls / zero-cloud-egress
# invariant. Every OTHER feature stays on the local LAN; this one intentionally
# reaches an endpoint OUTSIDE the LAN to measure internet health. It is therefore
# OFF by default and must be explicitly enabled (WAVR_NET_SPEEDTEST). Pure stdlib
# (socket latency probe + urllib timed transfer) — no speedtest library.
_LATENCY_HOST = "1.1.1.1"
_LATENCY_PORT = 443
_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=10000000"
_UPLOAD_URL = "https://speed.cloudflare.com/__up"


def _default_latency(host: str = _LATENCY_HOST, port: int = _LATENCY_PORT,
                     timeout: float = 2.0) -> float | None:
    """Round-trip TCP-connect latency to an EXTERNAL host, in ms (None on fail).
    Reaches outside the LAN — see the module caveat above."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None


def _default_download(url: str = _DOWNLOAD_URL, timeout: float = 10.0) -> float | None:
    """Rough download throughput in Mbps via a timed urllib GET (None on fail).
    Reaches outside the LAN — see the module caveat above."""
    import urllib.request  # stdlib, lazy

    start = time.perf_counter()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            while True:
                chunk = resp.read(65536)
                if not chunk or (time.perf_counter() - start) > timeout:
                    break
                total += len(chunk)
    except Exception:
        return None
    elapsed = time.perf_counter() - start
    if elapsed <= 0 or total == 0:
        return None
    return (total * 8) / elapsed / 1e6


def _default_upload(url: str = _UPLOAD_URL, size: int = 5_000_000,
                    timeout: float = 10.0) -> float | None:
    """Rough upload throughput in Mbps via a timed urllib POST (None on fail).
    Reaches outside the LAN — see the module caveat above."""
    import urllib.request  # stdlib, lazy

    payload = b"\x00" * size
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp.read()
    except Exception:
        return None
    elapsed = time.perf_counter() - start
    if elapsed <= 0:
        return None
    return (size * 8) / elapsed / 1e6


def internet_health(latency_fn: Callable[[], float | None] | None = None,
                    download_fn: Callable[[], float | None] | None = None,
                    upload_fn: Callable[[], float | None] | None = None) -> dict:
    """Measure latency + rough down/up throughput.

    DELIBERATE EXCEPTION to zero-egress: the default probes reach an endpoint
    OUTSIDE the LAN. This is the single opt-in feature that breaks Wavr's
    no-external-calls invariant, hence OFF by default (WAVR_NET_SPEEDTEST).
    Inject the *_fn transports to measure/test without touching the internet.
    """
    return {
        "latency_ms": (latency_fn or _default_latency)(),
        "download_mbps": (download_fn or _default_download)(),
        "upload_mbps": (upload_fn or _default_upload)(),
    }


# ---------------------------------------------------------------------------
# 3a) WAKE-ON-LAN  — stdlib UDP magic packet
# ---------------------------------------------------------------------------
def build_magic_packet(mac: str) -> bytes:
    """Build a Wake-on-LAN magic packet: 6x 0xFF followed by the target MAC
    repeated 16 times (102 bytes total). Raises ValueError on a malformed MAC."""
    clean = mac.replace("-", "").replace(":", "").replace(".", "").lower()
    if len(clean) != 12 or any(c not in "0123456789abcdef" for c in clean):
        raise ValueError(f"invalid MAC: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(clean) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9,
                      send: Callable[[bytes, str, int], None] | None = None) -> bytes:
    """Send a WoL magic packet to `mac` over the LAN broadcast. Returns the
    packet sent. Inject `send` to test without opening a socket."""
    packet = build_magic_packet(mac)
    (send or _default_udp_broadcast)(packet, broadcast, port)
    return packet


def _default_udp_broadcast(packet: bytes, broadcast: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))


# ---------------------------------------------------------------------------
# 3b) PER-DEVICE PING / LATENCY  — TCP-connect latency, no ICMP privileges
# ---------------------------------------------------------------------------
async def _tcp_latency_probe(ip: str, port: int, timeout: float) -> float | None:
    start = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return (time.perf_counter() - start) * 1000.0


async def ping_host(ip: str, ports=(7, 80, 443, 445), timeout: float = 1.0,
                    probe: Callable[[str, int, float], Awaitable[float | None]] | None = None
                    ) -> float | None:
    """Best-effort LAN latency to a host in ms via TCP connect (first port that
    answers wins), or None if unreachable. Stdlib only — no raw ICMP sockets, so
    no elevated privileges. Inject `probe` to test without a socket."""
    probe = probe or _tcp_latency_probe
    for port in ports:
        ms = await probe(ip, port, timeout)
        if ms is not None:
            return ms
    return None


# ---------------------------------------------------------------------------
# 3c) PRESENCE HISTORY  — in-memory MAC join/leave log (ADR-0002)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PresenceEvent:
    """A device joining or leaving the LAN. MAC-level only — no person/location."""
    mac: str
    event: str    # "joined" | "left"
    ts: str       # ISO-8601 UTC

    def to_dict(self) -> dict:
        return {"mac": self.mac, "event": self.event, "ts": self.ts}


class PresenceHistory:
    """Lightweight in-memory log of device join/leave transitions across scans.

    PRIVACY (ADR-0002): tracks MAC presence only and keeps everything in memory
    (bounded ring). Never persists per-person or per-target location data.
    """

    def __init__(self, max_events: int = 1000):
        self._present: set[str] = set()
        self._log: list[PresenceEvent] = []
        self._max = max_events

    def update(self, macs, ts: str | None = None) -> list[PresenceEvent]:
        """Diff a fresh set of seen MACs against the last one; append + return
        the resulting join/leave events."""
        ts = ts or datetime.now(timezone.utc).isoformat()
        seen = {m.replace("-", ":").lower() for m in macs}
        events = [PresenceEvent(m, "joined", ts) for m in sorted(seen - self._present)]
        events += [PresenceEvent(m, "left", ts) for m in sorted(self._present - seen)]
        self._present = seen
        self._log.extend(events)
        if len(self._log) > self._max:                 # bounded ring
            self._log = self._log[-self._max:]
        return events

    def history(self) -> list[PresenceEvent]:
        return list(self._log)

    def present(self) -> set[str]:
        return set(self._present)
