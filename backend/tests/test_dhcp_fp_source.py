"""wavr.sources.dhcp_fp -- passive DHCP-fingerprint collector."""
from __future__ import annotations

from wavr.sources.dhcp_fp import (
    DHCPFingerprintCollector,
    _infer_os,
    parse_dhcp_packet,
)

_MAGIC_COOKIE = b"\x63\x82\x53\x63"


def _bootp_header(mac: bytes, op: int = 1) -> bytes:
    # op(1) htype(1) hlen(1) hops(1) xid(4) secs(2) flags(2)
    # ciaddr(4) yiaddr(4) siaddr(4) giaddr(4) chaddr(16) sname(64) file(128)
    header = bytes([op, 1, 6, 0]) + b"\x00" * 4 + b"\x00" * 2 + b"\x00" * 2
    header += b"\x00" * 4 * 4  # ciaddr/yiaddr/siaddr/giaddr
    header += mac.ljust(16, b"\x00")
    header += b"\x00" * 64  # sname
    header += b"\x00" * 128  # file
    assert len(header) == 236
    return header


def _option(code: int, value: bytes) -> bytes:
    return bytes([code, len(value)]) + value


def _dhcp_packet(mac: bytes, msg_type: int, vendor_class: str | None = None,
                  param_request_list: list[int] | None = None, op: int = 1) -> bytes:
    options = _option(53, bytes([msg_type]))
    if vendor_class is not None:
        options += _option(60, vendor_class.encode())
    if param_request_list is not None:
        options += _option(55, bytes(param_request_list))
    options += b"\xff"  # END
    return _bootp_header(mac, op=op) + _MAGIC_COOKIE + options


_WINDOWS_DISCOVER = _dhcp_packet(
    b"\xaa\xbb\xcc\x00\x11\x22", msg_type=1, vendor_class="MSFT 5.0",
    param_request_list=[1, 15, 3, 6, 44, 46, 47, 31, 33, 121, 249, 43],
)

_ANDROID_REQUEST = _dhcp_packet(
    b"\xaa\xbb\xcc\x00\x11\x33", msg_type=3, vendor_class="android-dhcp-14",
)

_ROUTER_DISCOVER = _dhcp_packet(
    b"\xaa\xbb\xcc\x00\x11\x44", msg_type=1, vendor_class="udhcp 1.30.1",
)

_WPAD_ONLY_REQUEST = _dhcp_packet(
    b"\xaa\xbb\xcc\x00\x11\x55", msg_type=3, vendor_class=None,
    param_request_list=[1, 3, 6, 15, 252],
)

_OFFER_IS_IGNORED = _dhcp_packet(
    b"\xaa\xbb\xcc\x00\x11\x66", msg_type=2, vendor_class="MSFT 5.0", op=2,
)


# ---- packet parsing ------------------------------------------------------------

def test_parses_windows_discover():
    parsed = parse_dhcp_packet(_WINDOWS_DISCOVER)
    assert parsed["mac"] == "aa:bb:cc:00:11:22"
    assert parsed["vendor_class"] == "MSFT 5.0"
    assert 249 in parsed["param_request_list"]


def test_bootreply_offer_is_ignored():
    assert parse_dhcp_packet(_OFFER_IS_IGNORED) is None


def test_missing_magic_cookie_is_ignored():
    assert parse_dhcp_packet(b"\x01\x01\x06\x00" + b"\x00" * 300) is None


def test_zero_chaddr_is_ignored():
    packet = _dhcp_packet(b"\x00" * 6, msg_type=1, vendor_class="MSFT 5.0")
    assert parse_dhcp_packet(packet) is None


def test_malformed_bytes_do_not_raise():
    assert parse_dhcp_packet(b"\xff\xfe garbage") is None
    assert parse_dhcp_packet(b"") is None


def test_truncated_option_does_not_raise():
    # Cuts option 60's (vendor class) value short -- the parser must stop
    # cleanly and return whatever it already had (msg_type was parsed first),
    # never raise on the truncated trailing option.
    truncated = _WINDOWS_DISCOVER[:250]
    parsed = parse_dhcp_packet(truncated)
    assert parsed is not None
    assert parsed["mac"] == "aa:bb:cc:00:11:22"


# ---- OS inference --------------------------------------------------------------

def test_infer_os_from_vendor_class_prefixes():
    assert _infer_os("MSFT 5.0", []) == "Windows"
    assert _infer_os("android-dhcp-14", []) == "Android"
    assert _infer_os("udhcp 1.30.1", []) == "Linux"
    assert _infer_os("dhcpcd-9.4.1", []) == "Linux"
    assert _infer_os("SomeTotallyUnknownClient", []) is None


def test_infer_os_falls_back_to_wpad_option_252():
    assert _infer_os(None, [1, 3, 6, 15, 252]) == "Windows"
    assert _infer_os(None, [1, 3, 6, 15]) is None


# ---- DHCPFingerprintCollector end-to-end (fake transport, zero real sockets) --

async def test_collector_windows_discover_resolves_os():
    async def listen():
        yield _WINDOWS_DISCOVER, "0.0.0.0"

    out = (await DHCPFingerprintCollector(listen=listen).collect(duration=0.2))["aa:bb:cc:00:11:22"]
    assert out["os"] == "Windows"
    assert out["vendor_class"] == "MSFT 5.0"


async def test_collector_android_request_resolves_os():
    async def listen():
        yield _ANDROID_REQUEST, "0.0.0.0"

    out = (await DHCPFingerprintCollector(listen=listen).collect(duration=0.2))["aa:bb:cc:00:11:33"]
    assert out["os"] == "Android"


async def test_collector_ignores_offer_packets():
    async def listen():
        yield _OFFER_IS_IGNORED, "192.168.1.1"

    out = await DHCPFingerprintCollector(listen=listen).collect(duration=0.2)
    assert out == {}


async def test_collector_keys_by_chaddr_mac_not_source_ip():
    async def listen():
        yield _ROUTER_DISCOVER, "0.0.0.0"  # DISCOVER: client has no IP yet

    out = await DHCPFingerprintCollector(listen=listen).collect(duration=0.2)
    assert "aa:bb:cc:00:11:44" in out
    assert "0.0.0.0" not in out
    assert out["aa:bb:cc:00:11:44"]["os"] == "Linux"


async def test_collector_merges_discover_then_request_without_duplicating():
    async def listen():
        yield _WINDOWS_DISCOVER, "0.0.0.0"
        yield _WPAD_ONLY_REQUEST.replace(b"\xaa\xbb\xcc\x00\x11\x55", b"\xaa\xbb\xcc\x00\x11\x22"), "0.0.0.0"

    out = await DHCPFingerprintCollector(listen=listen).collect(duration=0.2)
    assert len(out) == 1
    assert out["aa:bb:cc:00:11:22"]["os"] == "Windows"  # first-seen value kept, not overwritten
