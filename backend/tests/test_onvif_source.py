"""A4.2 ONVIF camera probe -- module guards + route.

Zero real sockets: the WS-Discovery + SOAP transports are injected. Proves the
load-bearing invariants: SSRF (non-LAN / DNS / cloud-metadata XAddrs never
contacted; non-rtsp / non-LAN stream URI never surfaced), XXE (DOCTYPE rejected),
credentials never leak into the response/log (and the password never even reaches
the SOAP body -- WS-Security digest), opt-in default-OFF gate, require_local CSRF,
and no-crash on malformed SOAP.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.sources import onvif
from wavr.sources.onvif import (
    ONVIFProbe,
    _default_soap,
    _is_lan_ip,
    _mask_rtsp,
    _rtsp_ok,
    _xaddr_ok,
    parse_probe_matches,
    parse_profiles,
    parse_stream_uri,
)


# --------------------------------------------------------------------------- #
# Canned wire data
# --------------------------------------------------------------------------- #

def _probe_match(xaddr: str, name="AcmeCam", hardware="AC-1080",
                 location="hallway") -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
        "<s:Body><d:ProbeMatches><d:ProbeMatch>"
        "<a:EndpointReference><a:Address>urn:uuid:abc</a:Address></a:EndpointReference>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        f"<d:Scopes>onvif://www.onvif.org/name/{name} "
        f"onvif://www.onvif.org/hardware/{hardware} "
        f"onvif://www.onvif.org/location/{location}</d:Scopes>"
        f"<d:XAddrs>{xaddr}</d:XAddrs>"
        "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
    ).encode()


_PROFILES_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body><trt:GetProfilesResponse"
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<trt:Profiles token="Profile_1"><tt:VideoEncoderConfiguration>'
    "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
    "</tt:VideoEncoderConfiguration></trt:Profiles>"
    "</trt:GetProfilesResponse></s:Body></s:Envelope>"
)


def _stream_uri_xml(uri: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        "<s:Body><trt:GetStreamUriResponse"
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<trt:MediaUri><tt:Uri>{uri}</tt:Uri></trt:MediaUri>"
        "</trt:GetStreamUriResponse></s:Body></s:Envelope>"
    )


def _fake_soap(uri: str, sink: list | None = None):
    """Return an injectable soap(url, body, timeout) that answers GetProfiles then
    GetStreamUri(uri). Records every (url, body) into `sink` if given."""
    async def soap(url, body, timeout):
        if sink is not None:
            sink.append((url, body))
        if "GetStreamUri" in body:
            return _stream_uri_xml(uri)
        return _PROFILES_XML
    return soap


def _fake_discover(*datagrams_with_ip):
    async def discover(targets, timeout):
        return list(datagrams_with_ip)
    return discover


def _tracking_discover(sink, *datagrams_with_ip):
    async def discover(targets, timeout):
        sink.append(True)
        return list(datagrams_with_ip)
    return discover


# --------------------------------------------------------------------------- #
# SSRF host guards
# --------------------------------------------------------------------------- #

def test_is_lan_ip_accepts_private_literals():
    assert _is_lan_ip("192.168.1.64")
    assert _is_lan_ip("10.0.0.5")
    assert _is_lan_ip("127.0.0.1")


def test_is_lan_ip_rejects_public_and_dns():
    assert _is_lan_ip("8.8.8.8") is False
    assert _is_lan_ip("camera.local") is False   # DNS hostname refused
    assert _is_lan_ip("example.com") is False
    assert _is_lan_ip("") is False
    assert _is_lan_ip(None) is False


def test_is_lan_ip_rejects_cloud_metadata_despite_link_local():
    # 169.254.169.254 is link-local -> would pass the private/link-local allow;
    # explicitly denied (SSRF T2).
    assert _is_lan_ip("169.254.1.1") is True       # ordinary link-local ok
    assert _is_lan_ip("169.254.169.254") is False  # AWS IMDS denied
    assert _is_lan_ip("fd00:ec2::254") is False    # IPv6 IMDS denied


def test_is_lan_ip_rejects_ipv4_mapped_metadata_bypass():
    # SSRF T2 bypass: ::ffff:169.254.169.254 routes to the IPv4 IMDS on a
    # dual-stack host but is not == the IPv4 metadata object. Must be denied,
    # via xaddr/rtsp entry points too, and a mapped public IP still non-LAN.
    assert _is_lan_ip("::ffff:169.254.169.254") is False
    assert _xaddr_ok("http://[::ffff:169.254.169.254]/latest/meta-data/") is False
    assert _rtsp_ok("rtsp://[::ffff:169.254.169.254]/s") is False
    assert _is_lan_ip("::ffff:8.8.8.8") is False   # mapped public -> non-LAN
    assert _is_lan_ip("::ffff:192.168.1.64") is True  # mapped private ok


def test_xaddr_ok_scheme_and_host():
    assert _xaddr_ok("http://192.168.1.64/onvif/device_service")
    assert _xaddr_ok("https://10.0.0.9/onvif/device_service")
    assert _xaddr_ok("http://8.8.8.8/onvif/device_service") is False
    assert _xaddr_ok("http://camera.local/onvif/device_service") is False
    assert _xaddr_ok("file:///etc/passwd") is False
    assert _xaddr_ok("http://169.254.169.254/latest/meta-data/") is False


def test_rtsp_ok_scheme_and_host():
    assert _rtsp_ok("rtsp://192.168.1.64:554/stream")
    assert _rtsp_ok("rtsps://10.0.0.9/stream")
    assert _rtsp_ok("rtsp://evil.example.com/stream") is False  # DNS host
    assert _rtsp_ok("rtsp://8.8.8.8/stream") is False           # public host
    assert _rtsp_ok("http://192.168.1.64/stream") is False      # wrong scheme
    assert _rtsp_ok("file:///etc/passwd") is False


# --------------------------------------------------------------------------- #
# XML parsing: happy path, XXE, malformed
# --------------------------------------------------------------------------- #

def test_parse_probe_matches_extracts_fields():
    matches = parse_probe_matches(_probe_match("http://192.168.1.64/onvif/device_service"))
    assert len(matches) == 1
    m = matches[0]
    assert m["xaddr"] == "http://192.168.1.64/onvif/device_service"
    assert m["name"] == "AcmeCam"
    assert m["hardware"] == "AC-1080"
    assert m["location"] == "hallway"


def test_parse_profiles_and_stream_uri():
    profs = parse_profiles(_PROFILES_XML)
    assert profs == [{"token": "Profile_1", "resolution": "1920x1080"}]
    assert parse_stream_uri(_stream_uri_xml("rtsp://192.168.1.64:554/s")) \
        == "rtsp://192.168.1.64:554/s"


def test_doctype_is_rejected_outright():
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE root [<!ENTITY x "pwn">]>'
        "<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\">"
        "<s:Body><d:ProbeMatches xmlns:d=\"http://schemas.xmlsoap.org/ws/2005/04/discovery\">"
        "<d:ProbeMatch><d:XAddrs>&x;</d:XAddrs></d:ProbeMatch>"
        "</d:ProbeMatches></s:Body></s:Envelope>"
    )
    assert parse_probe_matches(xxe.encode()) == []
    assert parse_profiles(xxe) == []
    assert parse_stream_uri(xxe) is None


def test_malformed_xml_never_raises():
    assert parse_probe_matches(b"\xff\xfe not xml") == []
    assert parse_probe_matches(b"") == []
    assert parse_profiles("<not><closed") == []
    assert parse_stream_uri("garbage") is None


def test_oversized_body_rejected():
    huge = "<a>" + ("x" * (onvif._MAX_XML_BYTES + 10)) + "</a>"
    assert parse_profiles(huge) == []


# --------------------------------------------------------------------------- #
# ONVIFProbe end-to-end (injected transports, zero sockets)
# --------------------------------------------------------------------------- #

async def test_probe_happy_path_returns_masked_rtsp():
    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=_fake_soap("rtsp://192.168.1.64:554/Streaming/Channels/101"))
    out = await probe.probe()
    assert out["errors"] == []
    assert len(out["cameras"]) == 1
    cam = out["cameras"][0]
    assert cam["ip"] == "192.168.1.64"
    assert cam["make"] == "AcmeCam"
    assert cam["model"] == "AC-1080"
    assert cam["rtsp_url"] == "rtsp://192.168.1.64:554/Streaming/Channels/101"
    assert cam["profiles"][0]["resolution"] == "1920x1080"


async def test_probe_masks_embedded_credentials():
    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=_fake_soap("rtsp://admin:s3cret@192.168.1.64:554/stream"))
    out = await probe.probe()
    url = out["cameras"][0]["rtsp_url"]
    assert url == "rtsp://admin:***@192.168.1.64:554/stream"
    assert "s3cret" not in url


async def test_probe_refuses_external_xaddr_without_contacting_it():
    called = []

    async def soap(url, body, timeout):
        called.append(url)  # must never happen for the public host
        return _PROFILES_XML

    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://8.8.8.8/onvif/device_service"),
                                 "8.8.8.8")),
        soap=soap)
    out = await probe.probe()
    assert called == []                 # SSRF: never contacted
    assert out["cameras"] == []
    assert out["errors"] and "not on local LAN" in out["errors"][0]["reason"]


async def test_probe_refuses_dns_and_metadata_xaddr():
    called = []

    async def soap(url, body, timeout):
        called.append(url)
        return _PROFILES_XML

    probe = ONVIFProbe(
        discover=_fake_discover(
            (_probe_match("http://camera.local/onvif/device_service"), "192.168.1.5"),
            (_probe_match("http://169.254.169.254/onvif/device_service"), "192.168.1.6")),
        soap=soap)
    out = await probe.probe()
    assert called == []                 # neither DNS-host nor metadata host contacted
    assert out["cameras"] == []
    assert len(out["errors"]) == 2


async def test_probe_drops_non_lan_stream_uri():
    # XAddrs is LAN (so the device IS contacted) but the RETURNED rtsp url points
    # off-LAN -- result validation must drop it, surfacing no camera.
    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=_fake_soap("rtsp://attacker.example.com/stream"))
    out = await probe.probe()
    assert out["cameras"] == []
    assert out["errors"] and "no usable RTSP profile" in out["errors"][0]["reason"]


async def test_probe_drops_non_rtsp_stream_uri():
    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=_fake_soap("http://192.168.1.64/latest/meta-data/"))
    out = await probe.probe()
    assert out["cameras"] == []


async def test_probe_password_never_in_soap_body_username_is():
    sink: list = []
    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=_fake_soap("rtsp://192.168.1.64:554/s", sink=sink))
    out = await probe.probe(username="admin", password="hunter2")
    bodies = " ".join(b for _u, b in sink)
    assert "hunter2" not in bodies          # WS-Security digest: password never sent raw
    assert "admin" in bodies                # username IS present (auth wired)
    # ...and creds never appear in the result at all
    dump = repr(out)
    assert "hunter2" not in dump and "admin" not in dump


async def test_probe_soap_failure_is_isolated_no_crash():
    async def soap(url, body, timeout):
        raise OSError("connection refused")

    probe = ONVIFProbe(
        discover=_fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                                 "192.168.1.64")),
        soap=soap)
    out = await probe.probe(password="secret")
    assert out["cameras"] == []
    assert out["errors"] and out["errors"][0]["reason"] == "SOAP probe failed"
    assert "secret" not in repr(out)        # no cred leak via error path


def test_default_soap_refuses_non_lan_host_before_opening_socket():
    # The pre-open guard raises for a public host -> no socket is ever opened.
    with pytest.raises(ValueError):
        asyncio.run(_default_soap("http://8.8.8.8/onvif/device_service", "<a/>", 0.1))


def test_mask_rtsp_helper():
    assert _mask_rtsp("rtsp://u:p@1.2.3.4/s") == "rtsp://u:***@1.2.3.4/s"
    assert _mask_rtsp("rtsp://1.2.3.4/s") == "rtsp://1.2.3.4/s"


# --------------------------------------------------------------------------- #
# Route: POST /api/onvif/probe
# --------------------------------------------------------------------------- #

def _client(**kw):
    return TestClient(create_app(sources=[], **kw), headers={"X-Wavr-Local": "1"})


def test_route_503_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_ONVIF_PROBE", raising=False)
    with _client() as c:
        r = c.post("/api/onvif/probe", json={})
        assert r.status_code == 503


def test_route_requires_local_header(monkeypatch):
    monkeypatch.setenv("WAVR_ONVIF_PROBE", "1")
    app = create_app(sources=[])
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/onvif/probe", json={}).status_code == 403


def test_route_returns_masked_camera(monkeypatch):
    monkeypatch.setenv("WAVR_ONVIF_PROBE", "1")
    discover = _fake_discover((_probe_match("http://192.168.1.64/onvif/device_service"),
                               "192.168.1.64"))
    soap = _fake_soap("rtsp://admin:pw@192.168.1.64:554/stream")
    with _client(onvif_discover=discover, onvif_soap=soap) as c:
        r = c.post("/api/onvif/probe",
                   json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["cameras"]) == 1
        assert body["cameras"][0]["rtsp_url"] == "rtsp://admin:***@192.168.1.64:554/stream"
        # No credential anywhere in the response payload.
        assert "pw" not in r.text
        assert r.json()["cameras"][0]["ip"] == "192.168.1.64"


def test_route_external_xaddr_never_contacted(monkeypatch):
    monkeypatch.setenv("WAVR_ONVIF_PROBE", "1")
    called = []

    async def soap(url, body, timeout):
        called.append(url)
        return _PROFILES_XML

    discover = _fake_discover((_probe_match("http://8.8.8.8/onvif/device_service"), "8.8.8.8"))
    with _client(onvif_discover=discover, onvif_soap=soap) as c:
        r = c.post("/api/onvif/probe", json={})
        assert r.status_code == 200
        assert r.json()["cameras"] == []
        assert called == []             # SSRF: the public host is never contacted


def test_route_opt_in_gate_holds_off_by_default(monkeypatch):
    # Belt-and-suspenders: even with a working transport injected, the flag OFF
    # short-circuits to 503 before the probe ever runs.
    monkeypatch.delenv("WAVR_ONVIF_PROBE", raising=False)
    sink = []
    discover = _tracking_discover(
        sink, (_probe_match("http://192.168.1.64/onvif/device_service"), "192.168.1.64"))
    with _client(onvif_discover=discover, onvif_soap=_fake_soap("rtsp://192.168.1.64/s")) as c:
        assert c.post("/api/onvif/probe", json={}).status_code == 503
    assert sink == []                    # transport never invoked when disabled
