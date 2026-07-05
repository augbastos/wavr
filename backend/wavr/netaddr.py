"""Shared LAN-IP SSRF guard.

`is_lan_ip(host)` is the single hardened "is this a safe local-LAN IP literal?"
check reused across Wavr's LAN-touching write paths (ONVIF probe / PTZ, and the
F3 camera-rebind route). It is deliberately stronger than bare
`ipaddress.is_private`:

  * LITERAL-ONLY. A DNS hostname (not a bare IP literal) is refused -- without a
    scoped local resolver there is no guarantee it resolves on-LAN, and Wavr's
    zero-cloud-egress invariant means never taking that risk on a string an
    untrusted LAN device chose.
  * CLOUD-METADATA DENYLIST. 169.254.169.254 / fd00:ec2::254 are link-local, so
    they would otherwise pass the private/link-local allow -- denied explicitly
    (SSRF T2).
  * IPv4-MAPPED-IPv6 NORMALIZATION. `::ffff:169.254.169.254` is collapsed to its
    IPv4 form BEFORE the metadata/private tests, so the mapped form cannot slip
    past the denylist on a dual-stack host.

Extracted from wavr.sources.onvif._is_lan_ip so there is ONE implementation (a
second hand-maintained copy could drift and reopen the bypass). onvif.py re-
exports it as `_is_lan_ip` for its own internal uses / ptz.py / existing tests.
"""
from __future__ import annotations

import ipaddress

# Cloud metadata endpoints: link-local, so they pass the private/link-local allow --
# denied explicitly (SSRF T2). Both the AWS IMDS IPv4 and its IPv6 form.
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254"})
_METADATA_IPS = (
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
)


def is_lan_ip(host: str | None) -> bool:
    """True only if `host` is a LITERAL private/loopback/link-local IP address
    that is NOT a cloud-metadata endpoint. A DNS hostname (not a bare IP literal)
    is refused. Never raises."""
    if not host:
        return False
    h = host.strip().strip("[]")   # tolerate bracketed IPv6 literals
    if h in _METADATA_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    # Normalize the IPv4-mapped IPv6 form (::ffff:a.b.c.d) to its IPv4 address
    # BEFORE the metadata/private tests. On a dual-stack host
    # [::ffff:169.254.169.254] routes to the IPv4 IMDS endpoint, but the mapped
    # IPv6 object is not == the IPv4 metadata object and still reports
    # is_link_local True -- so without this it would slip past the denylist
    # (SSRF T2 bypass). Collapsing to the v4 form makes the denylist and the
    # non-LAN rejection (e.g. ::ffff:8.8.8.8) see the address that is really used.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA_IPS:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
