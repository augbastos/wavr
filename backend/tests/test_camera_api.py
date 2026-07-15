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


def test_mask_rtsp_masks_a_password_containing_at():
    # CREDENTIAL LEAK. _mask_rtsp split the authority on the FIRST "@", but the userinfo/host
    # boundary is the LAST one (RFC 3986, and what ffmpeg does -- camera_url.py already
    # rpartitions the same URL). A camera password containing "@" -- ordinary in real creds --
    # had its TAIL shipped VERBATIM to every paired `user` via GET /api/cameras:
    #   rtsp://user:p@ss@cam.local/stream  ->  rtsp://user:***@ss@cam.local/stream
    #                                                          ^^ the password, in the clear
    from wavr.app import _mask_rtsp
    assert _mask_rtsp("rtsp://user:p@ss@cam.local/stream") == "rtsp://user:***@cam.local/stream"
    assert _mask_rtsp("rtsp://user:s3nh@!@192.0.2.9/live") == "rtsp://user:***@192.0.2.9/live"
    assert _mask_rtsp("rtsp://user:a@b@c@host/s") == "rtsp://user:***@host/s"
    assert _mask_rtsp("rtsp://user:simples@host/s") == "rtsp://user:***@host/s"   # no regression
    assert _mask_rtsp("rtsp://cam.local/stream") == "rtsp://cam.local/stream"     # no creds -> untouched
    # onvif.py carries a DELIBERATE copy of this helper (so the source module has no app.py
    # dependency) and had the identical bug. It must not drift back.
    from wavr.sources.onvif import _mask_rtsp as _mask_onvif
    assert _mask_onvif("rtsp://user:p@ss@cam.local/stream") == "rtsp://user:***@cam.local/stream"
    assert _mask_onvif("a@b://c") == "a@b://c"


def test_get_never_echoes_a_password_containing_at(tmp_path):
    # The real exposure path, end to end: GET /api/cameras is readable by every paired user.
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://user:p@ss@10.0.0.5/s1",
                                  "confidence": 0.5}]) as c:
        [cam] = c.get("/api/cameras").json()
        assert "p@ss" not in cam["rtsp_url"]
        assert "ss@" not in cam["rtsp_url"], "the password TAIL leaked past the mask"
        assert cam["rtsp_url"] == "rtsp://user:***@10.0.0.5/s1"


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


# ---- Tapo privacy-mode CONTROL: a deliberate, honest 501 stub (feature 2) --------
# See wavr.camera_privacy for why: no documented ONVIF/local Tapo path exists, and
# implementing TP-Link's undocumented encrypted control API without real hardware to
# validate against would be guessing a security-sensitive protocol. Detection (feature
# 1, /api/cameras liveness='privacy') is fully implemented; this route only proves the
# gap is honest, gated, and discoverable -- never that it silently pretends to work.

def test_privacy_mode_route_is_a_flagged_501_stub(tmp_path):
    with _client(tmp_path, seed=_SEED) as c:
        r = c.post("/api/cameras/cam_q/privacy-mode", json={"enabled": True})
        assert r.status_code == 501
        # never echoes the stored credential back, even in a stub's error body
        assert "secret" not in r.text and "user:secret" not in r.text

def test_privacy_mode_route_unknown_camera_404(tmp_path):
    with _client(tmp_path) as c:
        assert c.post("/api/cameras/nope/privacy-mode",
                      json={"enabled": True}).status_code == 404

def test_privacy_mode_route_requires_csrf(tmp_path):
    store = CameraStore(str(tmp_path / "p.db"))
    store.add(**_SEED[0])
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/cameras/cam_q/privacy-mode", json={"enabled": True})
        assert r.status_code == 403


# ---- Geometry fix (HIGH-1): optional per-camera `level` disambiguates a same-named
# room across floors (see housemap.room_polygon's `level` param + app._camera_factory).
# Two floors intentionally share the room name "quarto" at DIFFERENT polygons so a
# passing test can only mean the level actually selected the right floor, not merely
# that a number round-tripped through the store.
# ------------------------------------------------------------------------------------

