import json
from fastapi.testclient import TestClient
from wavr.app import create_app
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
