"""wavr.sources.snmp -- active, targeted SNMPv1 GET collector."""
from __future__ import annotations

from wavr.sources.snmp import (
    SNMPCollector,
    _build_get_request,
    _decode_oid,
    _encode_oid,
    _os_from_descr,
    _vendor_from_object_id,
    parse_snmp_response,
)

_OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
_OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
_OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
_OID_SYS_SERVICES = "1.3.6.1.2.1.1.7.0"


# --- tiny BER encoder (test-only; builds a fake GetResponse) ----------------

def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = bytearray()
    while n:
        out.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(out)]) + bytes(out)


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(content)) + content


def _int(value: int) -> bytes:
    return _tlv(0x02, bytes([value]) if value else b"\x00")


def _octet(value: str) -> bytes:
    return _tlv(0x04, value.encode())


def _oid(oid: str) -> bytes:
    return _encode_oid(oid)


def _varbind(oid: str, value_tlv: bytes) -> bytes:
    return _tlv(0x30, _oid(oid) + value_tlv)


def _get_response(community: str, varbinds: list[tuple[str, bytes]]) -> bytes:
    vb_bytes = b"".join(_varbind(oid, v) for oid, v in varbinds)
    varbind_list = _tlv(0x30, vb_bytes)
    pdu_body = _int(1) + _int(0) + _int(0) + varbind_list
    pdu = _tlv(0xA2, pdu_body)  # GetResponse-PDU
    message_body = _int(0) + _octet(community) + pdu
    return _tlv(0x30, message_body)


_ROUTER_RESPONSE = _get_response("public", [
    (_OID_SYS_DESCR, _octet("RouterOS 6.47.1")),
    (_OID_SYS_OBJECT_ID, _oid("1.3.6.1.4.1.9.1.1")),
    (_OID_SYS_NAME, _octet("core-router")),
    (_OID_SYS_SERVICES, _int(78)),
])

_PRINTER_RESPONSE = _get_response("public", [
    (_OID_SYS_DESCR, _octet("HP LaserJet Pro M404")),
    (_OID_SYS_OBJECT_ID, _octet("")),  # deliberately malformed OID content
    (_OID_SYS_NAME, _octet("office-printer")),
    (_OID_SYS_SERVICES, _int(64)),
])

_LINUX_NAS_RESPONSE = _get_response("public", [
    (_OID_SYS_DESCR, _octet("Linux synology 4.4.180+")),
    (_OID_SYS_OBJECT_ID, _oid("1.3.6.1.4.1.2021.1")),
    (_OID_SYS_NAME, _octet("nas-basement")),
    (_OID_SYS_SERVICES, _int(72)),
])


# ---- OID encode/decode round-trip -------------------------------------------

def test_oid_encode_decode_round_trips():
    encoded = _encode_oid(_OID_SYS_DESCR)
    tag = encoded[0]
    assert tag == 0x06
    length = encoded[1]
    assert _decode_oid(encoded[2:2 + length]) == _OID_SYS_DESCR


# ---- GetResponse parsing -----------------------------------------------------

def test_parses_router_response():
    varbinds = parse_snmp_response(_ROUTER_RESPONSE)
    assert varbinds[_OID_SYS_DESCR] == "RouterOS 6.47.1"
    assert varbinds[_OID_SYS_NAME] == "core-router"
    assert varbinds[_OID_SYS_SERVICES] == 78


def test_malformed_response_does_not_raise():
    assert parse_snmp_response(b"\xff\xfe garbage") == {}
    assert parse_snmp_response(b"") == {}


def test_get_request_is_well_formed_ber():
    request = _build_get_request("public", [_OID_SYS_DESCR, _OID_SYS_NAME])
    assert request[0] == 0x30  # outer SEQUENCE
    assert b"public" in request


# ---- inference helpers --------------------------------------------------------

def test_os_from_descr_recognizes_known_tokens():
    assert _os_from_descr("RouterOS 6.47.1", None) == "RouterOS"
    assert _os_from_descr("Linux synology 4.4", None) == "Linux"
    assert _os_from_descr(None, "1.3.6.1.4.1.2021.1") == "Linux"
    assert _os_from_descr("Totally Unknown Firmware", None) is None


def test_vendor_from_object_id_uses_verified_enterprise_table():
    assert _vendor_from_object_id("1.3.6.1.4.1.9.1.1") == "Cisco"
    assert _vendor_from_object_id("1.3.6.1.4.1.2021.1") is None  # net-snmp registrant, not a vendor
    assert _vendor_from_object_id(None) is None
    assert _vendor_from_object_id("not-an-oid") is None


# ---- SNMPCollector end-to-end (fake prober, zero real sockets) --------------

async def test_collector_router_resolves_device_type_and_os():
    async def prober(ip, request):
        assert ip == "192.168.1.1"
        return _ROUTER_RESPONSE

    out = (await SNMPCollector(targets=["192.168.1.1"], prober=prober).collect())["192.168.1.1"]
    assert out["device_type"] == "router"
    assert out["os"] == "RouterOS"
    assert out["make"] == "Cisco"
    assert out["model"] is None  # never invented


async def test_collector_printer_resolves_via_sysdescr_hostname_regex():
    async def prober(ip, request):
        return _PRINTER_RESPONSE

    out = (await SNMPCollector(targets=["192.168.1.2"], prober=prober).collect())["192.168.1.2"]
    assert out["device_type"] == "printer"
    assert out["make"] is None  # malformed sysObjectID -- never guessed


async def test_collector_linux_nas_prefers_hostname_regex_over_ucdavis_os_hint():
    async def prober(ip, request):
        return _LINUX_NAS_RESPONSE

    out = (await SNMPCollector(targets=["192.168.1.3"], prober=prober).collect())["192.168.1.3"]
    assert out["device_type"] == "nas"
    assert out["os"] == "Linux"


async def test_ip_to_mac_mapping_keys_by_mac():
    async def prober(ip, request):
        return _ROUTER_RESPONSE

    out = await SNMPCollector(
        targets=["192.168.1.1"], prober=prober,
        ip_to_mac={"192.168.1.1": "AA-BB-CC-00-11-22"},
    ).collect()
    assert "aa:bb:cc:00:11:22" in out
    assert "192.168.1.1" not in out


async def test_unreachable_host_is_skipped_not_raised():
    async def prober(ip, request):
        raise TimeoutError("no response")

    out = await SNMPCollector(targets=["10.0.0.99"], prober=prober).collect()
    assert out == {}


async def test_empty_response_is_skipped():
    async def prober(ip, request):
        return b""

    out = await SNMPCollector(targets=["10.0.0.5"], prober=prober).collect()
    assert out == {}
