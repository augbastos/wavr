"""Pure authorization logic for multi-device client auth (ADR-0006).

No FastAPI, no I/O beyond the injected DeviceStore — just plain functions so the
load-bearing access decision is fully unit-testable in isolation. The app wiring
(app.py) calls `authorize` from the request middleware and maps its result to
allow/deny + role.

The rule (only reached when `WAVR_MULTIDEVICE` is on; otherwise app.py stays
strictly loopback-only, unchanged):

  * loopback peer                      -> "root"  (the DB-owning root central)
  * in-subnet peer + valid token       -> the device's role ("central" / "user")
  * everything else                    -> None    (deny / 403)

On-Wi-Fi alone is never enough: a valid, non-revoked per-device token is required,
AND the peer must be in the host's local /24.
"""
from __future__ import annotations

import ipaddress

# Mirrors app.py's loopback set (kept local so this module has no app.py import).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header, or None if
    the header is absent/malformed. Case-insensitive on the scheme."""
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def in_subnet(peer_host: str, local_ip: str) -> bool:
    """True iff `peer_host` is in the same IPv4 /24 as `local_ip`. Returns False on
    any unparseable/non-IPv4 input (fail-closed) — the loopback root path is handled
    separately in `authorize`, so this is only ever asked about LAN peers."""
    try:
        peer = ipaddress.ip_address(peer_host)
        local = ipaddress.ip_address(local_ip)
    except (ValueError, TypeError):
        return False
    if peer.version != 4 or local.version != 4:
        return False
    net = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    return peer in net


def authorize(peer_host, host_subnet, bearer_token, device_store) -> str | None:
    """Decide the caller's role, or None to deny.

    * `peer_host`    — the request's peer IP (request.client.host).
    * `host_subnet`  — the central's own LAN IP; its /24 defines "same Wi-Fi".
    * `bearer_token` — the raw token from the Authorization header (already parsed).
    * `device_store` — a DeviceStore for token verification.

    Returns "root" for loopback, the device role for a valid in-subnet token, else
    None. An out-of-subnet peer is rejected *before* any token lookup, so a stolen
    token is useless off the LAN and never even touches the store.
    """
    if peer_host in _LOOPBACK_HOSTS:
        return "root"
    if not bearer_token:
        return None
    if not in_subnet(peer_host, host_subnet):
        return None
    device = device_store.verify(bearer_token)
    if device is None:
        return None
    return device.role


# Role hierarchy helpers used by the per-route gate in app.py.
def can_change_state(role: str | None) -> bool:
    """State-changing routes (sources/cameras/config/pairing) require central+."""
    return role in ("root", "central")


def can_view(role: str | None) -> bool:
    """Read-only GETs + /ws/live are open to any authenticated role."""
    return role in ("root", "central", "user")
