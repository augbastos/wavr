"""wavr.sources._dhcp_raw -- shared raw AF_PACKET DHCP frame sniff.

Builds representative Ethernet+IPv4+UDP frames by hand (mirrors the parser's
own wire format) so every test runs with zero real sockets -- same ethos as
test_dhcp_source.py/test_dhcp_fp_source.py."""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from wavr.sources import _dhcp_raw
from wavr.sources._dhcp_raw import (
    _parse_udp_frame,
    open_with_timeout,
    raw_af_packet_supported,
    raw_dhcp_listen,
    reset_open_guards,
)


@pytest.fixture(autouse=True)
def _reset_open_guards():
    # `_timed_out_openers`/`_raw_open_timed_out` are module globals that persist for
    # the whole test session by design (that's the point in production) -- several
    # tests below deliberately trigger a genuine timeout, which would otherwise
    # permanently poison a later, unrelated test that reuses the same `what` label.
    reset_open_guards()
    yield
    reset_open_guards()


def _eth_ipv4_udp_frame(payload: bytes, src_port: int = 67, dst_port: int = 68,
                         src_ip: str = "192.168.1.1", ihl_words: int = 5,
                         eth_type: int = 0x0800, ip_version: int = 4) -> bytes:
    eth = b"\xff" * 6 + b"\xaa" * 6 + struct.pack("!H", eth_type)
    udp_len = 8 + len(payload)
    udp = struct.pack("!HHHH", src_port, dst_port, udp_len, 0) + payload
    ihl = ihl_words * 4
    ip_total_len = ihl + len(udp)
    ver_ihl = (ip_version << 4) | ihl_words
    ip = struct.pack("!BBHHHBBH4s4s", ver_ihl, 0, ip_total_len, 0, 0, 64, 17, 0,
                      socket.inet_aton(src_ip), socket.inet_aton("192.168.1.2"))
    ip += b"\x00" * (ihl - 20)  # IHL options padding when ihl_words > 5
    return eth + ip + udp


# ---- _parse_udp_frame -------------------------------------------------------

def test_parse_udp_frame_extracts_payload_ports_and_src_ip():
    frame = _eth_ipv4_udp_frame(b"hello-dhcp", src_port=68, dst_port=67, src_ip="10.0.0.5")
    parsed = _parse_udp_frame(frame)
    assert parsed == (b"hello-dhcp", 68, 67, "10.0.0.5")


def test_parse_udp_frame_rejects_non_ipv4_ethertype():
    frame = _eth_ipv4_udp_frame(b"x", eth_type=0x86DD)  # IPv6
    assert _parse_udp_frame(frame) is None


def test_parse_udp_frame_rejects_non_udp_protocol():
    frame = bytearray(_eth_ipv4_udp_frame(b"x"))
    frame[14 + 9] = 6  # TCP instead of UDP
    assert _parse_udp_frame(bytes(frame)) is None


def test_parse_udp_frame_handles_ip_options_ihl_greater_than_5():
    frame = _eth_ipv4_udp_frame(b"opts-present", ihl_words=6)
    parsed = _parse_udp_frame(frame)
    assert parsed[0] == b"opts-present"


def test_parse_udp_frame_rejects_truncated_frame():
    frame = _eth_ipv4_udp_frame(b"hello")[:20]
    assert _parse_udp_frame(frame) is None


def test_parse_udp_frame_never_raises_on_garbage():
    assert _parse_udp_frame(b"\xff" * 5) is None
    assert _parse_udp_frame(b"") is None


def test_parse_udp_frame_rejects_non_ipv4_version_nibble():
    frame = bytearray(_eth_ipv4_udp_frame(b"x"))
    frame[14] = (6 << 4) | 5  # version=6 in the IPv4 slot
    assert _parse_udp_frame(bytes(frame)) is None


# ---- raw_af_packet_supported ------------------------------------------------

def test_raw_af_packet_supported_matches_socket_module():
    assert raw_af_packet_supported() == hasattr(socket, "AF_PACKET")


# ---- open_with_timeout -------------------------------------------------------

async def test_open_with_timeout_returns_openers_result():
    sentinel = object()
    result = await open_with_timeout(lambda: sentinel, "test open")
    assert result is sentinel


async def test_open_with_timeout_raises_oserror_when_opener_never_returns(monkeypatch):
    # Simulates the G9 field bug: a bind() call that STALLS rather than
    # raising -- open_with_timeout must give up (not hang) and surface it as
    # a plain OSError so every existing except(PermissionError, OSError)
    # handler already covers it.
    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)
    import time as _time

    def _hang():
        _time.sleep(5)  # far longer than OPEN_TIMEOUT -- must not be awaited fully

    with pytest.raises(OSError):
        await open_with_timeout(_hang, "hanging bind")


