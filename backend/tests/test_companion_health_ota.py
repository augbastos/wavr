"""Route tests for:
  * `hub_level` on GET /api/status.features (server mirror of index.html's
    deriveTier()).
  * GET /api/companion/health (device+network health check, item 6 -- zero
    egress, passive self-report only).
  * GET /api/app/manifest + GET /api/app/bundle (pinned OTA channel, item 9).

All loopback-only (no multidevice wiring needed -- these three routes are
reachable by root exactly like every other GET, `require_authenticated`'s
root branch just needs the CSRF header).
"""
import hashlib
import io
import tarfile

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}


def _app():
    # sim registered ENABLED (unlike most other tests' boot-OFF convention) so
    # test_companion_health_reflects_running_system has a real active source to
    # observe once the system is toggled on -- SimulatedSource is a pure
    # in-process generator (no I/O), so this is zero-risk, unlike a camera.
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), True)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


# --------------------------------------------------------------------------- #
# hub_level
# --------------------------------------------------------------------------- #
def test_hub_level_off_when_system_not_running():
    with TestClient(_app(), headers=CSRF) as c:
        # the SourceManager auto-starts on the app lifespan (running=True, default tier
        # "presence" per index.html:2123); stop it to exercise the "off" branch.
        c.post("/api/system/toggle", json={"on": False})
        assert c.get("/api/status").json()["features"]["hub_level"] == "off"


def test_hub_level_presence_when_running_with_no_camera():
    with TestClient(_app(), headers=CSRF) as c:
        c.post("/api/system/toggle", json={"on": True})
        assert c.get("/api/status").json()["features"]["hub_level"] == "presence"

# NOTE: the "precise" branch (a registered camera source, enabled, running) is
# NOT exercised here -- enabling a real camera source spawns a genuine
# CameraSource task that opens an actual RTSP/cv2 connection, a pattern no
# existing test in this suite uses (test_camera_api.py only CRUDs cameras,
# never toggles one on) and out of proportion to add here. `_hub_level()`'s
# precise branch is a two-line set-intersection over already-tested data
# (`_cameras.list()` names, `manager.status()["sources"]` enabled flags) --
# code-reviewed, not executed.


# --------------------------------------------------------------------------- #
# GET /api/companion/health
# --------------------------------------------------------------------------- #
def test_companion_health_shape_and_zero_egress_fields():
    with TestClient(_app(), headers=CSRF) as c:
        # stop the auto-started manager so this exercises the not-running/zero-active state.
        c.post("/api/system/toggle", json={"on": False})
        r = c.get("/api/companion/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "system_running", "sources_active_count", "core_version",
            "my_presence_registered", "ws_clients", "last_frame_age_s",
        }
        assert body["system_running"] is False
        assert body["sources_active_count"] == 0
        assert body["ws_clients"] == 0
        # None (honest unknown, nothing fused) OR a real non-negative age if the sim
        # fused a frame in the ms before the toggle-off. Either is an honest shape.
        assert body["last_frame_age_s"] is None or body["last_frame_age_s"] >= 0
        assert isinstance(body["core_version"], str) and body["core_version"]


def test_companion_health_reflects_running_system():
    with TestClient(_app(), headers=CSRF) as c:
        c.post("/api/system/toggle", json={"on": True})
        body = c.get("/api/companion/health").json()
        assert body["system_running"] is True
        assert body["sources_active_count"] >= 1   # the boot-ON sim source


# --------------------------------------------------------------------------- #
# GET /api/app/manifest + GET /api/app/bundle
# --------------------------------------------------------------------------- #
def test_app_manifest_shape():
    with TestClient(_app(), headers=CSRF) as c:
        r = c.get("/api/app/manifest")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"version", "sha256", "size", "url"}
        assert body["url"] == "/api/app/bundle"
        assert len(body["sha256"]) == 64                 # hex sha256
        assert body["size"] > 0


def test_app_bundle_matches_manifest_hash_and_excludes_vendor():
    with TestClient(_app(), headers=CSRF) as c:
        manifest = c.get("/api/app/manifest").json()
        r = c.get("/api/app/bundle")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        data = r.content
        assert len(data) == manifest["size"]
        assert hashlib.sha256(data).hexdigest() == manifest["sha256"]
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = set(tar.getnames())
        assert "index.html" in names
        # Hard exclusions: never the frozen vendor payload, and -- structurally,
        # since these files don't even live under frontend/ -- never the mobile
        # shim/lib/native code that holds the pin.
        assert not any(n.startswith("vendor/") for n in names)
        assert "wavr-mobile-shim.js" not in names
        assert "wavr-lib.js" not in names
