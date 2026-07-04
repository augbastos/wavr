import struct

from wavr.hostname_resolver import (
    build_ptr_query,
    parse_ptr_response,
    resolve_hostnames,
    _reverse_name,
)
from wavr.netinventory import scan_inventory
from wavr.netinventory_service import NetworkInventoryService

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
  192.168.0.42          DE-AD-BE-EF-00-01     dynamic
"""


def _encode_name(name):
    out = bytearray()
    for label in name.split("."):
        b = label.encode()
        out.append(len(b))
        out += b
    out.append(0)
    return bytes(out)


def _ptr_response(ip, hostname, txid):
    qname = _encode_name(_reverse_name(ip))
    header = struct.pack(">HHHHHH", txid, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 12, 1)
    rdata = _encode_name(hostname)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 300, len(rdata)) + rdata
    return header + question + answer


# ---- reverse name --------------------------------------------------------

def test_reverse_name_valid():
    assert _reverse_name("192.168.1.42") == "42.1.168.192.in-addr.arpa"


def test_reverse_name_rejects_non_ipv4():
    assert _reverse_name("host.local") is None
    assert _reverse_name("1.2.3") is None
    assert _reverse_name("300.1.1.1") is None


# ---- query build ---------------------------------------------------------

def test_build_ptr_query_has_txid_and_ptr_qtype():
    q = build_ptr_query("192.168.1.42", txid=0x1234)
    assert q is not None
    assert q[:2] == b"\x12\x34"
    assert q[4:6] == b"\x00\x01"          # QDCOUNT == 1
    assert q[-4:] == struct.pack(">HH", 12, 1)   # QTYPE=PTR, QCLASS=IN


def test_build_ptr_query_rejects_bad_ip():
    assert build_ptr_query("not-an-ip") is None


# ---- response parse ------------------------------------------------------

def test_parse_ptr_response_roundtrip():
    resp = _ptr_response("192.168.0.23", "esp32-sensor.lan", 0x2222)
    assert parse_ptr_response(resp, 0x2222) == "esp32-sensor.lan"


def test_parse_ptr_response_wrong_txid_ignored():
    resp = _ptr_response("192.168.0.23", "esp32-sensor.lan", 0x2222)
    assert parse_ptr_response(resp, 0x9999) is None


def test_parse_ptr_response_malformed_never_raises():
    assert parse_ptr_response(b"", 0) is None
    assert parse_ptr_response(b"\x00" * 8, 0) is None
    assert parse_ptr_response(struct.pack(">HHHHHH", 1, 0x8180, 0, 1, 0, 0), 1) is None


# ---- resolve_hostnames (injected transport, no network) ------------------

async def test_resolve_hostnames_injected_maps_mac_to_hostname():
    async def fake_query(ip, server, timeout):
        return {"192.168.0.23": "esp32-abc"}.get(ip)
    entries = [("192.168.0.1", "A4-83-E7-11-22-33"),
               ("192.168.0.23", "24:0A:C4:AA:BB:CC")]
    res = await resolve_hostnames(entries, server="192.168.0.1", query=fake_query)
    assert res == {"24:0a:c4:aa:bb:cc": "esp32-abc"}


async def test_resolve_hostnames_no_gateway_returns_empty(monkeypatch):
    monkeypatch.setattr("wavr.hostname_resolver.guess_gateway", lambda *a, **k: None)
    async def fake_query(ip, server, timeout):
        return "x"
    res = await resolve_hostnames([("10.0.0.5", "de:ad:be:ef:00:01")],
                                  query=fake_query)
    assert res == {}


async def test_resolve_hostnames_tolerates_query_errors():
    async def boom(ip, server, timeout):
        raise OSError("dns down")
    res = await resolve_hostnames([("192.168.0.23", "24:0a:c4:aa:bb:cc")],
                                  server="192.168.0.1", query=boom)
    assert res == {}


# ---- wiring: scan_inventory resolve hook feeds hostnames= ----------------

async def test_scan_inventory_resolve_populates_hostname_and_fires_classifier():
    async def fake_scan():
        return WINDOWS_ARP
    async def fake_resolve(entries):
        return {"24:0a:c4:aa:bb:cc": "esp32-livingroom"}
    inv = await scan_inventory(scan=fake_scan, resolve=fake_resolve)
    esp = next(d for d in inv if d.mac == "24:0a:c4:aa:bb:cc")
    assert esp.hostname == "esp32-livingroom"
    assert esp.device_type == "esp_dev"
    assert any(s["signal"] == "hostname" for s in esp.sources)


async def test_scan_inventory_explicit_hostnames_win_over_resolve():
    async def fake_scan():
        return WINDOWS_ARP
    async def fake_resolve(entries):
        return {"24:0a:c4:aa:bb:cc": "resolved-name"}
    inv = await scan_inventory(scan=fake_scan,
                               hostnames={"24:0a:c4:aa:bb:cc": "pinned-name"},
                               resolve=fake_resolve)
    esp = next(d for d in inv if d.mac == "24:0a:c4:aa:bb:cc")
    assert esp.hostname == "pinned-name"


async def test_scan_inventory_resolve_failure_still_returns_inventory():
    async def fake_scan():
        return WINDOWS_ARP
    async def boom(entries):
        raise RuntimeError("resolver exploded")
    inv = await scan_inventory(scan=fake_scan, resolve=boom)
    assert {d.mac for d in inv} == {
        "a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01",
    }
    assert all(d.hostname is None for d in inv)


# ---- wiring: service opt-in (default OFF) --------------------------------

async def test_service_hostname_resolution_off_by_default():
    async def fake_scan():
        return WINDOWS_ARP
    calls = []
    async def fake_resolve(entries):
        calls.append(entries)
        return {"24:0a:c4:aa:bb:cc": "esp32-x"}
    svc = NetworkInventoryService(scan=fake_scan, interval=0,
                                  hostname_resolver=fake_resolve)
    await svc.scan_once()
    assert calls == []      # disabled -> resolver never called, zero PTR queries
    esp = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert esp.hostname is None


async def test_service_hostname_resolution_enabled_wires_into_inventory():
    async def fake_scan():
        return WINDOWS_ARP
    async def fake_resolve(entries):
        return {"24:0a:c4:aa:bb:cc": "esp32-kitchen"}
    svc = NetworkInventoryService(scan=fake_scan, interval=0,
                                  hostname_resolve_enabled=True,
                                  hostname_resolver=fake_resolve)
    await svc.scan_once()
    esp = next(d for d in svc.latest_inventory() if d.mac == "24:0a:c4:aa:bb:cc")
    assert esp.hostname == "esp32-kitchen"
    assert esp.device_type == "esp_dev"


# ---- config flag (opt-in, default OFF) -----------------------------------

def test_config_net_hostnames_defaults_off(monkeypatch):
    monkeypatch.delenv("WAVR_NET_HOSTNAMES", raising=False)
    from wavr.config import load_config
    assert load_config().net_hostnames is False


def test_config_net_hostnames_reads_env(monkeypatch):
    monkeypatch.setenv("WAVR_NET_HOSTNAMES", "1")
    from wavr.config import load_config
    assert load_config().net_hostnames is True
