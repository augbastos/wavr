import asyncio

import pytest

from wavr.netinventory import Device
from wavr.netutils import (
    PresenceHistory,
    _tcp_open_probe,
    annotate_risks,
    build_magic_packet,
    internet_health,
    ping_host,
    port_scan_enabled,
    port_scan_known_only_enabled,
    scan_risky_ports,
    send_magic_packet,
    speedtest_enabled,
)


def _dev(mac="24:0a:c4:aa:bb:cc", ip="192.168.0.23"):
    return Device(mac=mac, ip=ip, vendor="Espressif",
                  device_type="iot-embedded", known=False)


# ---- config gates default OFF ------------------------------------------------

def test_feature_flags_default_off(monkeypatch):
    monkeypatch.delenv("WAVR_NET_PORTSCAN", raising=False)
    monkeypatch.delenv("WAVR_NET_SPEEDTEST", raising=False)
    assert port_scan_enabled() is False
    assert speedtest_enabled() is False


def test_feature_flags_enable_via_env(monkeypatch):
    monkeypatch.setenv("WAVR_NET_PORTSCAN", "1")
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "true")
    assert port_scan_enabled() is True
    assert speedtest_enabled() is True


def test_port_scan_scope_defaults_off_and_enables_on_known(monkeypatch):
    monkeypatch.delenv("WAVR_NET_PORTSCAN_SCOPE", raising=False)
    assert port_scan_known_only_enabled() is False
    monkeypatch.setenv("WAVR_NET_PORTSCAN_SCOPE", "known")
    assert port_scan_known_only_enabled() is True
    monkeypatch.setenv("WAVR_NET_PORTSCAN_SCOPE", "all")  # anything else -> off
    assert port_scan_known_only_enabled() is False


# ---- 1) port / vuln awareness ------------------------------------------------

async def test_scan_risky_ports_flags_open_from_fake_probe():
    async def fake_probe(ip, port, timeout):
        return port == 23          # only Telnet answers
    open_ports = await scan_risky_ports("192.168.0.5", probe=fake_probe)
    assert open_ports == [23]


async def test_annotate_risks_adds_note_for_open_port():
    async def fake_probe(ip, port, timeout):
        return port in (23, 5900)  # Telnet + VNC open
    [d] = await annotate_risks([_dev()], probe=fake_probe)
    assert any("Telnet open" in r for r in d.risks)
    assert any("VNC open" in r for r in d.risks)
    assert len(d.risks) == 2


async def test_annotate_risks_empty_when_all_closed_and_never_mutates():
    async def fake_probe(ip, port, timeout):
        return False
    src = _dev()
    [d] = await annotate_risks([src], probe=fake_probe)
    assert d.risks == ()
    assert src.risks == ()          # input untouched (new record returned)


async def test_annotate_risks_skips_hosts_without_ip():
    async def fake_probe(ip, port, timeout):
        raise AssertionError("should not probe an IP-less device")
    [d] = await annotate_risks([_dev(ip=None)], probe=fake_probe)
    assert d.risks == ()


# ---- L6 audit fix: the REAL TCP probe's connect-only invariant, tested against
# an actual loopback socket (every other test in this file injects a fake probe) ---

async def test_tcp_open_probe_detects_open_port_and_never_sends_or_reads():
    received = []
    handled = asyncio.Event()

    async def handle(reader, writer):
        # A "server callback that raises on any read": if the probe ever wrote a
        # byte (a banner-grab request), it would show up here as non-empty data.
        data = await reader.read(64)
        received.append(data)
        handled.set()
        # Deliberately never close/write a response -- if the probe's own code
        # called reader.read() waiting for one, it would block here until our
        # timeout below. A true connect-only probe returns long before that.

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        loop = asyncio.get_event_loop()
        start = loop.time()
        opened = await _tcp_open_probe("127.0.0.1", port, timeout=2.0)
        elapsed = loop.time() - start
        await asyncio.wait_for(handled.wait(), timeout=1.0)
    finally:
        server.close()
        await server.wait_closed()

    assert opened is True
    assert elapsed < 0.5              # never blocked trying to read a response
    assert received == [b""]          # EOF only -- the probe never sent a byte


