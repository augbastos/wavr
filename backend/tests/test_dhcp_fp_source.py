"""wavr.sources.dhcp_fp -- passive DHCP-fingerprint collector."""
from __future__ import annotations

import pytest

from wavr.sources import _dhcp_raw, dhcp_fp
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


# ---- honest availability signal (panel-review finding #9/#17) ------------------

async def test_collector_available_none_before_first_collect():
    async def listen():
        yield _WINDOWS_DISCOVER, "0.0.0.0"

    collector = DHCPFingerprintCollector(listen=listen)
    assert collector.available is None
    assert collector.unavailable_reason is None


async def test_collector_available_true_after_a_clean_run():
    async def listen():
        yield _WINDOWS_DISCOVER, "0.0.0.0"

    collector = DHCPFingerprintCollector(listen=listen)
    await collector.collect(duration=0.2)
    assert collector.available is True
    assert collector.unavailable_reason is None


async def test_collector_available_false_on_permission_error_bind_failure():
    # Simulates a non-root proot/container lacking CAP_NET_BIND_SERVICE: the
    # transport raises the moment it's first iterated, before any packet.
    async def listen():
        raise PermissionError("[Errno 13] Permission denied")
        yield  # pragma: no cover -- makes this an async generator

    collector = DHCPFingerprintCollector(listen=listen)
    out = await collector.collect(duration=0.2)
    assert out == {}   # never raises up to the caller -- swallowed, recorded instead
    assert collector.available is False
    assert "PermissionError" in collector.unavailable_reason


async def test_collector_available_false_on_plain_os_error_bind_failure():
    # A real DHCP server already holding the port exclusively surfaces as a
    # plain OSError (not necessarily PermissionError) on some platforms.
    async def listen():
        raise OSError("Address already in use")
        yield  # pragma: no cover

    collector = DHCPFingerprintCollector(listen=listen)
    out = await collector.collect(duration=0.2)
    assert out == {}
    assert collector.available is False
    assert "Address already in use" in collector.unavailable_reason


async def test_collector_reflects_most_recent_attempt_across_repeated_collect():
    # A source of truth that flips honestly with the environment, not a
    # sticky "unavailable forever" flag -- mirrors RogueDhcpMonitor's
    # equivalent contract.
    state = {"boom": True}

    async def listen():
        if state["boom"]:
            raise PermissionError("Permission denied")
        yield _WINDOWS_DISCOVER, "0.0.0.0"

    collector = DHCPFingerprintCollector(listen=listen)
    await collector.collect(duration=0.2)
    assert collector.available is False

    state["boom"] = False
    await collector.collect(duration=0.2)
    assert collector.available is True
    assert collector.unavailable_reason is None


# ---- _default_listen raw-first / UDP-fallback orchestration (G9 Core fix) ---
# `listen=` injection (used above) bypasses `_default_listen` entirely, so
# these exercise the real transport-selection function directly, with the
# underlying socket layer monkeypatched (never a real socket in CI).

async def test_default_listen_uses_raw_when_supported(monkeypatch):
    async def fake_raw():
        yield b"raw-payload", "192.168.1.9"

    async def _udp_must_not_be_called():
        raise AssertionError("UDP fallback must not run when raw succeeds")
        yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(dhcp_fp, "raw_af_packet_supported", lambda: True)
    monkeypatch.setattr(dhcp_fp, "raw_dhcp_listen", fake_raw)
    monkeypatch.setattr(dhcp_fp, "_udp_listen", _udp_must_not_be_called)

    items = [item async for item in dhcp_fp._default_listen()]
    assert items == [(b"raw-payload", "192.168.1.9")]


async def test_default_listen_falls_back_to_udp_on_raw_permission_error(monkeypatch):
    async def fake_raw_denied():
        raise PermissionError("no CAP_NET_RAW")
        yield  # pragma: no cover

    async def fake_udp():
        yield b"udp-payload", "10.0.0.1"

    monkeypatch.setattr(dhcp_fp, "raw_af_packet_supported", lambda: True)
    monkeypatch.setattr(dhcp_fp, "raw_dhcp_listen", fake_raw_denied)
    monkeypatch.setattr(dhcp_fp, "_udp_listen", fake_udp)

    items = [item async for item in dhcp_fp._default_listen()]
    assert items == [(b"udp-payload", "10.0.0.1")]


async def test_default_listen_skips_raw_entirely_when_unsupported(monkeypatch):
    async def _raw_must_not_be_called():
        raise AssertionError("raw path must not be attempted when unsupported")
        yield  # pragma: no cover

    async def fake_udp():
        yield b"udp-only", "10.0.0.2"

    monkeypatch.setattr(dhcp_fp, "raw_af_packet_supported", lambda: False)
    monkeypatch.setattr(dhcp_fp, "raw_dhcp_listen", _raw_must_not_be_called)
    monkeypatch.setattr(dhcp_fp, "_udp_listen", fake_udp)

    items = [item async for item in dhcp_fp._default_listen()]
    assert items == [(b"udp-only", "10.0.0.2")]


async def test_udp_listen_bind_stall_surfaces_as_oserror_not_a_hang(monkeypatch):
    # Simulates the G9 field bug directly at the UDP-fallback layer: a
    # bind() that never returns must not hang collect() -- it must resolve
    # (bounded by _dhcp_raw.OPEN_TIMEOUT) to a plain OSError.
    import time as _time

    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)

    def _hanging_bind():
        _time.sleep(0.4)

    monkeypatch.setattr(dhcp_fp, "_open_server_port_socket", _hanging_bind)

    agen = dhcp_fp._udp_listen()
    with pytest.raises(OSError):
        await agen.__anext__()


async def test_collector_never_hangs_when_default_transport_bind_stalls(monkeypatch):
    # End-to-end through DHCPFingerprintCollector.collect() (no `listen=`
    # injection) -- both raw and UDP paths fail/stall, collect() must still
    # return promptly and record available=False instead of hanging the
    # caller (the actual production bug on the G9 Core).
    import time as _time

    monkeypatch.setattr(dhcp_fp, "raw_af_packet_supported", lambda: False)
    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)

    def _hanging_bind():
        _time.sleep(0.4)

    monkeypatch.setattr(dhcp_fp, "_open_server_port_socket", _hanging_bind)

    collector = DHCPFingerprintCollector()
    out = await collector.collect(duration=1.0)
    assert out == {}
    assert collector.available is False
    assert "bind" in collector.unavailable_reason.lower() or "OSError" in collector.unavailable_reason
