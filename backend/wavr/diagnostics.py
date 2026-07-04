"""Network diagnostics (A3.2) — ping / traceroute / DNS-benchmark.

LOCAL/LAN-first tools, gated as a family behind WAVR_NET_DIAGNOSTICS
(diagnostics_enabled(), default OFF -> each route 503s) + the require_local CSRF
gate. Every network touch is behind an injectable transport so the whole module
is mock-tested with zero real network in CI (same seam convention as netutils).

COMMAND-INJECTION HARD RULE (traceroute): the OS tracert/traceroute binary is
invoked with an argv LIST and shell=False -- NEVER a shell string, NEVER
shell=True. The user-supplied target is additionally run through validate_target
(a strict IP-literal / hostname regex that rejects every shell metacharacter)
BEFORE it is placed as a single argv element, and both a hop cap and a
wall-clock timeout bound the run. ping likewise validates its target.

DNS-benchmark defaults to the LAN resolver only (the guessed gateway) -> zero
egress by default; public resolvers (1.1.1.1 / 8.8.8.8 / ...) are only queried
when the caller explicitly supplies them, using a benign fixed query name.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import socket
import struct
import sys
import time
from typing import Awaitable, Callable

from wavr import netutils
from wavr.internet_monitor import guess_gateway

# Hostname per RFC 1123: labels of [A-Za-z0-9-] (not starting/ending with '-'),
# dot-separated, total <= 253. This permits ONLY letters, digits, hyphen and
# dot -- so every shell metacharacter (; | & ` $ ( ) < > space newline quote
# backslash *) and path/URL character is rejected. IP literals are accepted via
# ipaddress before the regex is consulted.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

_MAX_HOPS = 20
_MS_RE = re.compile(r"([\d.]+)\s*ms")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# argv-list runner: (argv, timeout) -> (returncode, combined_stdout). Injected in
# tests so no subprocess is ever spawned in CI.
TracerouteRunner = Callable[[list[str], float], Awaitable["tuple[int, str]"]]
# DNS query transport: (resolver_ip, query_name, timeout) -> latency_ms | None.
DnsQuery = Callable[[str, str, float], Awaitable["float | None"]]


def diagnostics_enabled() -> bool:
    """True only if WAVR_NET_DIAGNOSTICS is explicitly enabled. OFF by default."""
    return os.getenv("WAVR_NET_DIAGNOSTICS", "").strip().lower() in ("1", "true", "yes", "on")


def validate_target(host: str) -> str:
    """Return a normalized target (IP literal or hostname) or raise ValueError.

    The single security boundary for diagnostics targets: it accepts ONLY an IP
    literal or an RFC-1123 hostname, so any input carrying a shell metacharacter,
    whitespace, quote, slash, or option-looking prefix is rejected before it can
    reach a subprocess argv or a socket call."""
    h = (host or "").strip()
    if not h or len(h) > 253:
        raise ValueError("target is required")
    if h.startswith("-"):
        # Defense in depth: never let a target masquerade as a CLI option even
        # though it lives in a positional argv slot.
        raise ValueError(f"invalid target: {host!r}")
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(h):
        raise ValueError(f"invalid target: {host!r}")
    return h


def is_egress_target(target: str) -> bool:
    """True if `target` reaches OUTSIDE the LAN (a public / routable address).

    False for a private, loopback, link-local, multicast or unspecified IP, or
    an mDNS `.local` name. Used to stamp every diagnostic response with an honest
    `egress` signal -- the same disclosure dnsbench already carries -- so
    /api/status and the Tools tile can tell the user, per invocation, whether a
    ping/traceroute leaves the LAN. Best effort for hostnames: a non-IP, non-
    `.local` name is assumed to leave the LAN (it cannot be resolved here without
    egress, so the signal is disclosed conservatively)."""
    try:
        addr = ipaddress.ip_address(target)
    except ValueError:
        return not target.lower().endswith(".local")
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_unspecified)


# ---------------------------------------------------------------------------
# ping — reuse netutils.ping_host (TCP-connect latency, no raw ICMP privilege)
# ---------------------------------------------------------------------------
async def ping(host: str, count: int = 3, timeout: float = 1.0,
               probe: Callable[[str, int, float], Awaitable["float | None"]] | None = None
               ) -> dict:
    """TCP-connect ping `host` `count` times; return per-attempt ms + min/avg/max.
    Validates the target. Inject `probe` (netutils.ping_host's seam) to test."""
    target = validate_target(host)
    count = max(1, min(int(count), 10))
    samples: list[float | None] = []
    for _ in range(count):
        samples.append(await netutils.ping_host(target, timeout=timeout, probe=probe))
    ok = [s for s in samples if s is not None]
    return {
        "host": target,
        "count": count,
        "received": len(ok),
        "samples": samples,
        "min_ms": min(ok) if ok else None,
        "avg_ms": (sum(ok) / len(ok)) if ok else None,
        "max_ms": max(ok) if ok else None,
        "egress": is_egress_target(target),
    }


# ---------------------------------------------------------------------------
# traceroute — shell OUT to the OS binary via an INJECTABLE argv-list runner
# ---------------------------------------------------------------------------
def _traceroute_argv(target: str, max_hops: int) -> list[str]:
    """Build the argv LIST for the OS traceroute tool. `-d`/`-n` disables reverse
    DNS (fewer lookups, and hop output is IP-only -> smaller XSS surface). The OS
    tool already holds the raw-socket capability, so Wavr needs no privilege."""
    if sys.platform.startswith("win"):
        return ["tracert", "-d", "-h", str(max_hops), "-w", "1000", target]
    return ["traceroute", "-n", "-m", str(max_hops), "-w", "2", target]


async def _default_runner(argv: list[str], timeout: float) -> "tuple[int, str]":
    """Default traceroute runner: subprocess with an argv LIST + shell=False.
    NEVER builds a shell string. Bounded by `timeout` (process killed on
    expiry)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return 1, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _parse_traceroute(out: str) -> list[dict]:
    """Parse tracert/traceroute stdout into hop rows: {hop, hosts, ms, timeout}.
    Best-effort + defensive -- unparseable lines are skipped, never raise."""
    hops: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\b", line)
        if not m:
            continue
        hops.append({
            "hop": int(m.group(1)),
            "hosts": _IPV4_RE.findall(line),
            "ms": [float(x) for x in _MS_RE.findall(line)],
            "timeout": "*" in line,
        })
    return hops


async def traceroute(host: str, max_hops: int = _MAX_HOPS, timeout: float = 20.0,
                     runner: TracerouteRunner | None = None) -> dict:
    """Trace the route to `host`. Validates the target, invokes the OS tool with
    an argv LIST (no shell), caps hops + wall-clock. Inject `runner` to test with
    canned output (zero subprocess)."""
    target = validate_target(host)
    hops = max(1, min(int(max_hops), _MAX_HOPS))
    argv = _traceroute_argv(target, hops)
    runner = runner or _default_runner
    rc, out = await runner(argv, timeout)
    return {"host": target, "max_hops": hops, "hops": _parse_traceroute(out),
            "ok": rc == 0, "egress": is_egress_target(target)}


# ---------------------------------------------------------------------------
# dnsbench — stdlib UDP/53 A-query timing; LAN resolver only by default
# ---------------------------------------------------------------------------
def _build_dns_query(name: str) -> bytes:
    """Build a minimal DNS A-record query (RFC 1035): 12-byte header + QNAME +
    QTYPE(A)+QCLASS(IN). Standard-library only, no dnspython dep."""
    import random
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)  # RD=1, 1 question
    qname = b"".join(bytes([len(part)]) + part.encode("ascii")
                     for part in name.split(".") if part)
    qname += b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)


def _sync_dns_query(resolver: str, name: str, timeout: float) -> "float | None":
    packet = _build_dns_query(name)
    start = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(packet, (resolver, 53))
            s.recvfrom(1024)
    except OSError:
        return None
    return (time.perf_counter() - start) * 1000.0


async def _default_dns_query(resolver: str, name: str, timeout: float) -> "float | None":
    return await asyncio.to_thread(_sync_dns_query, resolver, name, timeout)


async def dnsbench(name: str = "example.com", resolvers: "list[str] | None" = None,
                   timeout: float = 2.0, query_fn: DnsQuery | None = None) -> dict:
    """Time an A-record query for `name` against each resolver, fastest-first.

    resolvers default = the guessed LAN gateway resolver ONLY -> zero egress.
    Supplying public resolvers (1.1.1.1 etc.) is an explicit caller opt-in and
    reaches those hosts. Each resolver must be an IP literal (validated). Inject
    `query_fn` to test without a socket."""
    query_name = validate_target(name)  # benign hostname; rejects metacharacters
    if not resolvers:
        gw = guess_gateway()
        resolvers = [gw] if gw else []
        egress = False
    else:
        # Cap the caller-supplied list so a huge resolvers array can't make one
        # request run for minutes (each query is awaited sequentially with a
        # timeout). Mirrors the count/hop caps on the sibling tools.
        resolvers = resolvers[:16]
        egress = True
    query_fn = query_fn or _default_dns_query
    rows: list[dict] = []
    for r in resolvers:
        try:
            ip = str(ipaddress.ip_address((r or "").strip()))
        except ValueError:
            raise ValueError(f"resolver must be an IP address: {r!r}")
        ms = await query_fn(ip, query_name, timeout)
        rows.append({"resolver": ip, "ms": ms})
    rows.sort(key=lambda x: (x["ms"] is None, x["ms"] if x["ms"] is not None else 0.0))
    return {"name": query_name, "results": rows, "egress": egress}
