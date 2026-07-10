"""A4.3 ONVIF PTZ actuator -- module logic + flag-gated require-local routes.

Zero real sockets: the PTZ SOAP transport is injected (`ptz_soap`), same seam as the
ONVIF probe tests. Proves the load-bearing invariants: server-side clamp (incl.
NaN/inf), SSRF (a non-LAN camera is NEVER contacted), credentials never appear in a
SOAP body (WS-Security digest) NOR in any response, opt-in default-OFF gate (503),
missing-camera 404, hostile id/token rejected, the master camera kill-switch
short-circuits a move with zero SOAP, and the server-side auto-stop runaway guard fires.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
import math

from wavr.localize import normalized_pan_tilt_to_radians
from wavr.ptz import (
    CameraPTZ,
    _clamp_unit,
    build_continuous_move,
    build_get_status,
    build_stop,
    parse_presets,
    parse_ptz_service,
    parse_ptz_status,
)

# --------------------------------------------------------------------------- #
# Canned wire data
# --------------------------------------------------------------------------- #

_PROFILES_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body><trt:GetProfilesResponse"
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<trt:Profiles token="Profile_1"><tt:Resolution>'
    "<tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
    "</tt:Resolution></trt:Profiles>"
    "</trt:GetProfilesResponse></s:Body></s:Envelope>"
)

_OK_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><tptz:Response xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"/>'
    "</s:Body></s:Envelope>"
)

_FAULT_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>"
    "<s:Reason><s:Text>nope</s:Text></s:Reason></s:Fault>"
    "</s:Body></s:Envelope>"
)

_PRESETS_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><tptz:GetPresetsResponse'
    ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<tptz:Preset token="1"><tt:Name>Door</tt:Name></tptz:Preset>'
    '<tptz:Preset token="2"><tt:Name>Window</tt:Name></tptz:Preset>'
    "</tptz:GetPresetsResponse></s:Body></s:Envelope>"
)


_STATUS_XML = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body><tptz:GetStatusResponse'
    ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    "<tptz:PTZStatus><tt:Position>"
    '<tt:PanTilt x="0.5000" y="-0.2500"/><tt:Zoom x="0.1000"/>'
    "</tt:Position><tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt></tt:MoveStatus>"
    "</tptz:PTZStatus></tptz:GetStatusResponse></s:Body></s:Envelope>"
)


def _services_xml(host: str, with_ptz: bool = True) -> str:
    svc = (
        "<tds:Service>"
        "<tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>"
        f"<tds:XAddr>http://{host}:2020/onvif/service</tds:XAddr>"
        "</tds:Service>"
    ) if with_ptz else (
        "<tds:Service>"
        "<tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>"
        f"<tds:XAddr>http://{host}:2020/onvif/device_service</tds:XAddr>"
        "</tds:Service>"
    )
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body><tds:GetServicesResponse'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        f"{svc}</tds:GetServicesResponse></s:Body></s:Envelope>"
    )


def _fake_ptz_soap(host="10.0.0.5", sink=None, with_ptz_service=True,
                   fault_on=None, presets_xml=None):
    """Injectable soap(url, body, action, timeout). Answers GetProfiles/GetServices
    for discovery, then ContinuousMove/Stop/GetPresets/GotoPreset. Records every
    (url, body, action) into `sink` if given. `fault_on` = a substring of the body
    that should return a SOAP Fault instead of success."""
    async def soap(url, body, action, timeout):
        if sink is not None:
            sink.append((url, body, action))
        if fault_on and fault_on in body:
            return _FAULT_XML
        if "GetProfiles" in body:
            return _PROFILES_XML
        if "GetServices" in body:
            return _services_xml(host, with_ptz=with_ptz_service)
        if "GetPresets" in body:
            return presets_xml if presets_xml is not None else _PRESETS_XML
        if "GetStatus" in body:
            return _STATUS_XML
        return _OK_XML   # ContinuousMove / Stop / GotoPreset
    return soap


_LAN_RTSP = "rtsp://admin:pw@10.0.0.5:554/stream1"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_clamp_unit_bounds_and_nonfinite():
    assert _clamp_unit(0.5) == 0.5
    assert _clamp_unit(5) == 1.0
    assert _clamp_unit(-9) == -1.0
    assert _clamp_unit(float("nan")) == 0.0
    assert _clamp_unit(float("inf")) == 0.0
    assert _clamp_unit(float("-inf")) == 0.0
    assert _clamp_unit("garbage") == 0.0
    assert _clamp_unit(None) == 0.0


def test_build_continuous_move_shape_and_escaping():
    body = build_continuous_move("Profile_1", 1.0, -1.0, 0.0, "admin", "pw")
    assert 'x="1.0000"' in body and 'y="-1.0000"' in body
    assert "<tt:Zoom" not in body                 # zoom==0 -> omitted
    assert "<tptz:ProfileToken>Profile_1</tptz:ProfileToken>" in body
    assert "pw" not in body                        # password never in the body
    assert "admin" in body                         # username IS present (WS-UsernameToken)
    # token XML-escaped
    body2 = build_continuous_move("a&<b", 0.0, 0.0, 0.5, "u", "p")
    assert "a&amp;&lt;b" in body2 and 'x="0.5000"' in body2  # zoom present when !=0


def test_build_stop_shape():
    body = build_stop("Profile_1", "admin", "pw")
    assert "<tptz:Stop" in body and "<tptz:PanTilt>true</tptz:PanTilt>" in body
    assert "pw" not in body


def test_parse_ptz_service_and_fallback():
    assert parse_ptz_service(_services_xml("10.0.0.5")) == "http://10.0.0.5:2020/onvif/service"
    assert parse_ptz_service(_services_xml("10.0.0.5", with_ptz=False)) is None
    assert parse_ptz_service("<not><xml") is None


def test_parse_presets():
    assert parse_presets(_PRESETS_XML) == [
        {"token": "1", "name": "Door"}, {"token": "2", "name": "Window"}]
    assert parse_presets("garbage") == []


def test_build_get_status_shape_and_no_password():
    # Distinctive plaintext-password sentinel WITH a hyphen: base64 (the nonce/digest alphabet)
    # never contains "-", so this can't collide with the random WS-Security nonce the way the old
    # 2-char "pw" did (~1.7% false-positive flake). We assert the plaintext never leaks into the body.
    body = build_get_status("Profile_1", "admin", "secret-pw-xyz")
    assert "<tptz:GetStatus" in body
    assert "<tptz:ProfileToken>Profile_1</tptz:ProfileToken>" in body
    assert "admin" in body and "secret-pw-xyz" not in body   # WS-UsernameToken digest, no plaintext pw
    # token XML-escaped
    assert "a&amp;b" in build_get_status("a&b", "u", "p")


def test_parse_ptz_status_reads_position():
    st = parse_ptz_status(_STATUS_XML)
    assert st == {"pan": 0.5, "tilt": -0.25, "zoom": 0.1}


def test_parse_ptz_status_no_zoom_omits_key():
    xml = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
        '<tptz:GetStatusResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        '<tt:Position><tt:PanTilt x="0.0" y="1.0"/></tt:Position>'
        "</tptz:GetStatusResponse></s:Body></s:Envelope>"
    )
    assert parse_ptz_status(xml) == {"pan": 0.0, "tilt": 1.0}


def test_parse_ptz_status_rejects_garbage_and_nonfinite():
    assert parse_ptz_status("<not xml") is None
    assert parse_ptz_status("<a/>") is None            # no PanTilt at all
    nan_xml = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
        '<tt:PanTilt xmlns:tt="http://www.onvif.org/ver10/schema" x="NaN" y="0.1"/>'
        "</s:Body></s:Envelope>"
    )
    assert parse_ptz_status(nan_xml) is None           # non-finite pan refused


def test_normalized_pan_tilt_to_radians_scaffold():
    # Centre (0,0) -> level bearing.
    assert normalized_pan_tilt_to_radians(0.0, 0.0) == (0.0, 0.0)
    # Full-right pan (+1) -> +pan_half_range; clamped input can't exceed the range.
    pan_rad, tilt_rad = normalized_pan_tilt_to_radians(9.0, -9.0)
    assert pan_rad == math.radians(170.0)              # clamp to +1 then * half-range
    assert tilt_rad == math.radians(-45.0)
    # Garbage/degree-valued input is clamped, never a wild angle.
    assert normalized_pan_tilt_to_radians(float("nan"), float("inf")) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# CameraPTZ (injected transport, zero sockets)
# --------------------------------------------------------------------------- #

async def test_discover_resolves_and_caches():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink))
    got = await ptz.discover("cam", _LAN_RTSP)
    assert got == ("http://10.0.0.5:2020/onvif/service", "Profile_1")
    assert len(sink) == 2                          # GetProfiles + GetServices
    await ptz.discover("cam", _LAN_RTSP)           # second call is cached
    assert len(sink) == 2                          # no further SOAP


async def test_discover_falls_back_when_no_ptz_service():
    ptz = CameraPTZ(soap=_fake_ptz_soap(with_ptz_service=False))
    got = await ptz.discover("cam", _LAN_RTSP)
    assert got == ("http://10.0.0.5:2020/onvif/service", "Profile_1")  # fallback path


async def test_discover_refuses_non_lan_without_contacting():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink))
    assert await ptz.discover("cam", "rtsp://admin:pw@8.8.8.8:554/s") is None
    assert sink == []                              # SSRF: never contacted


async def test_continuous_move_clamps_and_succeeds():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink), autostop_s=999)
    ok = await ptz.continuous_move("cam", _LAN_RTSP, 9.0, -9.0, 0.0)
    assert ok is True
    move = [b for _u, b, a in sink if "ContinuousMove" in b][0]
    assert 'x="1.0000"' in move and 'y="-1.0000"' in move   # server clamp to [-1,1]
    assert "pw" not in move
    ptz._cancel_autostop("cam")


async def test_continuous_move_fault_returns_false():
    ptz = CameraPTZ(soap=_fake_ptz_soap(fault_on="ContinuousMove"), autostop_s=999)
    assert await ptz.continuous_move("cam", _LAN_RTSP, 1.0, 0.0) is False


async def test_stop_and_get_presets_and_goto():
    ptz = CameraPTZ(soap=_fake_ptz_soap(), autostop_s=999)
    assert await ptz.stop("cam", _LAN_RTSP) is True
    assert await ptz.get_presets("cam", _LAN_RTSP) == [
        {"token": "1", "name": "Door"}, {"token": "2", "name": "Window"}]
    assert await ptz.goto_preset("cam", _LAN_RTSP, "1") is True


async def test_capabilities_true_false():
    ptz_yes = CameraPTZ(soap=_fake_ptz_soap())
    assert await ptz_yes.capabilities("cam", _LAN_RTSP) == {"ptz": True}
    ptz_no = CameraPTZ(soap=_fake_ptz_soap())
    assert await ptz_no.capabilities("cam", "rtsp://admin:pw@8.8.8.8/s") == {"ptz": False}


async def test_get_status_reads_bearing():
    ptz = CameraPTZ(soap=_fake_ptz_soap())
    assert await ptz.get_status("cam", _LAN_RTSP) == {"pan": 0.5, "tilt": -0.25, "zoom": 0.1}


async def test_get_status_fault_returns_none():
    ptz = CameraPTZ(soap=_fake_ptz_soap(fault_on="GetStatus"))
    assert await ptz.get_status("cam", _LAN_RTSP) is None


async def test_get_status_non_lan_never_contacted():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink))
    assert await ptz.get_status("cam", "rtsp://admin:pw@8.8.8.8:554/s") is None
    assert sink == []                              # SSRF: never contacted


async def test_get_status_no_creds_in_result():
    ptz = CameraPTZ(soap=_fake_ptz_soap())
    st = await ptz.get_status("cam", _LAN_RTSP)
    dump = repr(st)
    assert "pw" not in dump and "admin" not in dump and "10.0.0.5" not in dump


async def test_no_creds_in_any_result_repr():
    ptz = CameraPTZ(soap=_fake_ptz_soap(), autostop_s=999)
    out = [
        await ptz.continuous_move("cam", _LAN_RTSP, 1.0, 0.0),
        await ptz.stop("cam", _LAN_RTSP),
        await ptz.get_presets("cam", _LAN_RTSP),
        await ptz.capabilities("cam", _LAN_RTSP),
    ]
    dump = repr(out)
    assert "pw" not in dump and "admin" not in dump and "10.0.0.5" not in dump


async def test_autostop_fires_stop_without_keepalive():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink), autostop_s=0.05)
    await ptz.continuous_move("cam", _LAN_RTSP, 1.0, 0.0)
    assert not any("Stop" in b for _u, b, _a in sink)   # no stop yet
    await asyncio.sleep(0.15)                            # let the guard fire
    assert any("Stop" in b for _u, b, _a in sink)        # auto-stop sent a Stop


async def test_autostop_cancelled_by_explicit_stop():
    sink: list = []
    ptz = CameraPTZ(soap=_fake_ptz_soap(sink=sink), autostop_s=0.05)
    await ptz.continuous_move("cam", _LAN_RTSP, 1.0, 0.0)
    await ptz.stop("cam", _LAN_RTSP)                     # cancels the guard
    stops_before = sum("Stop" in b for _u, b, _a in sink)
    await asyncio.sleep(0.15)
    stops_after = sum("Stop" in b for _u, b, _a in sink)
    assert stops_after == stops_before                  # guard did not fire again


# --------------------------------------------------------------------------- #
# Routes: /api/ptz/*
# --------------------------------------------------------------------------- #

_DB_SEQ = [0]


async def _idle_frames(url):
    """Stand-in for rtsp_frames: keeps an enabled camera source ALIVE (so it reports
    `active`) without opening cv2 / touching the network. Yields nothing; cancelled
    cleanly on teardown. Reads NO frame -- matches the never-persist invariant."""
    await asyncio.sleep(3600)
    yield  # pragma: no cover -- never reached


def _client(tmp_path, monkeypatch, enable_flag=True, seed=None, ptz_soap=None):
    if enable_flag:
        monkeypatch.setenv("WAVR_PTZ", "1")
    else:
        monkeypatch.delenv("WAVR_PTZ", raising=False)
    # Neutralise the real cv2/RTSP frame source so activating a camera never opens a
    # socket or blocks -- the PTZ layer is what's under test, not frame ingestion.
    monkeypatch.setattr("wavr.sources.camera.rtsp_frames", _idle_frames)
    _DB_SEQ[0] += 1
    store = CameraStore(str(tmp_path / f"cams{_DB_SEQ[0]}.db"))
    for c in (seed or [{"name": "cam1", "room": "sala",
                        "rtsp_url": _LAN_RTSP, "confidence": 0.5}]):
        store.add(**c)
    app = create_app(sources=[], camera_store=store, ptz_soap=ptz_soap)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def _activate(c, name="cam1"):
    assert c.post("/api/system/toggle", json={"on": True}).status_code == 200
    assert c.post(f"/api/sources/{name}/toggle", json={"enabled": True}).status_code == 200


def test_status_features_ptz_flag(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enable_flag=True) as c:
        assert c.get("/api/status").json()["features"]["ptz"] is True
    with _client(tmp_path, monkeypatch, enable_flag=False) as c:
        assert c.get("/api/status").json()["features"]["ptz"] is False


def test_routes_503_when_flag_off(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enable_flag=False,
                 ptz_soap=_fake_ptz_soap()) as c:
        assert c.post("/api/ptz/cam1/move", json={"pan": 1}).status_code == 503
        assert c.post("/api/ptz/cam1/stop").status_code == 503
        assert c.get("/api/ptz/cam1/presets").status_code == 503
        assert c.post("/api/ptz/cam1/preset/1").status_code == 503
        assert c.get("/api/ptz/cam1/capabilities").status_code == 503
        assert c.get("/api/ptz/cam1/status").status_code == 503


def test_move_requires_local_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_PTZ", "1")
    store = CameraStore(str(tmp_path / "c.db"))
    store.add(name="cam1", room="sala", rtsp_url=_LAN_RTSP, confidence=0.5)
    app = create_app(sources=[], camera_store=store, ptz_soap=_fake_ptz_soap())
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/ptz/cam1/move", json={"pan": 1}).status_code == 403


def test_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        assert c.get("/api/ptz/nope/capabilities").status_code == 404


def test_bad_camera_id_400(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        # dot is not allowed by the camera-name regex
        assert c.get("/api/ptz/cam.bad/capabilities").status_code == 400


def test_bad_preset_token_400(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        _activate(c)
        assert c.post("/api/ptz/cam1/preset/" + "x" * 200).status_code in (400, 404)


def test_move_off_camera_short_circuits_no_soap(tmp_path, monkeypatch):
    sink: list = []
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap(sink=sink)) as c:
        # camera boots OFF -> move must NOT contact the camera
        r = c.post("/api/ptz/cam1/move", json={"pan": 1, "tilt": 0})
        assert r.status_code == 200
        assert r.json() == {"ok": False, "reason": "camera off"}
        assert sink == []


def test_move_happy_path_active_no_creds_in_response(tmp_path, monkeypatch):
    sink: list = []
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap(sink=sink)) as c:
        _activate(c)
        r = c.post("/api/ptz/cam1/move", json={"pan": 1, "tilt": 0})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert "pw" not in r.text and "10.0.0.5" not in r.text
        assert any("ContinuousMove" in b for _u, b, _a in sink)
        c.post("/api/ptz/cam1/stop")   # cancel the auto-stop guard before teardown


def test_capabilities_and_presets_routes_no_creds(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        rc = c.get("/api/ptz/cam1/capabilities")
        assert rc.status_code == 200 and rc.json() == {"ptz": True}
        assert "pw" not in rc.text
        rp = c.get("/api/ptz/cam1/presets")
        assert rp.status_code == 200
        assert rp.json() == [{"token": "1", "name": "Door"},
                             {"token": "2", "name": "Window"}]
        assert "pw" not in rp.text


def test_status_route_reads_bearing_no_creds(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        r = c.get("/api/ptz/cam1/status")
        assert r.status_code == 200
        assert r.json() == {"status": {"pan": 0.5, "tilt": -0.25, "zoom": 0.1}}
        assert "pw" not in r.text and "10.0.0.5" not in r.text


def test_status_route_unknown_camera_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, ptz_soap=_fake_ptz_soap()) as c:
        assert c.get("/api/ptz/nope/status").status_code == 404


def test_status_route_non_lan_camera_null_no_soap(tmp_path, monkeypatch):
    sink: list = []
    seed = [{"name": "cam1", "room": "sala",
             "rtsp_url": "rtsp://admin:pw@8.8.8.8:554/s", "confidence": 0.5}]
    with _client(tmp_path, monkeypatch, seed=seed,
                 ptz_soap=_fake_ptz_soap(sink=sink)) as c:
        r = c.get("/api/ptz/cam1/status")
        assert r.status_code == 200 and r.json() == {"status": None}
        assert sink == []                          # SSRF: never contacted


def test_capabilities_non_lan_camera_false_no_soap(tmp_path, monkeypatch):
    sink: list = []
    seed = [{"name": "cam1", "room": "sala",
             "rtsp_url": "rtsp://admin:pw@8.8.8.8:554/s", "confidence": 0.5}]
    with _client(tmp_path, monkeypatch, seed=seed,
                 ptz_soap=_fake_ptz_soap(sink=sink)) as c:
        r = c.get("/api/ptz/cam1/capabilities")
        assert r.status_code == 200 and r.json() == {"ptz": False}
        assert sink == []                          # SSRF: never contacted
