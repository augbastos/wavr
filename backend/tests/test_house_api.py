import copy
import json
from fastapi.testclient import TestClient
from wavr import housemap
from wavr.app import create_app
from wavr.housemap import load_house_map
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}

def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))

def _valid():
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[4,0],[4,3],[0,3]]}],
         "walls": [], "features": [], "backdrop": None}]}

def test_put_house_persists_and_updates_get(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    r = c.put("/api/house", json=_valid(), headers=CSRF)
    assert r.status_code == 200
    assert c.get("/api/house").json()["floors"][0]["rooms"][0]["name"] == "sala"
    assert (tmp_path / "house.json").exists()

def test_put_invalid_doc_is_422_and_writes_nothing(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    bad = _valid(); bad["floors"][0]["rooms"][0]["polygon"] = [[0,0],[1,1]]
    r = c.put("/api/house", json=bad, headers=CSRF)
    assert r.status_code == 422
    assert not (tmp_path / "house.json").exists()

def test_put_house_requires_csrf_on_loopback(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    assert c.put("/api/house", json=_valid()).status_code == 403   # no X-Wavr-Local


def test_default_house_map_persists_out_of_the_box(tmp_path, monkeypatch):
    # F1: with WAVR_HOUSE_MAP UNSET, the default is a bare cwd-relative "house.json".
    # chdir into tmp so the default resolves inside a throwaway dir (never the repo
    # root, which may hold a dev's real map). A fresh install must PUT -> 200 and
    # persist, instead of the old out-of-the-box 409.
    monkeypatch.delenv("WAVR_HOUSE_MAP", raising=False)
    monkeypatch.chdir(tmp_path)
    c = TestClient(create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:")))
    r = c.put("/api/house", json=_valid(), headers=CSRF)
    assert r.status_code == 200
    saved = tmp_path / "house.json"
    assert saved.exists()                                        # created at cwd
    # persist-across-restart: a fresh load re-reads the saved map
    assert load_house_map(str(saved))["floors"][0]["rooms"][0]["name"] == "sala"


def test_put_house_does_not_corrupt_default_map(tmp_path, monkeypatch):
    # Security MUST-FIX regression: load_house_map returns the module-level DEFAULT_MAP
    # object on any fallback, and put_house mutates _house in place (clear/update).
    # create_app must deepcopy so a PUT never rewrites DEFAULT_MAP process-wide. Point
    # WAVR_HOUSE_MAP at a not-yet-existing tmp file so _house starts as the DEFAULT_MAP
    # fallback, PUT a different map, then assert DEFAULT_MAP is unchanged.
    before = copy.deepcopy(housemap.DEFAULT_MAP)
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    c = TestClient(create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:")))
    assert c.put("/api/house", json=_valid(), headers=CSRF).status_code == 200
    assert housemap.DEFAULT_MAP == before                        # not mutated
    assert len(housemap.DEFAULT_MAP["floors"][0]["rooms"]) == 3


# === F2: PUT /api/house/room (phone "medir com o celular") =========================

def _room_body(name="escritorio", level=0, polygon=None):
    return {"level": level, "room": {"name": name,
            "polygon": polygon or [[10,10],[13,10],[13,14],[10,14]]}}


def test_put_house_room_merges_without_wiping_siblings(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    before = {r["name"] for r in c.get("/api/house").json()["floors"][0]["rooms"]}
    r = c.put("/api/house/room", json=_room_body(), headers=CSRF)
    assert r.status_code == 200
    after = {rm["name"] for rm in c.get("/api/house").json()["floors"][0]["rooms"]}
    assert after == before | {"escritorio"}                     # siblings kept + new added
    assert (tmp_path / "house.json").exists()                    # persisted


def test_put_house_room_new_level_creates_floor(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    r = c.put("/api/house/room",
              json=_room_body(name="suite", level=1, polygon=[[0,0],[3,0],[3,4],[0,4]]),
              headers=CSRF)
    assert r.status_code == 200
    floors = c.get("/api/house").json()["floors"]
    assert {f["level"] for f in floors} == {0, 1}
    f1 = next(f for f in floors if f["level"] == 1)
    assert [rm["name"] for rm in f1["rooms"]] == ["suite"]


def test_put_house_room_is_idempotent_by_name(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    c.put("/api/house/room", json=_room_body(name="sala", polygon=[[0,0],[2,0],[2,2],[0,2]]), headers=CSRF)
    c.put("/api/house/room", json=_room_body(name="sala", polygon=[[0,0],[2,0],[2,2],[0,2]]), headers=CSRF)
    rooms = c.get("/api/house").json()["floors"][0]["rooms"]
    assert [r["name"] for r in rooms].count("sala") == 1        # replaced, not duplicated


def test_put_house_room_invalid_polygon_is_422(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    r = c.put("/api/house/room", json=_room_body(polygon=[[0,0],[1,1]]), headers=CSRF)  # <3
    assert r.status_code == 422


def test_put_house_room_bad_shape_is_422(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    good_poly = [[0,0],[1,0],[1,1]]
    cases = [
        {"level": "0", "room": {"name": "x", "polygon": good_poly}},   # level not int
        {"level": True, "room": {"name": "x", "polygon": good_poly}},  # bool is not int
        {"level": 0, "room": {"name": "x"}},                            # polygon missing
        {"level": 0, "room": {"name": 5, "polygon": good_poly}},        # name not str
        {"level": 0, "room": "notadict"},                               # room not object
    ]
    for body in cases:
        assert c.put("/api/house/room", json=body, headers=CSRF).status_code == 422, body


def test_put_house_room_requires_csrf_on_loopback(tmp_path, monkeypatch):
    c = TestClient(_app(tmp_path, monkeypatch))
    assert c.put("/api/house/room", json=_room_body()).status_code == 403   # no X-Wavr-Local


def test_put_house_room_unset_map_path_is_409(tmp_path, monkeypatch):
    # Explicitly no path (WAVR_HOUSE_MAP="") -> save_house_map raises before writing, so
    # nothing lands at the repo root; the endpoint maps it to 409 (server misconfig).
    monkeypatch.setenv("WAVR_HOUSE_MAP", "")
    c = TestClient(create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:")))
    assert c.put("/api/house/room", json=_room_body(), headers=CSRF).status_code == 409


# --- LAN role gate (ADR-0006): a paired 'user' is read-only, 'central' may write ----

def _md_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def _pair(app, role):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_put_house_room_user_role_403_central_role_200(tmp_path, monkeypatch):
    app = _md_app(tmp_path, monkeypatch)
    body = _room_body(name="medido", polygon=[[0,0],[3,0],[3,4],[0,4]])
    user_peer, user_auth = _pair(app, "user")
    assert user_peer.put("/api/house/room", json=body, headers=user_auth).status_code == 403
    central_peer, central_auth = _pair(app, "central")
    # a LAN central is Bearer-authed (no X-Wavr-Local needed) and may write the map.
    assert central_peer.put("/api/house/room", json=body, headers=central_auth).status_code == 200
