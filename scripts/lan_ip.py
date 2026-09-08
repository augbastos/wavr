"""Best-effort discovery of this machine's own LAN (Wi-Fi) IPv4 address.

The first-user portal must bind to an address the S25, the tablet and the
the field device can all reach over the home Wi-Fi -- not `127.0.0.1` (only this
machine can reach that) and not `0.0.0.0` (which would also listen on a VPN
adapter or anything else this laptop happens to be attached to, more than
the mandate for this portal asks for). So this asks the OS which source
address it would use to leave the local network, the same technique already
proven in `backend/tests/test_companion_sees_a_live_core.py::_lan_ip`.

    python -c "from lan_ip import get_lan_ip; print(get_lan_ip())"
"""
from __future__ import annotations

import ipaddress
import socket


def get_lan_ip() -> str | None:
    """This host's private (RFC1918) address, or None if it has none.

    Opens a UDP socket and "connects" it to an address on the far side of
    any home router (`10.255.255.255`); UDP has no handshake, so nothing is
    actually sent, but the OS still has to pick a source address for the
    route, and that is the address other devices on this Wi-Fi can use to
    reach this machine. A loopback or non-private result (no network,
    firewalled routing table, single NIC in a weird state) is treated the
    same as failure: it is not an address anything else on the LAN can use.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("10.255.255.255", 1))
            address = sock.getsockname()[0]
    except OSError:
        return None
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    return address if parsed.is_private and not parsed.is_loopback else None


if __name__ == "__main__":
    print(get_lan_ip() or "(no LAN address found)")
