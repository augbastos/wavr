"""Gateway-identity flag (wifiman.md #2) + per-device latency wiring
(wifiman.md #1). Mock-tested with zero real network / zero subprocess:
the gateway detector and the latency ping are injectable seams."""
import wavr.sources.network as netmod
from wavr.api_inventory import _device_view
from wavr.config import load_config
from wavr.netinventory import Device, build_inventory, parse_arp_inventory, scan_inventory
from wavr.netinventory_service import NetworkInventoryService
from wavr.sources.network import default_gateway, parse_default_gateway

WINDOWS_ARP = (
    "\nInterface: 192.168.0.10 --- 0x5\n"
    "  Internet Address      Physical Address      Type\n"
    "  192.168.0.1           A4-83-E7-11-22-33     dynamic\n"
    "  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic\n"
    "  192.168.0.42          DE-AD-BE-EF-00-01     dynamic\n"
)

IPCONFIG = (
    "\nWindows IP Configuration\n\n"
    "Ethernet adapter Ethernet:\n"
    "   IPv4 Address. . . . . . . . . . . : 192.168.0.10\n"
    "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
    "   Default Gateway . . . . . . . . . : 192.168.0.1\n"
)


def test_parse_gateway_windows_single_line():
    assert parse_default_gateway(IPCONFIG) == "192.168.0.1"


def test_parse_gateway_windows_dual_stack_ipv4_on_continuation():
    txt = ("   Default Gateway . . . . . . . . . : fe80::1%12\n"
           "                                       192.168.1.254\n"
           "   DHCP Server . . . . . . . . . . . : 192.168.1.99\n")
    assert parse_default_gateway(txt) == "192.168.1.254"


def test_parse_gateway_skips_empty_and_zero_and_picks_real():
    txt = ("Ethernet adapter VirtualBox Host-Only Network:\n"
           "   Default Gateway . . . . . . . . . :\n"
           "Wireless LAN adapter Wi-Fi:\n"
           "   Default Gateway . . . . . . . . . : 10.0.0.138\n")
    assert parse_default_gateway(txt) == "10.0.0.138"
    assert parse_default_gateway("   Default Gateway . . . : 0.0.0.0\n") is None


def test_parse_gateway_linux_and_mac_and_none():
    assert parse_default_gateway("default via 192.168.1.1 dev wlan0 metric 600") == "192.168.1.1"
    assert parse_default_gateway("    gateway: 192.168.0.1\n  interface: en0") == "192.168.0.1"
    assert parse_default_gateway("no gateway here") is None


async def test_default_gateway_uses_injected_run(monkeypatch):
    async def fake_run(*args):
        return IPCONFIG
    monkeypatch.setattr(netmod, "_run", fake_run)
    assert await default_gateway() == "192.168.0.1"


async def test_default_gateway_none_when_command_fails(monkeypatch):
    async def boom(*args):
        raise OSError("no such command")
    monkeypatch.setattr(netmod, "_run", boom)
    assert await default_gateway() is None


def test_build_inventory_flags_only_the_gateway_ip():
    inv = build_inventory(parse_arp_inventory(WINDOWS_ARP), gateway_ip="192.168.0.1")
    by_mac = {d.mac: d for d in inv}
    assert by_mac["a4:83:e7:11:22:33"].is_gateway is True
    assert by_mac["24:0a:c4:aa:bb:cc"].is_gateway is False
    assert by_mac["de:ad:be:ef:00:01"].is_gateway is False


def test_build_inventory_no_gateway_ip_flags_nothing():
    inv = build_inventory(parse_arp_inventory(WINDOWS_ARP))
    assert all(d.is_gateway is False for d in inv)