async def test_tcp_open_probe_detects_closed_port():
    # Bind an ephemeral listener, close it immediately, then probe that now-free
    # port: nothing is listening, so the connect attempt must fail (closed).
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    assert await _tcp_open_probe("127.0.0.1", port, timeout=0.5) is False


# ---- 2) speed / internet health (injected, no real network) ------------------

def test_internet_health_uses_injected_probes():
    health = internet_health(
        latency_fn=lambda: 12.5,
        download_fn=lambda: 94.3,
        upload_fn=lambda: 21.7,
    )
    assert health == {"latency_ms": 12.5, "download_mbps": 94.3, "upload_mbps": 21.7}


def test_internet_health_reports_none_on_failure():
    health = internet_health(latency_fn=lambda: None,
                             download_fn=lambda: None,
                             upload_fn=lambda: None)
    assert health == {"latency_ms": None, "download_mbps": None, "upload_mbps": None}


# ---- 3a) Wake-on-LAN ---------------------------------------------------------

def test_build_magic_packet_structure():
    pkt = build_magic_packet("a4:83:e7:11:22:33")
    assert len(pkt) == 102                       # 6 + 16*6
    assert pkt[:6] == b"\xff" * 6
    mac_bytes = bytes.fromhex("a483e7112233")
    assert pkt[6:] == mac_bytes * 16


def test_build_magic_packet_accepts_dash_form():
    assert build_magic_packet("A4-83-E7-11-22-33") == build_magic_packet("a4:83:e7:11:22:33")


def test_build_magic_packet_rejects_bad_mac():
    with pytest.raises(ValueError):
        build_magic_packet("not-a-mac")
    with pytest.raises(ValueError):
        build_magic_packet("a4:83:e7:11:22")     # too short


def test_send_magic_packet_uses_injected_sender():
    sent = {}
    def fake_send(packet, broadcast, port):
        sent["packet"], sent["broadcast"], sent["port"] = packet, broadcast, port
    pkt = send_magic_packet("a4:83:e7:11:22:33", send=fake_send)
    assert sent["packet"] == pkt
    assert sent["broadcast"] == "255.255.255.255" and sent["port"] == 9


# ---- 3b) per-device ping / latency ------------------------------------------

async def test_ping_host_returns_first_answering_port_latency():
    async def fake_probe(ip, port, timeout):
        return 8.0 if port == 80 else None       # only :80 answers
    assert await ping_host("192.168.0.9", probe=fake_probe) == 8.0


async def test_ping_host_none_when_unreachable():
    async def fake_probe(ip, port, timeout):
        return None
    assert await ping_host("192.168.0.9", probe=fake_probe) is None


# ---- 3c) presence history (in-memory, MAC-level only) ------------------------

def test_presence_history_records_join_and_leave():
    h = PresenceHistory()
    e1 = h.update({"aa:bb:cc:dd:ee:ff"}, ts="t0")
    assert [(e.mac, e.event) for e in e1] == [("aa:bb:cc:dd:ee:ff", "joined")]
    e2 = h.update({"aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"}, ts="t1")
    assert [(e.mac, e.event) for e in e2] == [("11:22:33:44:55:66", "joined")]
    e3 = h.update({"11:22:33:44:55:66"}, ts="t2")
    assert [(e.mac, e.event) for e in e3] == [("aa:bb:cc:dd:ee:ff", "left")]
    assert h.present() == {"11:22:33:44:55:66"}
    assert len(h.history()) == 3                 # join, join, leave


def test_presence_history_no_events_when_unchanged():
    h = PresenceHistory()
    h.update({"aa:bb:cc:dd:ee:ff"}, ts="t0")
    assert h.update({"AA-BB-CC-DD-EE-FF"}, ts="t1") == []   # same MAC, normalized
    assert h.history() == [] or len(h.history()) == 1


def test_presence_history_is_bounded():
    h = PresenceHistory(max_events=5)
    for i in range(20):
        # alternate a unique MAC in/out to generate many join/leave events
        mac = f"02:00:00:00:00:{i:02x}"
        h.update({mac}, ts=f"t{i}")
    assert len(h.history()) <= 5
