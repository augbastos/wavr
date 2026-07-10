"""wavr.sources.dhcp -- passive/active DHCP collector.

Builds representative BOOTP/DHCP packets by hand (mirrors the parser's own
wire format) so every test runs with zero real sockets."""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from wavr.sources import _dhcp_raw, dhcp
from wavr.sources._dhcp_raw import reset_open_guards
from wavr.sources.dhcp import (
    MAGIC_COOKIE,
    DHCPCollector,
    build_discover_packet,
    parse_dhcp_packet,
)


@pytest.fixture(autouse=True)
def _reset_open_guards():
    # Two tests below deliberately make `_open_client_socket` genuinely time out
    # through `_dhcp_raw.open_with_timeout` -- that guard is a module global that
    # otherwise persists for the rest of the test session and would poison any
    # later test reusing the same "UDP/68 bind" `what` label. See
    # tests/test_dhcp_raw.py for the guard's own unit tests.
    reset_open_guards()
    yield
    reset_open_guards()


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


# ---- _default_listen raw-first / UDP-fallback orchestration (G9 Core fix) ---
# `listen=` injection (used above) bypasses `_default_listen` entirely, so
# these exercise the real transport-selection function directly, with the
# underlying socket layer monkeypatched (never a real socket in CI).

async def test_default_listen_uses_raw_when_supported(monkeypatch):
    async def fake_raw():
        yield b"raw-payload", "192.168.1.9"

    async def _udp_must_not_be_called(probe=False):
        raise AssertionError("UDP fallback must not run when raw succeeds")
        yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(dhcp, "raw_af_packet_supported", lambda: True)
    monkeypatch.setattr(dhcp, "raw_dhcp_listen", fake_raw)
    monkeypatch.setattr(dhcp, "_udp_listen", _udp_must_not_be_called)

    items = [item async for item in dhcp._default_listen(probe=False)]
    assert items == [(b"raw-payload", "192.168.1.9")]


async def test_default_listen_falls_back_to_udp_on_raw_permission_error(monkeypatch):
    async def fake_raw_denied():
        raise PermissionError("no CAP_NET_RAW")
        yield  # pragma: no cover

    async def fake_udp(probe=False):
        yield b"udp-payload", "10.0.0.1"

    monkeypatch.setattr(dhcp, "raw_af_packet_supported", lambda: True)
    monkeypatch.setattr(dhcp, "raw_dhcp_listen", fake_raw_denied)
    monkeypatch.setattr(dhcp, "_udp_listen", fake_udp)

    items = [item async for item in dhcp._default_listen(probe=False)]
    assert items == [(b"udp-payload", "10.0.0.1")]


async def test_default_listen_skips_raw_entirely_when_unsupported(monkeypatch):
    async def _raw_must_not_be_called():
        raise AssertionError("raw path must not be attempted when unsupported")
        yield  # pragma: no cover

    async def fake_udp(probe=False):
        yield b"udp-only", "10.0.0.2"

    monkeypatch.setattr(dhcp, "raw_af_packet_supported", lambda: False)
    monkeypatch.setattr(dhcp, "raw_dhcp_listen", _raw_must_not_be_called)
    monkeypatch.setattr(dhcp, "_udp_listen", fake_udp)

    items = [item async for item in dhcp._default_listen(probe=False)]
    assert items == [(b"udp-only", "10.0.0.2")]


async def test_default_listen_probe_mode_always_uses_udp_path(monkeypatch):
    # Active probing needs to broadcast on the same socket it reads replies
    # from -- the read-only raw sniff can't do that, so probe=True must
    # never even attempt the raw path, regardless of platform support.
    async def _raw_must_not_be_called():
        raise AssertionError("probe mode must not try the raw path")
        yield  # pragma: no cover

    calls = []

    async def fake_udp(probe=False):
        calls.append(probe)
        yield b"probed", "10.0.0.3"

    monkeypatch.setattr(dhcp, "raw_af_packet_supported", lambda: True)
    monkeypatch.setattr(dhcp, "raw_dhcp_listen", _raw_must_not_be_called)
    monkeypatch.setattr(dhcp, "_udp_listen", fake_udp)

    items = [item async for item in dhcp._default_listen(probe=True)]
    assert items == [(b"probed", "10.0.0.3")]
    assert calls == [True]


async def test_udp_listen_bind_stall_surfaces_as_oserror_not_a_hang(monkeypatch):
    # Simulates the G9 field bug directly at the UDP-fallback layer: a
    # bind() that never returns must not hang collect() -- it must resolve
    # (bounded by _dhcp_raw.OPEN_TIMEOUT) to a plain OSError.
    import time as _time

    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)

    def _hanging_bind():
        _time.sleep(0.4)

    monkeypatch.setattr(dhcp, "_open_client_socket", _hanging_bind)

    agen = dhcp._udp_listen(probe=False)
    with pytest.raises(OSError):
        await agen.__anext__()


async def test_udp_listen_does_not_retry_after_a_genuine_timeout(monkeypatch):
    # The gap the generalized `_timed_out_openers` guard closes: unlike the raw
    # AF_PACKET path (which had its own sticky `_raw_open_timed_out` flag from the
    # start), the UDP-bind fallback previously had NO guard at all -- a stalled
    # bind() would leak one more unreclaimable executor thread every single
    # collect() cycle forever. It must now fail fast on a second attempt too,
    # WITHOUT calling `_open_client_socket` again.
    import time as _time

    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)
    monkeypatch.setattr(dhcp, "_open_client_socket", lambda: _time.sleep(0.4))

    agen1 = dhcp._udp_listen(probe=False)
    with pytest.raises(OSError):
        await agen1.__anext__()

    called = []
    monkeypatch.setattr(dhcp, "_open_client_socket", lambda: called.append(1))
    agen2 = dhcp._udp_listen(probe=False)
    with pytest.raises(OSError):
        await agen2.__anext__()
    assert called == []


async def test_collector_never_hangs_when_default_transport_bind_stalls(monkeypatch):
    # End-to-end through DHCPCollector.collect() (no `listen=` injection) --
    # both raw and UDP paths fail/stall, collect() must still return
    # promptly instead of hanging the caller (the actual production bug).
    import time as _time

    monkeypatch.setattr(dhcp, "raw_af_packet_supported", lambda: False)
    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)

    def _hanging_bind():
        _time.sleep(0.4)

    monkeypatch.setattr(dhcp, "_open_client_socket", _hanging_bind)

    c = DHCPCollector()
    with pytest.raises(OSError):
        await asyncio.wait_for(c.collect(duration=1.0), timeout=2.0)
