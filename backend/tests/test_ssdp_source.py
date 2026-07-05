"""wavr.sources.ssdp -- passive SSDP/UPnP collector + optional LOC-XML fetch."""
from __future__ import annotations

from wavr.sources.ssdp import (
    SSDPCollector,
    _device_type_from_urn,
    _is_lan_location,
    _os_from_server,
    parse_ssdp_packet,
    parse_upnp_description,
)

_IGD_NOTIFY = (
    "NOTIFY * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "CACHE-CONTROL: max-age=1800\r\n"
    "LOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n"
    "NT: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "NTS: ssdp:alive\r\n"
    "SERVER: Linux/3.14.0 UPnP/1.0 MiniUPnPd/2.1\r\n"
    "USN: uuid:12345678-1234-1234-1234-123456789abc::"
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
).encode()

_MSEARCH_REQUEST = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 2\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
).encode()

_BYEBYE_NOTIFY = (
    "NOTIFY * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "NT: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "NTS: ssdp:byebye\r\n"
    "USN: uuid:12345678-1234-1234-1234-123456789abc::"
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
).encode()

_TV_SEARCH_RESPONSE = (
    "HTTP/1.1 200 OK\r\n"
    "CACHE-CONTROL: max-age=1800\r\n"
    "EXT:\r\n"
    "LOCATION: http://192.168.1.50:52235/description.xml\r\n"
    "SERVER: Linux/3.10 UPnP/1.0 Sony-Bravia/2.0\r\n"
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "USN: uuid:aa-bb-cc::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "\r\n"
).encode()

_TV_DESCRIPTION_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Living Room TV</friendlyName>
    <manufacturer>Sony</manufacturer>
    <modelName>KD-55X80J</modelName>
    <modelNumber>KD-55X80J</modelNumber>
    <serialNumber>1234567</serialNumber>
    <UDN>uuid:12345678-1234-1234-1234-123456789abc</UDN>
  </device>
