"""Routes for /api/routines: CRUD + enable + the real-actuation /test button, plus the
authorization gate (household control only -- never an 'agent' or a plain 'user')."""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.devices import DeviceStore
from wavr.routines import RoutineStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}


def _app(store=None):
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=store or RoutineStore(":memory:"))


def _light(entity="light.sala", service="turn_on"):
    return {"kind": "ha_service", "params": {"domain": "light", "service": service,
                                             "entity_id": entity}}


def test_crud_lifecycle():
    with TestClient(_app(), headers=CSRF) as c:
        assert c.get("/api/routines").json()["routines"] == []
        # create (starts disabled)
        r = c.post("/api/routines", json={
            "name": "arrive light", "trigger_kind": "house_arrived",
            "actions": [_light()]}).json()
        rid = r["id"]
        assert r["enabled"] is False and r["name"] == "arrive light"
        assert len(c.get("/api/routines").json()["routines"]) == 1
        # enable
        assert c.post(f"/api/routines/{rid}/enable", json={"on": True}).status_code == 200
        assert c.get("/api/routines").json()["routines"][0]["enabled"] is True
        # update
        up = c.put(f"/api/routines/{rid}", json={
            "name": "arrive light 2", "trigger_kind": "house_left",
            "actions": [_light(service="turn_off")]})
        assert up.status_code == 200 and up.json()["trigger_kind"] == "house_left"
        # delete
        assert c.delete(f"/api/routines/{rid}").status_code == 200
        assert c.get("/api/routines").json()["routines"] == []


def test_invalid_create_is_400():
    with TestClient(_app(), headers=CSRF) as c:
        # missing the required room param for a room trigger
        r = c.post("/api/routines", json={
            "name": "bad", "trigger_kind": "room_occupied", "actions": [_light()]})
        assert r.status_code == 400
        # a forbidden action kind
        r2 = c.post("/api/routines", json={
            "name": "bad2", "trigger_kind": "house_arrived",
            "actions": [{"kind": "sensing_on", "params": {}}]})
        assert r2.status_code == 400


def test_404_on_missing_routine():
    with TestClient(_app(), headers=CSRF) as c:
        assert c.post("/api/routines/ghost/enable", json={"on": True}).status_code == 404
        assert c.delete("/api/routines/ghost").status_code == 404
        assert c.post("/api/routines/ghost/test").status_code == 404
        assert c.put("/api/routines/ghost", json={
            "name": "x", "trigger_kind": "house_arrived", "actions": [_light()]}).status_code == 404


def test_test_button_actuates_the_real_sink():
    # /test runs the actions NOW through the real gated executor. A set_watch action is
    # observable via /api/watch and needs no external HA.
    store = RoutineStore(":memory:")
    r = store.add("discreet", "schedule", trigger_params={"at": "00:00"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    with TestClient(_app(store), headers=CSRF) as c:
        assert c.get("/api/watch").json()["on"] is False
        res = c.post(f"/api/routines/{r['id']}/test")
        assert res.status_code == 200 and res.json()["status"] == "ok"
        assert c.get("/api/watch").json()["on"] is True, "the test button really ran the action"


def test_ha_entities_empty_without_home_assistant():
    with TestClient(_app(), headers=CSRF) as c:
        assert c.get("/api/routines/ha-entities").json() == {"entities": []}


# --------------------------------------------------------------------------- #
# Authorization: routines are household config -- control scope only.
# --------------------------------------------------------------------------- #
def _md_app(tmp_path, monkeypatch, role):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    seed = DeviceStore(str(tmp_path / "md.db"))
    _id, token = seed.add("dev", role)
    seed.close()
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=RoutineStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    return peer, {"Authorization": f"Bearer {token}"}


def test_agent_and_user_denied_routines(tmp_path, monkeypatch):
    # An 'agent' (scope {mcp}) must never manage routines; a plain 'user' (presence:read,
    # no control) also can't -- routines are a central/root household config.
    for role in ("agent", "user"):
        peer, auth = _md_app(tmp_path, monkeypatch, role)
        assert peer.get("/api/routines", headers=auth).status_code == 403, f"{role} read"
        assert peer.post("/api/routines", headers=auth, json={
            "name": "x", "trigger_kind": "house_arrived",
            "actions": [_light()]}).status_code == 403, f"{role} write"


def test_central_can_manage_routines(tmp_path, monkeypatch):
    peer, auth = _md_app(tmp_path, monkeypatch, "central")
    # central holds control -> reads work; a write also needs the CSRF header (require_local).
    assert peer.get("/api/routines", headers=auth).status_code == 200
