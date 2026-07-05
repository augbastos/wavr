"""wavr.sources.netbios -- active, targeted NBSTAT collector."""
from __future__ import annotations

import struct

from wavr.sources.netbios import (
    NetBIOSCollector,
    _encode_nbname,
    build_nbstat_query,
    parse_nbstat_response,
)


def _name_entry(name: str, suffix: int, is_group: bool = False) -> bytes:
    padded = name.ljust(15)[:15].encode("ascii")
    flags = 0x8000 if is_group else 0x0000
    return padded + bytes([suffix]) + struct.pack(">H", flags)


def _nbstat_response(entries: list[bytes], mac: bytes | None = b"\xaa\xbb\xcc\x00\x11\x22") -> bytes:
    header = struct.pack(">HHHHHH", 0x1337, 0x8400, 0, 1, 0, 0)
    encoded_name = _encode_nbname(b"*" + b"\x00" * 15)
    rr_name = bytes([len(encoded_name)]) + encoded_name + b"\x00"
    rdata = bytes([len(entries)]) + b"".join(entries) + (mac or b"")
    rr = rr_name + struct.pack(">HHIH", 0x21, 1, 0, len(rdata)) + rdata
    return header + rr


_WINDOWS_PC_RESPONSE = _nbstat_response([
    _name_entry("DESKTOP-A1B2C3", 0x00, is_group=False),
    _name_entry("WORKGROUP", 0x00, is_group=True),
    _name_entry("DESKTOP-A1B2C3", 0x20, is_group=False),  # file server (sharing enabled)
])

_DOMAIN_CONTROLLER_RESPONSE = _nbstat_response([
    _name_entry("DC01", 0x00, is_group=False),
    _name_entry("CORP", 0x00, is_group=True),
    _name_entry("CORP", 0x1C, is_group=True),   # domain controllers group
    _name_entry("DC01", 0x1B, is_group=False),  # domain master browser
])


# ---- query encoding ----------------------------------------------------------

def test_query_uses_wildcard_name_and_nbstat_type():
    query = build_nbstat_query()
    assert query[12] == 0x20  # 32-byte first-level-encoded label length
    # "*" (0x2A) -> high nibble 2 -> 'C' (0x41+2), low nibble A -> 'K' (0x41+10)
    assert query[13:15] == b"CK"
    qtype, qclass = struct.unpack(">HH", query[-4:])
    assert qtype == 0x0021
    assert qclass == 0x0001


def test_encode_nbname_produces_32_ascii_chars():
    encoded = _encode_nbname(b"*" + b"\x00" * 15)
    assert len(encoded) == 32
    assert all(0x41 <= b <= 0x50 for b in encoded)


# ---- response parsing ---------------------------------------------------------

def test_parses_windows_pc_name_workgroup_and_file_server_flag():
    parsed = parse_nbstat_response(_WINDOWS_PC_RESPONSE)
    names = {(e["name"], e["suffix"], e["is_group"]) for e in parsed["entries"]}
    assert ("DESKTOP-A1B2C3", 0x00, False) in names
    assert ("WORKGROUP", 0x00, True) in names
    assert parsed["mac"] == "aa:bb:cc:00:11:22"


def test_malformed_response_does_not_raise():
    assert parse_nbstat_response(b"\xff\xfe garbage") == {}
    assert parse_nbstat_response(b"") == {}
    assert parse_nbstat_response(b"\x00" * 11) == {}  # shorter than the 12-byte header


def test_zero_mac_statistics_block_is_treated_as_absent():
    resp = _nbstat_response([_name_entry("HOST1", 0x00)], mac=b"\x00" * 6)
    parsed = parse_nbstat_response(resp)
    assert parsed["mac"] is None


# ---- NetBIOSCollector end-to-end (fake prober, zero real sockets) -----------

async def test_collector_windows_pc_reports_name_workgroup_and_file_server():
    async def prober(ip, request):
        assert ip == "192.168.1.10"
        return _WINDOWS_PC_RESPONSE

    out = (await NetBIOSCollector(targets=["192.168.1.10"], prober=prober).collect())["192.168.1.10"]
    assert out["name"] == "DESKTOP-A1B2C3"
    assert out["workgroup"] == "WORKGROUP"
    assert out["is_file_server"] is True
    assert out["is_domain_controller"] is False
    assert out["mac"] == "aa:bb:cc:00:11:22"
    assert out["make"] is None
    assert out["model"] is None
    assert out["os"] is None
    # "DESKTOP-A1B2C3" matches hostname_type's desktop pattern.
    assert out["device_type"] == "desktop"


async def test_collector_domain_controller_flags():
    async def prober(ip, request):
        return _DOMAIN_CONTROLLER_RESPONSE

    out = (await NetBIOSCollector(targets=["192.168.1.11"], prober=prober).collect())["192.168.1.11"]
    assert out["name"] == "DC01"
    assert out["workgroup"] == "CORP"
    assert out["is_domain_controller"] is True
    assert out["is_file_server"] is False


async def test_ip_to_mac_mapping_keys_by_mac():
    async def prober(ip, request):
        return _WINDOWS_PC_RESPONSE

    out = await NetBIOSCollector(
        targets=["192.168.1.10"], prober=prober,
        ip_to_mac={"192.168.1.10": "AA-BB-CC-00-11-22"},
    ).collect()
    assert "aa:bb:cc:00:11:22" in out
    assert "192.168.1.10" not in out


async def test_unreachable_host_is_skipped_not_raised():
    async def prober(ip, request):
        raise TimeoutError("no response")

    out = await NetBIOSCollector(targets=["10.0.0.99"], prober=prober).collect()
    assert out == {}


async def test_empty_name_table_is_skipped():
    async def prober(ip, request):
        return _nbstat_response([])

    out = await NetBIOSCollector(targets=["10.0.0.5"], prober=prober).collect()
    assert out == {}
