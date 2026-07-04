"""wavr.sources.mdns -- passive mDNS/Bonjour collector.

Builds representative real-world-shaped mDNS response packets by hand (a
minimal DNS-wire-format encoder, mirroring the parser's own format) so every
test runs with zero real sockets."""
from __future__ import annotations

import struct

from wavr.sources.mdns import MDNSCollector, parse_mdns_packet


# --- tiny DNS-wire-format encoder (test-only; the production module never
# encodes, only decodes) -----------------------------------------------------

def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode()
    return out + b"\x00"


def _dns_header(ancount: int = 0) -> bytes:
    # id=0, flags=0x8400 (standard response, authoritative), qd=0, an=ancount
    return struct.pack(">HHHHHH", 0, 0x8400, 0, ancount, 0, 0)


def _rr(name: str, rtype: int, rdata: bytes, ttl: int = 120) -> bytes:
    return _encode_name(name) + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata


def _txt_rdata(pairs: dict) -> bytes:
    out = b""
    for k, v in pairs.items():
        entry = f"{k}={v}".encode()
        out += bytes([len(entry)]) + entry
    return out


def _srv_rdata(target: str, port: int = 80) -> bytes:
    return struct.pack(">HHH", 0, 0, port) + _encode_name(target)


def _packet(*rrs: bytes) -> bytes:
    return _dns_header(ancount=len(rrs)) + b"".join(rrs)


# ---- HomeKit smart plug (ci=7 fallback, no model-string match) --------------

def _homekit_plug_packets():
    ptr = _rr("_hap._tcp.local", 12, _encode_name("Kitchen Outlet._hap._tcp.local"))
    srv = _rr("Kitchen Outlet._hap._tcp.local", 33,
              _srv_rdata("kitchen-outlet.local"))
    txt = _rr("Kitchen Outlet._hap._tcp.local", 16,
              _txt_rdata({"c#": "1", "ci": "7", "md": "KP125", "s#": "1"}))
    pkt1 = _packet(ptr, srv)   # split across two packets to test aggregation
    pkt2 = _packet(txt)
    return [(pkt1, "192.168.1.50"), (pkt2, "192.168.1.50")]


async def test_homekit_outlet_maps_ci_to_smart_plug():
    async def listen():
        for data, ip in _homekit_plug_packets():
            yield data, ip
    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.50"]
    assert out["device_type"] == "smart_plug"
    assert out["hostname"] == "kitchen-outlet"
    assert out["model"] == "KP125"
    assert out["services"] == ["_hap._tcp"]
    assert out["txt"]["ci"] == "7"


# ---- AirPlay 2 speaker (manufacturer/model TXT + hostname_type on model) ----

async def test_airplay_homepod_resolves_via_model_regex():
    ptr = _rr("_airplay._tcp.local", 12, _encode_name("Living Room._airplay._tcp.local"))
    srv = _rr("Living Room._airplay._tcp.local", 33, _srv_rdata("livingroom.local"))
    txt = _rr("Living Room._airplay._tcp.local", 16, _txt_rdata({
        "manufacturer": "Apple Inc.", "model": "HomePod mini", "deviceid": "aa:bb:cc:dd:ee:ff",
    }))

    async def listen():
        yield _packet(ptr, srv, txt), "192.168.1.60"

    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.60"]
    assert out["device_type"] == "speaker"
    assert out["make"] == "Apple Inc."
    assert out["model"] == "HomePod mini"
    assert out["hostname"] == "livingroom"


# ---- Chromecast: model resolves cleanly; bare service type does NOT --------

async def test_chromecast_model_resolves_to_streaming_stick():
    ptr = _rr("_googlecast._tcp.local", 12, _encode_name("Living Room TV._googlecast._tcp.local"))
    txt = _rr("Living Room TV._googlecast._tcp.local", 16,
              _txt_rdata({"md": "Chromecast", "fn": "Living Room TV"}))

    async def listen():
        yield _packet(ptr, txt), "192.168.1.70"

    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.70"]
    assert out["device_type"] == "streaming_stick"
    assert out["model"] == "Chromecast"


async def test_ambiguous_googlecast_alone_is_not_guessed():
    # Google Home Mini also advertises _googlecast._tcp -- the SAME service as
    # a Chromecast dongle -- so without a model string that resolves via
    # hostname_type, Wavr must NOT guess a device_type (overclaiming risk).
    ptr = _rr("_googlecast._tcp.local", 12, _encode_name("Kitchen Speaker._googlecast._tcp.local"))
    txt = _rr("Kitchen Speaker._googlecast._tcp.local", 16,
              _txt_rdata({"md": "Google Home Mini"}))

    async def listen():
        yield _packet(ptr, txt), "192.168.1.71"

    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.71"]
    assert "device_type" not in out
    assert out["model"] == "Google Home Mini"


