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
import re
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

from wavr.data.oui import (
    VENDOR_DEVICE_TYPE,
    is_locally_administered,
    lookup_vendor,
)
from wavr.sources import network

# ip + mac on one `arp -a` line. Separator-agnostic (Windows "-", Unix ":").
_ARP_LINE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})"
)

# hostname substring -> device type. Checked before the vendor guess because a
# hostname ("Johns-iPhone") is a stronger signal than the OUI's silicon maker.
_HOSTNAME_HINTS: tuple[tuple[str, str], ...] = (
    ("iphone", "phone"),
    ("ipad", "tablet"),
    ("android", "phone"),
    ("pixel", "phone"),
    ("galaxy", "phone"),
    ("macbook", "computer"),
    ("imac", "computer"),
    ("laptop", "computer"),
    ("desktop", "computer"),
    ("printer", "printer"),
    ("camera", "camera"),
    ("cam", "camera"),
    ("tv", "tv"),
    ("roku", "streaming"),
    ("chromecast", "streaming"),
    ("echo", "smart-speaker"),
    ("alexa", "smart-speaker"),
    ("sonos", "speaker"),
    ("router", "network-gear"),
    ("switch", "network-gear"),
    ("nas", "storage"),
    ("esp", "iot-embedded"),
    ("watch", "wearable"),
)


@dataclass(frozen=True)
class Device:
    """One host seen on the LAN. `known` = MAC is on the allowlist.
    `risks` holds optional report-only risk notes from wavr.netutils port
    awareness (empty unless that opt-in pass ran)."""
    mac: str
    ip: str | None
    vendor: str
    device_type: str
    known: bool
    hostname: str | None = None
    risks: tuple = ()      # tuple[str, ...] — optional last field

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risks"] = list(d["risks"])
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
    """Coarse device-type guess from hostname hints, then vendor class. Falls
    back to a randomized-MAC heuristic (privacy phones), else "unknown"."""
    if hostname:
        low = hostname.lower()
        for needle, dtype in _HOSTNAME_HINTS:
            if needle in low:
                return dtype
    if vendor in VENDOR_DEVICE_TYPE:
        return VENDOR_DEVICE_TYPE[vendor]
    if mac and vendor == "unknown" and is_locally_administered(mac):
        return "mobile?"  # randomized MAC, no OUI -- most likely a phone
    return "unknown"


def _norm_macs(macs) -> set[str]:
    return {m.strip().replace("-", ":").lower() for m in (macs or ()) if m.strip()}


def build_inventory(entries: list[tuple[str, str]], known_macs=None,
                    hostnames: dict[str, str] | None = None) -> list[Device]:
    """Turn (ip, mac) pairs into resolved Device records. `known_macs` is the
    allowlist; `hostnames` optionally maps mac -> hostname for a better type
    guess. Pure/offline -- no I/O."""
    known = _norm_macs(known_macs)
    hostnames = hostnames or {}
    out: list[Device] = []
    for ip, mac in entries:
        mac = mac.replace("-", ":").lower()
        hostname = hostnames.get(mac)
        vendor = lookup_vendor(mac)
        out.append(Device(
            mac=mac,
            ip=ip,
            vendor=vendor,
            device_type=guess_device_type(vendor, hostname, mac),
            known=mac in known,
            hostname=hostname,
        ))
    return out


async def _arp_output() -> str:
    """Default real transport: warm the ARP cache with a local /24 ping sweep,
    then return raw `arp -a` text. Scans ONLY the LAN this host is already on --
    no sniffing, no injection. Reuses wavr.sources.network's subprocess seam so
    tests can inject a mock transport instead."""
    ip = network._local_ipv4()
    if ip:
        net = ipaddress.ip_network(ip + "/24", strict=False)

        async def ping(addr: str) -> None:
            with contextlib.suppress(Exception):
                await network._run("ping", "-n", "1", "-w", "200", addr)

        await asyncio.gather(*(ping(str(h)) for h in net.hosts()))
    return await network._run("arp", "-a")


async def scan_inventory(known_macs=None,
                         scan: Callable[[], Awaitable[str]] | None = None,
                         hostnames: dict[str, str] | None = None) -> list[Device]:
    """Scan the LAN and return the resolved device inventory. `scan` is the
    injectable transport returning raw `arp -a` text (default: real ARP scan);
    inject a coroutine returning canned text to test without a network."""
    raw = await (scan or _arp_output)()
    return build_inventory(parse_arp_inventory(raw), known_macs, hostnames)