_TWO_FLOOR_HOUSE = {"version": 2, "units": "m", "floors": [
    {"id": "f0", "name": "Ground", "level": 0,
     "rooms": [{"id": "r0", "name": "quarto", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
     "walls": [], "features": [], "backdrop": None},
    {"id": "f1", "name": "Upstairs", "level": 1,
     "rooms": [{"id": "r1", "name": "quarto", "polygon": [[10, 10], [14, 10], [14, 13], [10, 13]]}],
     "walls": [], "features": [], "backdrop": None},
]}


def _client_two_floor_house(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    store = CameraStore(str(tmp_path / "cams.db"))
    app = create_app(sources=[], camera_store=store)
    c = TestClient(app, headers={"X-Wavr-Local": "1"})
    assert c.put("/api/house", json=_TWO_FLOOR_HOUSE, headers={"X-Wavr-Local": "1"}).status_code == 200
    return c


def test_post_camera_level_validated_against_house_floors(tmp_path, monkeypatch):
    with _client_two_floor_house(tmp_path, monkeypatch) as c:
        r = c.post("/api/cameras", json={"name": "cam_up", "room": "quarto",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5,
                                         "level": 9})               # no floor at level 9
        assert r.status_code == 400
        assert "level" in r.json()["detail"]
        assert c.get("/api/cameras").json() == []                  # never persisted


def test_post_camera_level_persists_and_round_trips(tmp_path, monkeypatch):
    with _client_two_floor_house(tmp_path, monkeypatch) as c:
        r = c.post("/api/cameras", json={"name": "cam_up", "room": "quarto",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5, "level": 1})
        assert r.status_code == 200
        [cam] = c.get("/api/cameras").json()
        assert cam["level"] == 1


def test_post_camera_without_level_stores_null_out_of_box(tmp_path, monkeypatch):
    with _client_two_floor_house(tmp_path, monkeypatch) as c:
        c.post("/api/cameras", json={"name": "cam_any", "room": "quarto",
                                     "rtsp_url": "rtsp://x", "confidence": 0.5})
        [cam] = c.get("/api/cameras").json()
        assert cam["level"] is None


def test_calib_spots_disambiguates_same_named_room_by_level(tmp_path, monkeypatch):
    # The real seam: room_polygon(house, room, level=cam.get("level")) must resolve
    # EACH camera to its OWN floor's polygon, not deterministically the first match.
    with _client_two_floor_house(tmp_path, monkeypatch) as c:
        c.post("/api/cameras", json={"name": "cam_ground", "room": "quarto",
                                     "rtsp_url": "rtsp://x", "confidence": 0.5, "level": 0})
        c.post("/api/cameras", json={"name": "cam_up", "room": "quarto",
                                     "rtsp_url": "rtsp://x", "confidence": 0.5, "level": 1})
        ground = c.get("/api/cameras/cam_ground/calib-spots").json()["spots"]
        up = c.get("/api/cameras/cam_up/calib-spots").json()["spots"]
        assert ground != up
        assert (ground[0]["x"], ground[0]["y"]) == pytest.approx((2.0, 1.5))   # ground centroid
        assert (up[0]["x"], up[0]["y"]) == pytest.approx((12.0, 11.5))         # upstairs centroid


def test_calib_spots_without_level_falls_back_to_first_matching_floor(tmp_path, monkeypatch):
    # Old (pre-level) behaviour unchanged: no `level` -> room_polygon's documented
    # deterministic first-match across floors in document order (the ground floor here).
    with _client_two_floor_house(tmp_path, monkeypatch) as c:
        c.post("/api/cameras", json={"name": "cam_any", "room": "quarto",
                                     "rtsp_url": "rtsp://x", "confidence": 0.5})
        spots = c.get("/api/cameras/cam_any/calib-spots").json()["spots"]
        assert (spots[0]["x"], spots[0]["y"]) == pytest.approx((2.0, 1.5))
