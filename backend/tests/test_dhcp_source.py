"""wavr.sources.dhcp -- passive/active DHCP collector.

Builds representative BOOTP/DHCP packets by hand (mirrors the parser's own
wire format) so every test runs with zero real sockets."""
from __future__ import annotations

import socket
import struct

from wavr.sources.dhcp import (
    MAGIC_COOKIE,
    DHCPCollector,
    build_discover_packet,
    parse_dhcp_packet,
)


def _dhcp_packet(msg_type: int, server_id: str | None = None,
                  yiaddr: str = "192.168.1.50", mac: str = "aa:bb:cc:dd:ee:ff",
                  op: int = 2) -> bytes:
    chaddr = bytes.fromhex(mac.replace(":", "")).ljust(16, b"\x00")
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        op, 1, 6, 0, 0x1234, 0, 0,
        b"\x00\x00\x00\x00",
        socket.inet_aton(yiaddr),
        b"\x00\x00\x00\x00",
        b"\x00\x00\x00\x00",
        chaddr, b"\x00" * 64, b"\x00" * 128,
    )
    options = MAGIC_COOKIE
    options += bytes([53, 1, msg_type])
    if server_id:
        options += bytes([54, 4]) + socket.inet_aton(server_id)
    options += bytes([255])
    return header + options


def _fake_source(packets):
    async def listen():
        for data, ip in packets:
            yield data, ip
    return listen


# ---- parse_dhcp_packet ------------------------------------------------------

def test_parse_offer_extracts_server_id_and_yiaddr():
    pkt = _dhcp_packet(2, server_id="192.168.1.1", yiaddr="192.168.1.77")
    parsed = parse_dhcp_packet(pkt)
    assert parsed["msg_type"] == 2
    assert parsed["server_id"] == "192.168.1.1"
    assert parsed["yiaddr"] == "192.168.1.77"
    assert parsed["mac"] == "aa:bb:cc:dd:ee:ff"


def test_parse_rejects_too_short_packet():
    assert parse_dhcp_packet(b"\x00" * 50) is None


def test_parse_rejects_missing_magic_cookie():
    pkt = bytearray(_dhcp_packet(2))
    pkt[236:240] = b"\x00\x00\x00\x00"
    assert parse_dhcp_packet(bytes(pkt)) is None


def test_parse_never_raises_on_truncated_options():
    pkt = _dhcp_packet(2, server_id="192.168.1.1")[:250]  # cut mid-option
    parsed = parse_dhcp_packet(pkt)   # must not raise regardless of the result shape
    assert parsed is None or isinstance(parsed, dict)


def test_parse_hostile_garbage_returns_none():
    assert parse_dhcp_packet(b"\xff" * 300) is None


# ---- build_discover_packet ---------------------------------------------------

def test_discover_packet_roundtrips_as_discover_with_broadcast_flag():
    pkt = build_discover_packet(xid=0xdeadbeef)
    parsed = parse_dhcp_packet(pkt)
    assert parsed["msg_type"] == 1        # MSG_DISCOVER
    flags = struct.unpack("!H", pkt[10:12])[0]
    assert flags == 0x8000                # broadcast bit set


def test_discover_packet_uses_locally_administered_throwaway_mac():
    pkt = build_discover_packet()
    chaddr_first_byte = pkt[28]
    assert chaddr_first_byte & 0x02        # U/L bit set -- never a real vendor MAC


def test_discover_packet_xid_is_randomized_when_unset():
    a = build_discover_packet()
    b = build_discover_packet()
    assert a[4:8] != b[4:8]


# ---- DHCPCollector.collect ---------------------------------------------------

async def test_collect_counts_distinct_offer_servers():
    packets = [
        (_dhcp_packet(2, server_id="192.168.1.1"), "192.168.1.1"),
        (_dhcp_packet(2, server_id="10.0.0.99"), "10.0.0.99"),
    ]
    c = DHCPCollector(listen=_fake_source(packets))
    result = await c.collect(duration=0.01)
    assert set(result.keys()) == {"192.168.1.1", "10.0.0.99"}
    assert result["192.168.1.1"]["offers"] == 1


async def test_collect_ignores_ack_and_discover_messages():
    packets = [
        (_dhcp_packet(5, server_id="192.168.1.1"), "192.168.1.1"),   # ACK
        (_dhcp_packet(1), "192.168.1.50"),                            # DISCOVER (from a client)
    ]
    c = DHCPCollector(listen=_fake_source(packets))
    result = await c.collect(duration=0.01)
    assert result == {}


async def test_collect_falls_back_to_source_ip_when_no_server_id_option():
    packets = [(_dhcp_packet(2, server_id=None), "192.168.1.5")]
    c = DHCPCollector(listen=_fake_source(packets))
    result = await c.collect(duration=0.01)
    assert set(result.keys()) == {"192.168.1.5"}


async def test_collect_tolerates_malformed_packets():
    packets = [(b"\xff" * 10, "1.2.3.4"), (_dhcp_packet(2, server_id="1.1.1.1"), "1.1.1.1")]
    c = DHCPCollector(listen=_fake_source(packets))
    result = await c.collect(duration=0.01)
    assert set(result.keys()) == {"1.1.1.1"}


async def test_collect_repeated_offers_from_same_server_increment_count():
    packets = [
        (_dhcp_packet(2, server_id="192.168.1.1"), "192.168.1.1"),
        (_dhcp_packet(2, server_id="192.168.1.1"), "192.168.1.1"),
    ]
    c = DHCPCollector(listen=_fake_source(packets))
    result = await c.collect(duration=0.01)
    assert result["192.168.1.1"]["offers"] == 2