async def test_open_with_timeout_propagates_opener_exception_immediately():
    def _boom():
        raise PermissionError("no CAP_NET_RAW")

    with pytest.raises(PermissionError):
        await open_with_timeout(_boom, "raw socket")


async def test_open_with_timeout_never_blocks_the_event_loop(monkeypatch):
    # The actual regression this whole feature fixes: while a slow opener is
    # "hanging" in the executor thread, the event loop itself must keep
    # ticking (other coroutines still make progress) -- proven by a
    # concurrent sleep completing well before the hanging opener's deadline.
    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.2)
    import time as _time

    def _hang():
        _time.sleep(2)

    other_done = []

    async def _other():
        await asyncio.sleep(0.01)
        other_done.append(True)

    results = await asyncio.gather(
        open_with_timeout(_hang, "hang"), _other(), return_exceptions=True)
    assert other_done == [True]
    assert isinstance(results[0], OSError)


# ---- raw_dhcp_listen ----------------------------------------------------------

class _FakeRawSocket:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def recv(self, _size):
        if not self._frames:
            raise socket.timeout()
        return self._frames.pop(0)

    def close(self):
        self.closed = True


async def test_raw_dhcp_listen_yields_only_dhcp_port_frames(monkeypatch):
    dhcp_frame = _eth_ipv4_udp_frame(b"dhcp-payload", src_port=67, dst_port=68)
    other_frame = _eth_ipv4_udp_frame(b"not-dhcp", src_port=443, dst_port=51000)
    fake_sock = _FakeRawSocket([other_frame, dhcp_frame])
    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", lambda: fake_sock)

    agen = raw_dhcp_listen()
    payload, src_ip = await agen.__anext__()
    assert payload == b"dhcp-payload"
    assert src_ip == "192.168.1.1"
    await agen.aclose()
    assert fake_sock.closed


async def test_raw_dhcp_listen_closes_socket_on_early_exit(monkeypatch):
    # A quiet segment (no frames at all -- recv() always times out): the
    # generator must still open+close the socket cleanly when the caller
    # gives up early (mirrors the collect(duration=...) budget expiring).
    fake_sock = _FakeRawSocket([])
    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", lambda: fake_sock)
    agen = raw_dhcp_listen()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=0.1)
    await agen.aclose()
    assert fake_sock.closed


async def test_raw_dhcp_listen_propagates_open_failure(monkeypatch):
    def _boom():
        raise PermissionError("no CAP_NET_RAW")

    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", _boom)
    agen = raw_dhcp_listen()
    with pytest.raises(PermissionError):
        await agen.__anext__()


# ---- sticky "give up after a genuine open timeout" guard --------------------
# Bounds worst-case leaked-thread count to 1 for the process's lifetime if
# `_open_raw_socket()` ever truly never returns (as opposed to failing fast)
# -- see the `_raw_open_timed_out` module docstring for the full rationale.

async def test_raw_dhcp_listen_does_not_retry_after_a_genuine_timeout(monkeypatch):
    import time as _time

    monkeypatch.setattr(_dhcp_raw, "_raw_open_timed_out", False)
    monkeypatch.setattr(_dhcp_raw, "OPEN_TIMEOUT", 0.05)
    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", lambda: _time.sleep(0.4))

    agen1 = raw_dhcp_listen()
    with pytest.raises(OSError):
        await agen1.__anext__()
    assert _dhcp_raw._raw_open_timed_out is True

    # A second attempt in the SAME process must fail fast WITHOUT calling
    # the opener again (that call would leak yet another background thread).
    called = []
    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", lambda: called.append(1))
    agen2 = raw_dhcp_listen()
    with pytest.raises(OSError):
        await agen2.__anext__()
    assert called == []


async def test_raw_dhcp_listen_fast_fail_does_not_set_the_sticky_guard(monkeypatch):
    # A PermissionError/AttributeError returns immediately (no leaked
    # thread) -- safe, and expected, to retry every cycle forever.
    monkeypatch.setattr(_dhcp_raw, "_raw_open_timed_out", False)
    calls = []

    def _boom():
        calls.append(1)
        raise PermissionError("no CAP_NET_RAW")

    monkeypatch.setattr(_dhcp_raw, "_open_raw_socket", _boom)
    for _ in range(2):
        agen = raw_dhcp_listen()
        with pytest.raises(PermissionError):
            await agen.__anext__()
    assert calls == [1, 1]
    assert _dhcp_raw._raw_open_timed_out is False
