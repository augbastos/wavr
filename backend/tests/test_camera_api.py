import pytest
from fastapi.testclient import TestClient
from wavr.app import create_app
from wavr.camera_store import CameraStore

def _client(tmp_path, seed=None):
    store = CameraStore(str(tmp_path / "cams.db"))
    if seed:
        for c in seed:
            store.add(**c)
    app = create_app(
        sources=[],                       # no default sources -> isolate camera behavior
        camera_store=store,
    )
    return TestClient(app, headers={"X-Wavr-Local": "1"})

def test_post_adds_camera_as_boot_off_source(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_sala", "room": "sala",
                                         "rtsp_url": "rtsp://u:pw@10.0.0.5/s1", "confidence": 0.5})
        assert r.status_code == 200
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert "cam_sala" in sysrc
        assert sysrc["cam_sala"]["enabled"] is False       # SAFETY: boots OFF

def test_get_masks_credentials(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_sala", "room": "sala",
                                  "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}]) as c:
        [cam] = c.get("/api/cameras").json()
        assert "secret" not in cam["rtsp_url"]             # password never echoed
        assert cam["name"] == "cam_sala" and cam["room"] == "sala"

def test_persisted_cameras_registered_on_startup(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" in sysrc                            # loaded from store at boot

def test_delete_removes_camera(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        assert c.delete("/api/cameras/cam_q").status_code == 200
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" not in sysrc
        assert c.delete("/api/cameras/cam_q").status_code == 404   # already gone

def test_duplicate_name_rejected(tmp_path):
    with _client(tmp_path) as c:
        body = {"name": "cam_sala", "room": "sala", "rtsp_url": "rtsp://x", "confidence": 0.5}
        assert c.post("/api/cameras", json=body).status_code == 200
        assert c.post("/api/cameras", json=body).status_code == 409   # duplicate

def test_camera_endpoints_require_local_header(tmp_path):
    store = CameraStore(str(tmp_path / "c.db"))
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/cameras", json={"name": "x", "room": "r",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5})
        assert r.status_code == 403


def test_delete_camera_requires_local_header(tmp_path):
    store = CameraStore(str(tmp_path / "d.db"))
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.delete("/api/cameras/x")
        assert r.status_code == 403


def test_post_malformed_rtsp_url_rejected_and_not_persisted(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_bad", "room": "sala",
                                         "rtsp_url": "a@b://c", "confidence": 0.5})
        assert r.status_code == 400
        assert c.get("/api/cameras").json() == []          # never persisted; later GET still works


def test_post_notaurl_rejected_and_not_persisted(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_bad2", "room": "sala",
                                         "rtsp_url": "notaurl", "confidence": 0.5})
        assert r.status_code == 400
        assert c.get("/api/cameras").json() == []


def test_mask_rtsp_never_raises_on_malformed_url():
    from wavr.app import _mask_rtsp
    assert _mask_rtsp("a@b://c") == "a@b://c"               # malformed shape -> returned unchanged, no raise


def test_post_name_with_slash_rejected(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "a/b", "room": "sala",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5})
        assert r.status_code == 400


def test_post_name_with_html_rejected(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "<img src=x>", "room": "sala",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5})
        assert r.status_code == 400


def test_post_confidence_out_of_range_rejected(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_conf", "room": "sala",
                                         "rtsp_url": "rtsp://x", "confidence": 999})
        assert r.status_code == 400


# ---- F3: optional mac on add ----------------------------------------------------

def test_post_camera_with_explicit_mac_persists_it(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_q", "room": "quarto",
                                         "rtsp_url": "rtsp://u:pw@10.0.0.5/s1",
                                         "confidence": 0.5, "mac": "AA-BB-CC-DD-EE-FF"})
        assert r.status_code == 200
        [cam] = c.get("/api/cameras").json()
        assert cam["mac"] == "aa:bb:cc:dd:ee:ff"       # normalized to lowercase colon form

def test_post_camera_with_junk_mac_rejected_and_not_persisted(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_q", "room": "quarto",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5,
                                         "mac": "not-a-mac"})
        assert r.status_code == 400
        assert c.get("/api/cameras").json() == []       # never persisted

def test_post_camera_without_mac_stores_null_out_of_box(tmp_path):
    # net_inventory is OFF by default -> auto-resolve yields null, honestly.
    with _client(tmp_path) as c:
        c.post("/api/cameras", json={"name": "cam_q", "room": "quarto",
                                     "rtsp_url": "rtsp://x", "confidence": 0.5})
        [cam] = c.get("/api/cameras").json()
        assert cam["mac"] is None


# ---- F3: GET /api/cameras/suggestions (read-only) -------------------------------

def test_suggestions_shape_empty_out_of_box(tmp_path):
    with _client(tmp_path) as c:
        body = c.get("/api/cameras/suggestions").json()
        assert body == {"suggestions": []}              # no drift / no inventory


# ---- F3: POST /api/cameras/{name}/rebind ----------------------------------------

_SEED = [{"name": "cam_q", "room": "quarto",
          "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}]

def test_rebind_rewrites_host_and_masks_password(tmp_path):
    with _client(tmp_path, seed=_SEED) as c:
        r = c.post("/api/cameras/cam_q/rebind", json={"ip": "10.0.0.9"})
        assert r.status_code == 200
        [cam] = r.json()
        assert "10.0.0.9" in cam["rtsp_url"]            # host rewritten
        assert "10.0.0.5" not in cam["rtsp_url"]        # old host gone
        assert "secret" not in r.text                   # password never echoed (masked)
        # persisted + source re-registered boot-OFF (never auto-enabled)
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert sysrc["cam_q"]["enabled"] is False

def test_rebind_requires_csrf(tmp_path):
    store = CameraStore(str(tmp_path / "r.db"))
    store.add(**_SEED[0])
    with TestClient(create_app(sources=[], camera_store=store)) as c:  # no X-Wavr-Local
        assert c.post("/api/cameras/cam_q/rebind", json={"ip": "10.0.0.9"}).status_code == 403

def test_rebind_rejects_public_ip(tmp_path):
    with _client(tmp_path, seed=_SEED) as c:
        assert c.post("/api/cameras/cam_q/rebind", json={"ip": "8.8.8.8"}).status_code == 400

def test_rebind_rejects_hostname(tmp_path):
    with _client(tmp_path, seed=_SEED) as c:
        assert c.post("/api/cameras/cam_q/rebind", json={"ip": "camera.local"}).status_code == 400

def test_rebind_rejects_cloud_metadata_ip(tmp_path):
    with _client(tmp_path, seed=_SEED) as c:
        assert c.post("/api/cameras/cam_q/rebind",
                      json={"ip": "169.254.169.254"}).status_code == 400
        assert c.post("/api/cameras/cam_q/rebind",
                      json={"ip": "::ffff:169.254.169.254"}).status_code == 400

def test_rebind_unknown_camera_404(tmp_path):
    with _client(tmp_path) as c:
        assert c.post("/api/cameras/nope/rebind", json={"ip": "10.0.0.9"}).status_code == 404

def test_rebind_clears_matching_suggestion(tmp_path):
    # A rebind must drop any drift suggestion for that camera (defence-in-depth: the
    # UI shouldn't keep offering a rebind that was already applied).
    with _client(tmp_path, seed=_SEED) as c:
        assert c.post("/api/cameras/cam_q/rebind", json={"ip": "10.0.0.9"}).status_code == 200
        assert c.get("/api/cameras/suggestions").json() == {"suggestions": []}
