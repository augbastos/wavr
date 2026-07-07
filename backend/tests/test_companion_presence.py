"""Unit tests for the IP -> MAC ARP resolution seam (wavr.companion_presence).
Fully offline: no real subprocess, canned `arp -a` text injected as the
transport (same style as test_netinventory.py's WINDOWS_ARP fixture)."""
import pytest

from wavr.companion_presence import mac_prefix, resolve_source_mac

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
  192.168.0.255         FF-FF-FF-FF-FF-FF     static
"""


async def test_resolves_known_ip_to_normalized_mac():
    async def transport():
        return WINDOWS_ARP
    mac = await resolve_source_mac("192.168.0.23", arp_transport=transport)
    assert mac == "24:0a:c4:aa:bb:cc"


async def test_unknown_ip_returns_none():
    async def transport():
        return WINDOWS_ARP
    assert await resolve_source_mac("192.168.0.99", arp_transport=transport) is None


async def test_broadcast_row_never_matches_as_a_device():
    # 192.168.0.255 is present in the table but maps to a broadcast/multicast MAC
    # -- parse_arp_inventory drops it, so it must not be resolvable to anything.
    async def transport():
        return WINDOWS_ARP
    assert await resolve_source_mac("192.168.0.255", arp_transport=transport) is None


async def test_empty_ip_returns_none_without_calling_transport():
    called = False

    async def transport():
        nonlocal called
        called = True
        return WINDOWS_ARP
    assert await resolve_source_mac("", arp_transport=transport) is None
    assert await resolve_source_mac(None, arp_transport=transport) is None
    assert called is False


async def test_transport_failure_is_honest_none_not_a_crash():
    async def transport():
        raise OSError("arp not available / not rooted")
    assert await resolve_source_mac("192.168.0.23", arp_transport=transport) is None


async def test_default_transport_used_when_none_injected(monkeypatch):
    # Wires to the real subprocess seam (wavr.sources.network._run) -- verified
    # by monkeypatching that one function rather than actually spawning `arp`.
    async def fake_run(*args):
        assert args == ("arp", "-a")
        return WINDOWS_ARP
    monkeypatch.setattr("wavr.sources.network._run", fake_run)
    mac = await resolve_source_mac("192.168.0.23")
    assert mac == "24:0a:c4:aa:bb:cc"


def test_mac_prefix_masks_the_full_address():
    assert mac_prefix("24:0a:c4:aa:bb:cc") == "24:0a:c4"
