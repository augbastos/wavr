"""Adding a camera without knowing what RTSP is.

The card says "this looks like a camera". The user gives the camera's own
username and password and picks a room. Wavr asks the camera for its stream
address. Nobody types a URL.
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from wavr.api_space import build_discovery_router, rtsp_url_with_credentials
from wavr.camera_store import CameraStore
from wavr.discovery_inbox import (
    KIND_CAMERA_FOUND, KIND_DEVICE_NEW, STATUS_ACCEPTED, DiscoveryInbox,
)


# -- Rebuilding the URL -------------------------------------------------------

def test_credentials_are_substituted_into_the_masked_url():
    # The probe masks only the password, so host and path survive and the real
    # URL is recoverable without the probe ever handing back a live credential.
    out = rtsp_url_with_credentials(
        "rtsp://admin:***@192.168.1.50:554/stream2", "admin", "hunter2")
    assert out == "rtsp://admin:hunter2@192.168.1.50:554/stream2"


def test_awkward_credentials_are_escaped():
    out = rtsp_url_with_credentials(
        "rtsp://admin:***@10.0.0.5/live", "ad min", "p@ss:word/1")
    assert "@10.0.0.5/live" in out
    assert "ad%20min" in out and "p%40ss%3Aword%2F1" in out
    # The escaping must not create a SECOND authority boundary.
    assert out.count("@") == 1


def test_a_camera_with_no_credentials_keeps_a_bare_url():
    assert rtsp_url_with_credentials(
        "rtsp://10.0.0.5:554/live", "", "") == "rtsp://10.0.0.5:554/live"


@pytest.mark.parametrize("masked,expect_scheme", [
    ("rtsp://admin:***@10.0.0.5/live", "rtsp://"),
    ("rtsps://admin:***@10.0.0.5/live", "rtsps://"),      # RTSP over TLS
    ("RTSP://admin:***@10.0.0.5/live", "RTSP://"),        # some cameras shout
])
def test_the_cameras_own_scheme_is_preserved(masked, expect_scheme):
    # `sources.onvif._rtsp_ok` accepts both schemes case-insensitively upstream,
    # so being stricter here only rejected legitimate cameras. The host was
    # already validated as a LAN literal before masking, so nothing is lost.
    out = rtsp_url_with_credentials(masked, "u", "p")
    assert out.startswith(expect_scheme)
    assert out.endswith("@10.0.0.5/live")


@pytest.mark.parametrize("junk", [
    "http://10.0.0.5/live", "", "not a url", "rtsp://", "rtsp://@",
    "file:///etc/passwd", "rtspx://10.0.0.5/live"])
def test_anything_that_is_not_a_usable_rtsp_url_is_refused(junk):
    # A malformed probe result must never be stored as a camera.
    assert rtsp_url_with_credentials(junk, "a", "b") == ""


# -- The flow -----------------------------------------------------------------

class _Probe:
    """Stands in for the real ONVIF probe. The real one is already covered by
    its own tests; what matters here is the wiring around it."""

    def __init__(self, cameras=None, needs_creds=False):
        self._cameras = cameras or []
        self._needs_creds = needs_creds
        self.calls = []

    async def __call__(self, targets=None, username=None, password=None,
                       timeout=3.0):
        self.calls.append({"targets": targets, "username": username})
        if self._needs_creds and not username:
            return {"cameras": [], "errors": []}
        return {"cameras": self._cameras, "errors": []}


def _app(inbox, cameras, probe, onvif_enabled=True):
    app = FastAPI()
    app.include_router(build_discovery_router(
        inbox, deps=[Depends(lambda: None)], cameras=cameras,
        onvif_probe=probe, onvif_enabled=onvif_enabled))
    return TestClient(app)


@pytest.fixture
def scene():
    inbox = DiscoveryInbox(":memory:")
    cameras = CameraStore(":memory:")
    item = inbox.observe(KIND_CAMERA_FOUND, "aa:bb:cc:dd",
                         "Tapo C210 — looks like a camera.",
                         detail={"ip": "192.168.1.50", "mac": "aa:bb:cc:dd",
                                 "vendor": "TP-Link"},
                         confidence=0.9)
    yield inbox, cameras, item
    inbox.close()
    cameras.close()


def test_the_whole_add_a_camera_journey(scene):
    inbox, cameras, item = scene
    probe = _Probe([{"ip": "192.168.1.50", "name": "Tapo C210",
                     "rtsp_url": "rtsp://admin:***@192.168.1.50:554/stream2"}])
    with _app(inbox, cameras, probe) as c:
        r = c.post(f"/api/discoveries/{item.discovery_id}/add-camera",
                   json={"room": "Hall", "username": "admin", "password": "pw"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "added" and body["room"] == "Hall"
        # Cameras boot OFF (ADR-0002). Adding one is not turning one on.
        assert body["enabled"] is False

    # It was probed at the address the discovery knew, and stored with a usable
    # URL the user never saw or typed.
    assert probe.calls[0]["targets"] == ["192.168.1.50"]
    row = cameras.list()[0]
    assert row["room"] == "Hall"
    assert row["rtsp_url"] == "rtsp://admin:pw@192.168.1.50:554/stream2"
    assert row["mac"] == "aa:bb:cc:dd", "bound for later IP-drift detection"
    # And the card is done nagging.
    assert inbox.get(item.discovery_id).status == STATUS_ACCEPTED


def test_a_camera_that_wants_credentials_asks_rather_than_failing(scene):
    inbox, cameras, item = scene
    probe = _Probe([], needs_creds=True)
    with _app(inbox, cameras, probe) as c:
        r = c.post(f"/api/discoveries/{item.discovery_id}/add-camera",
                   json={"room": "Hall"})
        # NOT an error: wanting credentials is the normal case.
        assert r.status_code == 200
        assert r.json()["status"] == "needs_credentials"
        assert "username and password" in r.json()["message"]
    assert cameras.list() == [], "nothing is stored until it actually works"


def test_wrong_credentials_say_so_plainly(scene):
    inbox, cameras, item = scene
    probe = _Probe([])          # answers, but yields no stream
    with _app(inbox, cameras, probe) as c:
        body = c.post(f"/api/discoveries/{item.discovery_id}/add-camera",
                      json={"room": "Hall", "username": "admin",
                            "password": "wrong"}).json()
        assert body["status"] == "unreachable"
        assert "username and password" in body["message"]
        assert "RTSP" not in body["message"], "the user should not meet that word"


def test_the_probe_switch_is_named_not_the_env_var(scene):
    inbox, cameras, item = scene
    with _app(inbox, cameras, _Probe([]), onvif_enabled=False) as c:
        r = c.post(f"/api/discoveries/{item.discovery_id}/add-camera",
                   json={"room": "Hall"})
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "Look for cameras" in detail
        assert "WAVR_" not in detail, "an env var is not an instruction"


def test_only_a_camera_discovery_can_become_a_camera(scene):
    inbox, cameras, _ = scene
    other = inbox.observe(KIND_DEVICE_NEW, "ff:ee", "A laptop")
    with _app(inbox, cameras, _Probe([])) as c:
        assert c.post(f"/api/discoveries/{other.discovery_id}/add-camera",
                      json={"room": "Hall"}).status_code == 400


def test_an_unknown_discovery_is_a_404(scene):
    inbox, cameras, _ = scene
    with _app(inbox, cameras, _Probe([])) as c:
        assert c.post("/api/discoveries/nope/add-camera",
                      json={"room": "Hall"}).status_code == 404


def test_a_discovery_with_no_address_cannot_be_probed():
    inbox = DiscoveryInbox(":memory:")
    cameras = CameraStore(":memory:")
    try:
        item = inbox.observe(KIND_CAMERA_FOUND, "aa:bb", "A camera somewhere",
                             detail={"vendor": "TP-Link"})
        with _app(inbox, cameras, _Probe([])) as c:
            assert c.post(f"/api/discoveries/{item.discovery_id}/add-camera",
                          json={"room": "Hall"}).status_code == 422
    finally:
        inbox.close()
        cameras.close()


def test_the_route_is_gated_like_every_other_admin_route():
    inbox = DiscoveryInbox(":memory:")
    try:
        app = FastAPI()
        app.include_router(build_discovery_router(inbox))   # no deps wired
        with TestClient(app) as c:
            r = c.post("/api/discoveries/x/add-camera", json={"room": "Hall"})
            assert r.status_code == 403
            assert "no auth gate wired" in r.json()["detail"]
    finally:
        inbox.close()
