"""wavr.data.ports table sanity + the opt-in connect-only quick-scan pass
(wavr.netutils.annotate_ports) with an injected probe -- zero real sockets."""
from wavr.data.ports import DEVICE_TYPE_HINTS, QUICK_SCAN_PORTS, port_type_hint
from wavr.netinventory import Device
from wavr.netinventory_service import NetworkInventoryService
from wavr.netutils import RISKY_PORTS, annotate_ports


def _dev(mac="24:0a:c4:aa:bb:cc", ip="192.168.0.23"):
    return Device(mac=mac, ip=ip, vendor="Espressif",
                  device_type="esp_dev", known=False)


# ---- table sanity --------------------------------------------------------------

def test_quick_scan_is_a_small_high_signal_tcp_set():
    assert len(QUICK_SCAN_PORTS) == 15
    assert len(set(QUICK_SCAN_PORTS)) == 15
    assert all(isinstance(p, int) and 0 < p < 65536 for p in QUICK_SCAN_PORTS)
    # every quick-scan port has an authored hint entry
    assert set(QUICK_SCAN_PORTS) <= set(DEVICE_TYPE_HINTS)


def test_signature_ports_are_present_with_expected_types():
    assert DEVICE_TYPE_HINTS[554][0] == "camera"
    assert DEVICE_TYPE_HINTS[9100][0] == "printer"
    assert DEVICE_TYPE_HINTS[62078][0] == "phone"
    assert DEVICE_TYPE_HINTS[8009][0] == "streaming_stick"
    assert DEVICE_TYPE_HINTS[3389][0] == "desktop"
    # informative-only ports carry no type claim
    assert DEVICE_TYPE_HINTS[161][0] is None
    assert DEVICE_TYPE_HINTS[5353][0] is None
    assert DEVICE_TYPE_HINTS[1900][0] is None


# ---- port_type_hint -------------------------------------------------------------

def test_hint_none_for_empty_or_non_diagnostic():
    assert port_type_hint(None) is None
    assert port_type_hint([]) is None
    assert port_type_hint([80, 443, 8080]) is None   # web UI could be anything


def test_hint_picks_the_most_diagnostic_port():
    # iPhone sync outweighs generic SMB
    dtype, note = port_type_hint([445, 62078])
    assert dtype == "phone"
    assert "62078" in note
    dtype, note = port_type_hint([9100])
    assert dtype == "printer"


# ---- annotate_ports (the opt-in combined pass) -----------------------------------

async def test_annotate_ports_fills_open_ports_and_risks_in_one_pass():
    async def fake_probe(ip, port, timeout):
        return port in (23, 554)          # Telnet (risky) + RTSP (hint) open
    [d] = await annotate_ports([_dev()], probe=fake_probe)
    assert d.open_ports == (23, 554)
    assert any("Telnet open" in r for r in d.risks)
    assert 554 in RISKY_PORTS             # RTSP is in both sets -> also a risk note


async def test_annotate_ports_scans_union_of_quick_and_risky_sets():
    seen = set()

    async def fake_probe(ip, port, timeout):
        seen.add(port)
        return False
    await annotate_ports([_dev()], probe=fake_probe)
    assert seen == set(QUICK_SCAN_PORTS) | set(RISKY_PORTS)


async def test_annotate_ports_skips_ipless_and_never_mutates():
    async def fake_probe(ip, port, timeout):
        raise AssertionError("should not probe an IP-less device")
    src = _dev(ip=None)
    [d] = await annotate_ports([src], probe=fake_probe)
    assert d.open_ports == () and d.risks == ()
    assert src.open_ports == ()           # input untouched (new record returned)


# ---- service wiring: the port pass feeds recognition ------------------------------

async def test_service_port_pass_refines_device_type():
    # an ESP32 with RTSP open (e.g. an ESP32-CAM): the port hint must beat the
    # OUI-only esp_dev default once the opt-in pass runs.
    async def fake_scan():
        return "192.168.0.23 24-0a-c4-aa-bb-cc dynamic\n"

    async def fake_probe(ip, port, timeout):
        return port == 554

    svc = NetworkInventoryService(scan=fake_scan, interval=0,
                                  port_scan=True, port_probe=fake_probe)
    [d] = await svc.scan_once()
    assert d.open_ports == (554,)
    assert d.device_type == "camera"
    assert any(s["signal"] == "port_hint" for s in d.sources)
    assert any("RTSP" in r for r in d.risks)     # 554 is also a risky port


async def test_service_port_pass_stays_off_by_default(monkeypatch):
    monkeypatch.delenv("WAVR_NET_PORTSCAN", raising=False)

    async def fake_scan():
        return "192.168.0.23 24-0a-c4-aa-bb-cc dynamic\n"

    async def boom_probe(ip, port, timeout):
        raise AssertionError("no probing without the opt-in gate")

    svc = NetworkInventoryService(scan=fake_scan, interval=0, port_probe=boom_probe)
    [d] = await svc.scan_once()
    assert d.open_ports == () and d.risks == ()
    assert d.device_type == "esp_dev"            # OUI default, no port influence


# ---- L3 audit fix: optional known-MAC scoping for the opt-in port pass -------

async def test_port_pass_scoped_to_known_macs_when_configured():
    # Two hosts: one allowlisted (known), one not. Scoped mode must connect-scan
    # only the known one -- the unknown/rogue host is left untouched (still
    # inventoried and still alert-eligible, just never connect-scanned).
    async def fake_scan():
        return ("192.168.0.1 a4-83-e7-11-22-33 dynamic\n"      # known (Apple)
                "192.168.0.23 24-0a-c4-aa-bb-cc dynamic\n")     # unknown (Espressif)

    probed_ips = set()

    async def fake_probe(ip, port, timeout):
        probed_ips.add(ip)
        return port == 554

    svc = NetworkInventoryService(
        known_macs={"a4:83:e7:11:22:33"}, scan=fake_scan, interval=0,
        port_scan=True, port_scan_known_only=True, port_probe=fake_probe)
    devices = await svc.scan_once()
    by_mac = {d.mac: d for d in devices}

    assert probed_ips == {"192.168.0.1"}              # only the known host touched
    assert by_mac["a4:83:e7:11:22:33"].open_ports == (554,)
    assert by_mac["24:0a:c4:aa:bb:cc"].open_ports == ()   # unknown host untouched
    assert by_mac["24:0a:c4:aa:bb:cc"].device_type == "esp_dev"  # OUI default only


async def test_port_pass_scans_every_host_when_scope_off(monkeypatch):
    # Default (scope flag off): unchanged behaviour -- every discovered host is
    # connect-scanned, known or not.
    monkeypatch.delenv("WAVR_NET_PORTSCAN_SCOPE", raising=False)

    async def fake_scan():
        return ("192.168.0.1 a4-83-e7-11-22-33 dynamic\n"
                "192.168.0.23 24-0a-c4-aa-bb-cc dynamic\n")

    probed_ips = set()

    async def fake_probe(ip, port, timeout):
        probed_ips.add(ip)
        return False

    svc = NetworkInventoryService(
        known_macs={"a4:83:e7:11:22:33"}, scan=fake_scan, interval=0,
        port_scan=True, port_probe=fake_probe)
    await svc.scan_once()
    assert probed_ips == {"192.168.0.1", "192.168.0.23"}
