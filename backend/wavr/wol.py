"""Wake-on-LAN (A3.1) — a LAN-LOCAL actuator, ZERO external egress.

Sends a standard Wake-on-LAN magic packet (reusing netutils.build_magic_packet /
send_magic_packet) to wake a device already on the local network. WoL is an
actuator, so per Wavr's every-actuator OPT-IN + DEFAULT-OFF invariant it is
gated behind WAVR_NET_WOL (wol_enabled(), default OFF -> the route 503s) and the
require_local CSRF gate.

What leaves the box: nothing beyond the LAN. A 102-byte UDP magic packet to a
LAN/private broadcast address on port 0/7/9 only. A broadcast + port allowlist
keeps WoL from being turned into a unicast-to-internet UDP packet primitive
(an attacker who reached the route could otherwise spray crafted UDP at any
routable host). The raw packet bytes are never echoed back in the API response.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Callable

from wavr import netutils

# WoL is delivered by UDP to a broadcast address; the canonical, non-routable
# ports are 0 (reserved), 7 (echo) and 9 (discard). Anything else is refused so
# the route cannot be used to reach an arbitrary service port on a host.
_ALLOWED_PORTS = frozenset({0, 7, 9})


def wol_enabled() -> bool:
    """True only if WAVR_NET_WOL is explicitly enabled. OFF by default."""
    return os.getenv("WAVR_NET_WOL", "").strip().lower() in ("1", "true", "yes", "on")


def validate_broadcast(broadcast: str) -> str:
    """Return a normalized broadcast address, or raise ValueError.

    Accepts ONLY the limited broadcast 255.255.255.255 or a private / link-local
    / loopback address (RFC1918 subnet-directed broadcasts such as
    192.168.1.255 are private). Rejects any globally-routable / public IP so a
    WoL send can never be aimed at an internet host (which would make WoL a
    UDP-egress primitive, violating the zero-egress invariant)."""
    b = (broadcast or "").strip()
    if b == "255.255.255.255":
        return b
    try:
        addr = ipaddress.IPv4Address(b)   # WoL is IPv4 UDP broadcast only
    except ipaddress.AddressValueError:
        raise ValueError(f"invalid IPv4 broadcast address: {broadcast!r}")
    if not (addr.is_private or addr.is_link_local or addr.is_loopback):
        raise ValueError(
            f"broadcast must be a LAN/private address (zero-egress), not {broadcast!r}")
    return str(addr)


def validate_port(port: int) -> int:
    """Return the port if it is in the WoL allowlist {0, 7, 9}, else ValueError."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"invalid port: {port!r}")
    if p not in _ALLOWED_PORTS:
        raise ValueError(f"port must be one of {sorted(_ALLOWED_PORTS)}")
    return p


def wake(mac: str, broadcast: str = "255.255.255.255", port: int = 9,
         send: Callable[[bytes, str, int], None] | None = None) -> dict:
    """Validate the MAC + broadcast + port, then send a WoL magic packet.

    Returns a small API-safe dict (never the raw packet bytes). Raises
    ValueError on a malformed MAC (via build_magic_packet), a non-LAN broadcast,
    or a disallowed port. Inject `send` to test without opening a socket."""
    bcast = validate_broadcast(broadcast)
    prt = validate_port(port)
    packet = netutils.send_magic_packet(mac, broadcast=bcast, port=prt, send=send)
    normalized = mac.replace("-", ":").replace(".", ":").lower()
    return {
        "sent": True,
        "mac": normalized,
        "broadcast": bcast,
        "port": prt,
        "bytes": len(packet),
    }
