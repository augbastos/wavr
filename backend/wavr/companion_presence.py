"""Companion presence self-registration: a paired LAN companion (or the loopback
operator) registers ITS OWN device as a named presence signal.

The MAC is never client-supplied -- it is derived SERVER-SIDE from the
request's own source IP (`request.client.host`), resolved against the LAN ARP
table. This is the same MAC-from-ARP mechanism wavr.sources.network's presence
source and wavr.netinventory's inventory scan already use: `arp -a` text,
parsed into (ip, mac) pairs (wavr.netinventory.parse_arp_inventory). No
ping-sweep here -- unlike a full inventory scan, the caller's IP is, by
construction, already ARP-resident on this host (it just completed a TCP
handshake to reach this API), so a bare `arp -a` read is enough.

The resolved MAC is persisted via the existing consent-first IdentityStore
(source='network', origin='companion') -- the SAME registry whose
`as_net_map()` is already merged into NetworkSource's live known-device
provider (see app.py's `_net_known_provider`), so a registration takes effect
on the very next scan cycle with no restart, and survives one.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from wavr.netinventory import _same_ip, parse_arp_inventory
from wavr.sources import network


async def resolve_source_mac(ip: str,
                             arp_transport: "Callable[[], Awaitable[str]] | None" = None
                             ) -> "str | None":
    """Resolve `ip` to a MAC via the local ARP table. `arp_transport` is the
    injectable () -> Awaitable[str] returning raw `arp -a` text (tests inject
    canned output; the default calls the real `arp -a` subprocess, the same
    transport wavr.sources.network / wavr.netinventory use). Never raises --
    an unreachable/unavailable ARP transport or an IP absent from the table
    both resolve to None, so a caller can only ever get an HONEST "can't
    resolve", never a guessed/fabricated MAC."""
    if not ip:
        return None
    transport = arp_transport or (lambda: network._run("arp", "-a"))
    try:
        raw = await transport()
    except Exception:
        logging.warning("companion presence: ARP transport failed", exc_info=True)
        return None
    for entry_ip, mac in parse_arp_inventory(raw):
        if _same_ip(entry_ip, ip):
            return mac
    return None


def mac_prefix(mac: str) -> str:
    """First 3 octets only (e.g. 'aa:bb:cc') -- the API response never echoes
    the full MAC back to the caller."""
    return ":".join(mac.split(":")[:3])