</root>"""

_MALICIOUS_XML = """<?xml version="1.0"?>
<!DOCTYPE root [<!ENTITY xxe "pwn">]>
<root><friendlyName>&xxe;</friendlyName></root>"""


# ---- packet parsing ----------------------------------------------------------

def test_igd_notify_parses_router_fields():
    parsed = parse_ssdp_packet(_IGD_NOTIFY)
    assert parsed["location"] == "http://192.168.1.1:5000/rootDesc.xml"
    assert parsed["server"] == "Linux/3.14.0 UPnP/1.0 MiniUPnPd/2.1"
    assert parsed["target"] == "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
    assert parsed["usn"].startswith("uuid:")


def test_msearch_request_is_ignored():
    assert parse_ssdp_packet(_MSEARCH_REQUEST) is None


def test_byebye_notify_is_ignored():
    assert parse_ssdp_packet(_BYEBYE_NOTIFY) is None


def test_malformed_bytes_do_not_raise():
    assert parse_ssdp_packet(b"\xff\xfe\x00garbage") is None
    assert parse_ssdp_packet(b"") is None


# ---- device-type inference ---------------------------------------------------

def test_urn_table_maps_igd_to_router_and_printer_to_printer():
    assert _device_type_from_urn("urn:schemas-upnp-org:device:InternetGatewayDevice:1") == "router"
    assert _device_type_from_urn("urn:schemas-upnp-org:device:Printer:1") == "printer"
    assert _device_type_from_urn("urn:schemas-upnp-org:device:MediaServer:1") == "nas"


def test_mediarenderer_urn_alone_is_not_guessed():
    # MediaRenderer covers TVs, AVRs, and DLNA speakers alike -- no single
    # taxonomy value is safe to assume from the URN alone.
    assert _device_type_from_urn("urn:schemas-upnp-org:device:MediaRenderer:1") is None


def test_os_from_server_recognizes_known_os_tokens():
    assert _os_from_server("Linux/3.14.0 UPnP/1.0 MiniUPnPd/2.1") == "Linux"
    assert _os_from_server("Windows 10/10.0 UPnP/1.5 WMDRM/1.0") == "Windows"
    assert _os_from_server(None) is None
    assert _os_from_server("Totally/1.0 Unknown/2.0") is None


# ---- LOC-XML parsing ----------------------------------------------------------

def test_parses_tv_description_xml():
    desc = parse_upnp_description(_TV_DESCRIPTION_XML)
    assert desc["friendly_name"] == "Living Room TV"
    assert desc["manufacturer"] == "Sony"
    assert desc["model_name"] == "KD-55X80J"
    assert desc["serial_number"] == "1234567"


def test_doctype_bearing_xml_is_rejected_outright():
    assert parse_upnp_description(_MALICIOUS_XML) == {}


def test_malformed_xml_does_not_raise():
    assert parse_upnp_description("<not><valid") == {}


# ---- SSRF guard for the optional LOC-XML fetch -------------------------------

def test_is_lan_location_accepts_private_ip():
    assert _is_lan_location("http://192.168.1.1:5000/rootDesc.xml") is True
    assert _is_lan_location("http://10.0.0.5/desc.xml") is True


def test_is_lan_location_rejects_public_ip():
    assert _is_lan_location("http://8.8.8.8/desc.xml") is False


def test_is_lan_location_rejects_https_and_hostnames():
    assert _is_lan_location("https://192.168.1.1/desc.xml") is False
    assert _is_lan_location("http://router.local/desc.xml") is False
    assert _is_lan_location("not a url") is False


# ---- SSDPCollector end-to-end (fake transport, zero real sockets) ------------

async def test_collector_router_notify_without_location_fetch():
    async def listen():
        yield _IGD_NOTIFY, "192.168.1.1"

    out = (await SSDPCollector(listen=listen).collect(duration=0.2))["192.168.1.1"]
    assert out["device_type"] == "router"
    assert out["os"] == "Linux"
    assert out["make"] is None    # never fetched -- no XML, no invented manufacturer
    assert out["location"] == "http://192.168.1.1:5000/rootDesc.xml"


async def test_collector_tv_resolves_via_server_header_alone():
    # "Sony-Bravia" in the SERVER string matches hostname_type's bravia
    # pattern -- device_type resolves to "tv" even with fetch_location off.
    async def listen():
        yield _TV_SEARCH_RESPONSE, "192.168.1.50"

    out = (await SSDPCollector(listen=listen).collect(duration=0.2))["192.168.1.50"]
    assert out["device_type"] == "tv"


async def test_collector_fetches_location_and_enriches_make_model():
    fetched_urls = []

    async def fetcher(url):
        fetched_urls.append(url)
        return _TV_DESCRIPTION_XML

    async def listen():
        yield _TV_SEARCH_RESPONSE, "192.168.1.50"

    out = (await SSDPCollector(listen=listen, fetch_location=True, fetcher=fetcher)
           .collect(duration=0.2))["192.168.1.50"]
    assert fetched_urls == ["http://192.168.1.50:52235/description.xml"]
    assert out["make"] == "Sony"
    assert out["model"] == "KD-55X80J"
    assert out["serial"] == "1234567"
    assert out["device_type"] == "tv"   # now via friendlyName, same answer either way


async def test_collector_never_fetches_a_public_location():
    fetched = {"called": False}

    async def fetcher(url):
        fetched["called"] = True
        return _TV_DESCRIPTION_XML

    hostile = _TV_SEARCH_RESPONSE.replace(
        b"http://192.168.1.50:52235/description.xml", b"http://8.8.8.8/description.xml"
    )

    async def listen():
        yield hostile, "192.168.1.50"

    await SSDPCollector(listen=listen, fetch_location=True, fetcher=fetcher).collect(duration=0.2)
    assert fetched["called"] is False


async def test_collector_location_fetch_failure_is_tolerated():
    async def fetcher(url):
        raise OSError("connection refused")

    async def listen():
        yield _TV_SEARCH_RESPONSE, "192.168.1.50"

    out = (await SSDPCollector(listen=listen, fetch_location=True, fetcher=fetcher)
           .collect(duration=0.2))["192.168.1.50"]
    assert out["device_type"] == "tv"   # still resolves via SERVER header
    assert out["make"] is None          # no XML data merged, no crash


async def test_ip_to_mac_mapping_keys_by_mac():
    async def listen():
        yield _IGD_NOTIFY, "192.168.1.1"

    out = await SSDPCollector(listen=listen, ip_to_mac={"192.168.1.1": "AA-BB-CC-00-11-22"}).collect(duration=0.2)
    assert "aa:bb:cc:00:11:22" in out
    assert "192.168.1.1" not in out


async def test_collector_ignores_msearch_and_byebye():
    async def listen():
        yield _MSEARCH_REQUEST, "192.168.1.5"
        yield _BYEBYE_NOTIFY, "192.168.1.5"

    out = await SSDPCollector(listen=listen).collect(duration=0.2)
    assert out == {}
