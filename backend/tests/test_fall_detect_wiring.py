"""A9 -- app-level wiring: the real create_app pipeline, a camera-like source emitting
'lying' targets, WAVR_FALL_DETECT/WAVR_FALL_DWELL_S config, and the real housemap (bed/rest
zones editable via PUT /api/house). Mirrors test_house_status_wiring.py / test_watch.py's
own style: injected fakes stand in for I/O, the real wiring/route is exercised end-to-end.
"""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}

# Deterministic event stream: a single target lying at room-local (1.0, 1.0) for a LONG
# time. Fusion is injected WITHOUT a wall-clock now_fn (see FusionEngine docstring), so it
# ages each event against ITS OWN ts, never real time -- the dwell math below is driven
# entirely by these fixed ISO strings, never by how fast the test actually runs.
T0 = "2026-07-10T00:00:00+00:00"
T1 = "2026-07-10T00:00:02+00:00"   # 2s after T0


class _LyingSource:
    """Two 'lying' readings 2s apart in room 'quarto', then holds forever -- enough for a
    dwell_s<=2 detector to cross threshold on the second event."""

    async def events(self):
        tgt = (Target(id=1, x=1.0, y=1.0, posture="lying", confidence=0.9),)
        yield SensingEvent(room="quarto", modality="camera", presence=True, motion=0.1,
                           breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=T0,
                           targets=tgt, count=1)
        yield SensingEvent(room="quarto", modality="camera", presence=True, motion=0.1,
                           breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=T1,
                           targets=tgt, count=1)
        while True:
            await asyncio.sleep(0.05)


class _FakeInvService:
    def latest_inventory(self):
        return []

    def recent_alerts(self):
        return []

    async def start(self):
        return None

    async def stop(self):
        return None


def _settle(client, rooms, tries=40):
    for _ in range(tries):
        st = client.get("/api/state").json()
        if all(r in st for r in rooms):
            return st
        time.sleep(0.05)
    return client.get("/api/state").json()


def _house_doc(zones=None):
    return {
        "version": 2, "units": "m", "floors": [{
            "id": "f0", "name": "T", "level": 0, "walls": [], "features": [],
            "backdrop": None,
            "rooms": [{"id": "r1", "name": "quarto", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
            "zones": zones or [],
        }],
    }


def test_fall_detect_off_by_default_no_env(monkeypatch):
    monkeypatch.delenv("WAVR_FALL_DETECT", raising=False)
    app = create_app(sources=[("cam", lambda: _LyingSource(), True)],
                      storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                      camera_store=CameraStore(":memory:"), net_inventory=_FakeInvService())
    with TestClient(app) as client:
        _settle(client, ["quarto"])
        time.sleep(0.2)
        body = client.get("/api/alerts").json()
    assert not [a for a in body["alerts"] if a["kind"] == "fall_suspected"]


def test_fall_detect_fires_when_lying_outside_any_zone(monkeypatch, tmp_path):
    house_path = tmp_path / "house.json"
    house_path.write_text(json.dumps(_house_doc(zones=[])), encoding="utf-8")
    monkeypatch.setenv("WAVR_FALL_DETECT", "1")
    monkeypatch.setenv("WAVR_FALL_DWELL_S", "1")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(house_path))
    try:
        app = create_app(sources=[("cam", lambda: _LyingSource(), True)],
                          storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                          camera_store=CameraStore(":memory:"), net_inventory=_FakeInvService())
        with TestClient(app) as client:
            _settle(client, ["quarto"])
            time.sleep(0.2)
            alerts = client.get("/api/alerts").json()["alerts"]
            status = client.get("/api/house-status").json()
        fall = [a for a in alerts if a["kind"] == "fall_suspected"]
        assert fall, alerts
        assert fall[0]["room"] == "quarto" and fall[0]["severity"] == "alert"
        assert "duration_s" in fall[0] and "disclaimer" in fall[0]
        # never a target position/posture -- ADR-0002/ADR-0003 egress discipline
        assert "x" not in fall[0] and "y" not in fall[0] and "posture" not in fall[0]
        phys = [r for r in status["reasons"] if r["kind"] == "fall_suspected"]
        assert phys and phys[0]["layer"] == "physical"
    finally:
        for k in ("WAVR_FALL_DETECT", "WAVR_FALL_DWELL_S", "WAVR_HOUSE_MAP"):
            monkeypatch.delenv(k, raising=False)


def test_fall_detect_never_fires_when_lying_inside_a_marked_bed_zone(monkeypatch, tmp_path):
    # The zone covers (0.5..2, 0.5..2) -- the source's target sits at room-local (1.0, 1.0),
    # squarely inside it (A9 requirement #1: lying in bed never alerts).
    house_path = tmp_path / "house.json"
    zone = [{"id": "z1", "name": "bed", "kind": "rest",
            "polygon": [[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0]]}]
    house_path.write_text(json.dumps(_house_doc(zones=zone)), encoding="utf-8")
    monkeypatch.setenv("WAVR_FALL_DETECT", "1")
    monkeypatch.setenv("WAVR_FALL_DWELL_S", "1")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(house_path))
    try:
        app = create_app(sources=[("cam", lambda: _LyingSource(), True)],
                          storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                          camera_store=CameraStore(":memory:"), net_inventory=_FakeInvService())
        with TestClient(app) as client:
            _settle(client, ["quarto"])
            time.sleep(0.2)
            alerts = client.get("/api/alerts").json()["alerts"]
        assert not [a for a in alerts if a["kind"] == "fall_suspected"]
    finally:
        for k in ("WAVR_FALL_DETECT", "WAVR_FALL_DWELL_S", "WAVR_HOUSE_MAP"):
            monkeypatch.delenv(k, raising=False)


def test_fall_detect_zone_editable_via_put_house(monkeypatch, tmp_path):
    # A9 requirement #1: the SAME PUT /api/house the map editor already uses persists a
    # bed/rest zone -- no separate zone-CRUD route needed.
    house_path = tmp_path / "house.json"
    house_path.write_text(json.dumps(_house_doc(zones=[])), encoding="utf-8")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(house_path))
    try:
        app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                          net_inventory=_FakeInvService())
        with TestClient(app) as client:
            zone = [{"id": "z1", "name": "bed", "kind": "rest",
                    "polygon": [[0.5, 0.5], [2.0, 0.5], [2.0, 2.0], [0.5, 2.0]]}]
            r = client.put("/api/house", json=_house_doc(zones=zone), headers=LOCAL)
            assert r.status_code == 200
            got = client.get("/api/house").json()
            assert got["floors"][0]["zones"][0]["kind"] == "rest"
    finally:
        monkeypatch.delenv("WAVR_HOUSE_MAP", raising=False)