# ---- IPP printer: usb_MFG/usb_MDL feed make/model; service alone -> printer -

async def test_ipp_printer_uses_usb_mfg_mdl_and_service_hint():
    ptr = _rr("_ipp._tcp.local", 12, _encode_name("HP OfficeJet Pro 9010._ipp._tcp.local"))
    txt = _rr("HP OfficeJet Pro 9010._ipp._tcp.local", 16, _txt_rdata({
        "usb_MFG": "HP", "usb_MDL": "OfficeJet Pro 9010", "rp": "ipp/print",
    }))

    async def listen():
        yield _packet(ptr, txt), "192.168.1.80"

    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.80"]
    assert out["device_type"] == "printer"
    assert out["make"] == "HP"
    assert out["model"] == "OfficeJet Pro 9010"


# ---- ip_to_mac keying -------------------------------------------------------

async def test_ip_to_mac_mapping_keys_by_mac():
    ptr = _rr("_ipp._tcp.local", 12, _encode_name("Printer._ipp._tcp.local"))

    async def listen():
        yield _packet(ptr), "192.168.1.80"

    out = await MDNSCollector(listen=listen, ip_to_mac={"192.168.1.80": "AA-BB-CC-DD-EE-FF"}).collect(duration=0.2)
    assert "aa:bb:cc:dd:ee:ff" in out
    assert "192.168.1.80" not in out


async def test_unmapped_ip_falls_back_to_ip_key():
    ptr = _rr("_ipp._tcp.local", 12, _encode_name("Printer._ipp._tcp.local"))

    async def listen():
        yield _packet(ptr), "192.168.1.81"

    out = await MDNSCollector(listen=listen, ip_to_mac={"192.168.1.80": "aa:bb:cc:dd:ee:ff"}).collect(duration=0.2)
    assert "192.168.1.81" in out


# ---- packet-count safety cap -------------------------------------------------

async def test_collect_stops_at_packet_cap(monkeypatch):
    import wavr.sources.mdns as mdns_mod
    monkeypatch.setattr(mdns_mod, "_MAX_PACKETS_PER_WINDOW", 3)
    ptr = _rr("_ipp._tcp.local", 12, _encode_name("Printer._ipp._tcp.local"))

    async def listen():
        i = 0
        while True:   # simulates a flooding LAN host -- must never hang the collector
            yield _packet(ptr), f"192.168.1.{90 + (i % 5)}"
            i += 1

    collector = MDNSCollector(listen=listen)
    packets = await collector._drain(duration=2.0)
    assert len(packets) == 3


# ---- parser resilience: malformed / truncated packets never raise ----------

def test_empty_packet_does_not_raise():
    parsed = parse_mdns_packet(b"")
    assert parsed == {"hostname": None, "services": set(), "txt": {}}


def test_truncated_header_does_not_raise():
    parsed = parse_mdns_packet(b"\x00\x00\x00\x00\x00")
    assert parsed["hostname"] is None


def test_bogus_high_counts_do_not_raise_or_hang():
    # header claims 5000 answer records but the packet has none
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 5000, 0, 0)
    parsed = parse_mdns_packet(header)
    assert parsed["services"] == set()


async def test_collector_skips_unparseable_packet_without_crashing():
    async def listen():
        yield b"\xff\xff\xff", "192.168.1.99"
        yield _packet(_rr("_ipp._tcp.local", 12, _encode_name("P._ipp._tcp.local"))), "192.168.1.99"

    out = await MDNSCollector(listen=listen).collect(duration=0.2)
    assert "192.168.1.99" in out


# ---- name-compression pointer handling --------------------------------------

async def test_compressed_owner_name_is_followed_correctly():
    header = _dns_header(ancount=2)
    owner1_offset = len(header)
    rr1 = _encode_name("_hap._tcp.local") + struct.pack(">HHIH", 16, 1, 120, 1) + b"\x00"
    rr2_owner = struct.pack(">H", 0xC000 | owner1_offset)
    rr2_rdata = _encode_name("Instance._hap._tcp.local")
    rr2 = rr2_owner + struct.pack(">HHIH", 12, 1, 120, len(rr2_rdata)) + rr2_rdata
    packet = header + rr1 + rr2

    async def listen():
        yield packet, "192.168.1.55"

    out = (await MDNSCollector(listen=listen).collect(duration=0.2))["192.168.1.55"]
    assert "_hap._tcp" in out["services"]
