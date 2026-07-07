"""Wavr Net -- a DEFENSIVE local-network inventory.

Turns the bare ARP scan (wavr.sources.network) into a device inventory: each
LAN host resolved to MAC + IP + VENDOR (from the bundled offline OUI table) +
a coarse device-type guess, with a config-driven known-MAC allowlist so unknown
devices can be flagged as rogue.

HARD BOUNDARIES -- defensive only:
- Reads the ARP cache of the LAN this host is ALREADY on. Nothing else.
- No monitor mode, no sniffing, no packet injection, no port scanning, no
  external/online lookups. Vendor resolution is 100% local (bundled table).

Follows the injectable-transport seam of wavr.sources.network so the whole
thing is mock-testable with zero hardware / zero real network.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from dataclasses import asdict, dataclass, replace
from typing import Awaitable, Callable

from wavr.data.oui import lookup_vendor
from wavr.recog import recognize
from wavr.sources import network

_LOG = logging.getLogger(__name__)

# ip + mac on one `arp -a` line. Separator-agnostic (Windows "-", Unix ":").
_ARP_LINE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})"
)


@dataclass(frozen=True)
class Device:
    """One host seen on the LAN. `known` = MAC is on the allowlist.
    `risks` holds optional report-only risk notes from wavr.netutils port
    awareness (empty unless that opt-in pass ran).

    Identity fields (device_type/type_confidence/make/model/os/sources) come
    from wavr.recog's local fusion; `device_type` is one of the fixed
    wavr.data.deviceclass.DEVICE_TYPES values. `open_ports` is filled only by
    the opt-in connect-only port pass (wavr.netutils.annotate_ports).

    `is_gateway` is True only when this host's IP is the LAN default gateway,
    read from THIS host's own routing table
    (wavr.sources.network.default_gateway) -- never a guess, so it is honestly
    False when the gateway can't be determined. `latency_ms` is the opt-in
    per-device TCP-connect round-trip (wavr.netutils.ping_host), None until
    that active pass runs (or the host doesn't answer)."""
    mac: str
    ip: str | None
    vendor: str
    device_type: str
    known: bool
    hostname: str | None = None
    risks: tuple = ()             # tuple[str, ...] — opt-in risk notes
    type_confidence: str = "low"  # "high" | "medium" | "low"
    make: str | None = None
    model: str | None = None
    os: str | None = None
    open_ports: tuple = ()        # tuple[int, ...] — opt-in port pass only
    sources: tuple = ()           # tuple[dict] — recog evidence trail
    is_gateway: bool = False      # this host's IP == the LAN default gateway
    latency_ms: float | None = None  # opt-in TCP-connect latency (ping_host)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risks"] = list(d["risks"])
        d["open_ports"] = list(d["open_ports"])
        d["sources"] = [dict(s) for s in d["sources"]]
        return d


def _is_multicast_or_reserved(mac: str) -> bool:
    """True for broadcast, all-zero, and IPv4/IPv6 multicast MACs (I/G bit set)
    -- never real unicast hosts, so we keep them out of the inventory and, more
    importantly, never flag them as rogue devices."""
    if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
        return True
    try:
        return bool(int(mac.split(":")[0], 16) & 0x01)
    except (ValueError, IndexError):
        return True


def parse_arp_inventory(arp_output: str) -> list[tuple[str, str]]:
    """Extract (ip, mac) pairs from raw `arp -a` output. MACs are normalized to
    lowercase colon form; broadcast/multicast entries are dropped. De-duplicated
    by MAC (first IP wins), preserving first-seen order."""
    seen: dict[str, str] = {}
    for ip, mac in _ARP_LINE_RE.findall(arp_output):
        norm = mac.replace("-", ":").lower()
        if _is_multicast_or_reserved(norm):
            continue
        seen.setdefault(norm, ip)
    return [(ip, mac) for mac, ip in seen.items()]


def guess_device_type(vendor: str, hostname: str | None = None,
                      mac: str | None = None) -> str:
    """Coarse device-type guess -- thin wrapper over wavr.recog.recognize so
    existing call sites keep working. Returns one of the fixed taxonomy values
    (wavr.data.deviceclass.DEVICE_TYPES); use apply_recognition/recognize
    directly when you also want confidence + the evidence trail."""
    return recognize(
        {"vendor": vendor, "hostname": hostname, "mac": mac or ""}
    ).device_type


def apply_recognition(device: Device, pin: str | None = None,
                      bonjour: dict | None = None, upnp: dict | None = None,
                      snmp: dict | None = None, netbios: dict | None = None,
                      dhcp: dict | None = None, ha: dict | None = None) -> Device:
    """Return a NEW Device with the identity fields re-fused from everything
    currently known about it (vendor/hostname/MAC/open_ports + the optional
    user type-pin, which always wins) plus any passive/active protocol self-
    description handed in for this scan cycle (`bonjour` from
    wavr.sources.mdns, `upnp` from wavr.sources.ssdp, `snmp` from
    wavr.sources.snmp, `netbios` from wavr.sources.netbios, `dhcp` from
    wavr.sources.dhcp_fp, `ha` imported from the local Home Assistant device
    registry via wavr.ha_import -- all optional, all keyed per-device by the
    caller, e.g. wavr.netinventory_service). Pure/offline -- call again after
    the opt-in port pass fills `open_ports` (or fresh collector signals arrive)
    to fold new hints in."""
    ident = recognize({
        "mac": device.mac,
        "vendor": device.vendor,
        "hostname": device.hostname,
        "open_ports": device.open_ports or None,
        "user_pin": pin,
        "bonjour": bonjour,
        "upnp": upnp,
        "snmp": snmp,
        "netbios": netbios,
        "dhcp": dhcp,
        "ha": ha,
    })
    return replace(
        device,
        device_type=ident.device_type,
        type_confidence=ident.confidence,
        make=ident.make,
        model=ident.model,
        os=ident.os,
        sources=ident.sources,
    )


def _norm_macs(macs) -> set[str]:
    return {m.strip().replace("-", ":").lower() for m in (macs or ()) if m.strip()}


def _same_ip(a: "str | None", b: "str | None") -> bool:
    """True when two dotted-IPv4 strings denote the same address. Defensive:
    None or malformed input compares unequal, never raises."""
    if not a or not b:
        return False
    try:
        return ipaddress.ip_address(a.strip()) == ipaddress.ip_address(b.strip())
    except ValueError:
        return a.strip() == b.strip()


def build_inventory(entries: list[tuple[str, str]], known_macs=None,
                    hostnames: dict[str, str] | None = None,
                    pins: dict[str, str] | None = None,
                    gateway_ip: str | None = None) -> list[Device]:
    """Turn (ip, mac) pairs into resolved Device records. `known_macs` is the
    allowlist; `hostnames` optionally maps mac -> hostname for a better type
    guess; `pins` optionally maps mac -> user-pinned device_type (highest-
    precedence recog signal); `gateway_ip` (when given) flags the device whose
    IP is the LAN default gateway as is_gateway. Pure/offline -- no I/O."""
    known = _norm_macs(known_macs)
    hostnames = hostnames or {}
    pins = pins or {}
    out: list[Device] = []
    for ip, mac in entries:
        mac = mac.replace("-", ":").lower()
        hostname = hostnames.get(mac)
        device = Device(
            mac=mac,
            ip=ip,
            vendor=lookup_vendor(mac),
            device_type="unknown",
            known=mac in known,
            hostname=hostname,
            is_gateway=_same_ip(ip, gateway_ip),
        )
        out.append(apply_recognition(device, pin=pins.get(mac)))
    return out


async def _arp_output() -> str:
    """Default real transport: warm the ARP cache with a local /24 ping sweep,
    then return raw `arp -a` text. Scans ONLY the LAN this host is already on --
    no sniffing, no injection. Reuses wavr.sources.network's subprocess seam so
    tests can inject a mock transport instead."""
    ip = network._local_ipv4()
    if ip:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        sem = asyncio.Semaphore(32)   # cap concurrent ping subprocesses (was up to 254)

        async def ping(addr: str) -> None:
            async with sem:
                with contextlib.suppress(Exception):
                    await network._run(*network.ping_argv(addr, 200))

        await asyncio.gather(*(ping(str(h)) for h in net.hosts()))
    return await network._run("arp", "-a")


async def scan_inventory(known_macs=None,
                         scan: Callable[[], Awaitable[str]] | None = None,
                         hostnames: dict[str, str] | None = None,
                         pins: dict[str, str] | None = None,
                         resolve: Callable[[list[tuple[str, str]]], Awaitable[dict[str, str]]] | None = None,
                         gateway: Callable[[], Awaitable["str | None"]] | None = None) -> list[Device]:
    """Scan the LAN and return the resolved device inventory. `scan` is the
    injectable transport returning raw `arp -a` text (default: real ARP scan);
    inject a coroutine returning canned text to test without a network.
    `pins` maps mac -> user-pinned device_type (see build_inventory). `resolve`
    is an optional injectable coroutine mapping the parsed (ip, mac) entries to
    a {mac: hostname} dict (wavr.hostname_resolver.resolve_hostnames); its output
    feeds the hostnames= build parameter when hostnames is not supplied. `gateway`
    is an optional injectable coroutine returning THIS host's default-gateway IP;
    the device whose IP matches is flagged is_gateway (None-safe: no detector or
    an undetermined gateway simply leaves every is_gateway False, never guessed)."""
    raw = await (scan or _arp_output)()
    entries = parse_arp_inventory(raw)
    if hostnames is None and resolve is not None:
        # Populate the hostnames= build parameter from the injectable reverse-DNS
        # resolver -- tolerant: a resolver failure must never lose the whole scan.
        try:
            hostnames = await resolve(entries)
        except Exception:
            _LOG.warning("hostname resolver failed", exc_info=True)
            hostnames = None
    gateway_ip = None
    if gateway is not None:
        # Injectable default-gateway detector (wavr.sources.network.default_gateway)
        # -- tolerant, same rule as the resolver: a detection failure must never
        # lose the whole scan, it just leaves every is_gateway honestly False.
        try:
            gateway_ip = await gateway()
        except Exception:
            _LOG.warning("gateway detection failed", exc_info=True)
            gateway_ip = None
    return build_inventory(entries, known_macs, hostnames, pins, gateway_ip=gateway_ip)
