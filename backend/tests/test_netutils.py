import pytest

from wavr.netinventory import Device
from wavr.netutils import (
    PresenceHistory,
    annotate_risks,
    build_magic_packet,
    internet_health,
    ping_host,
    port_scan_enabled,
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
