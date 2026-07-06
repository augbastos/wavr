from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.identity_store import IdentityStore


def _client(tmp_path, seed=None, bonded=None):
    store = IdentityStore(str(tmp_path / "id.db"))
    if seed:
        for row in seed:
            store.add(**row)

    async def _bonded():
        return list(bonded or [])

    app = create_app(sources=[], identity_store=store, bonded_reader=_bonded)
    return TestClient(app, headers={"X-Wavr-Local": "1"})


def test_get_devices_lists_registered(tmp_path):
    seed = [{"address": "aa:bb:cc:dd:ee:ff", "person": "alice",
             "source": "ble", "origin": "bonded"}]
    with _client(tmp_path, seed=seed) as c:
        body = c.get("/api/identity/devices").json()
        assert body["devices"][0]["address"] == "aa:bb:cc:dd:ee:ff"
        assert body["devices"][0]["person"] == "alice"


def test_post_registers_and_lists(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/identity/devices", json={
            "person": "alice",
            "devices": [{"address": "AA-BB-CC-DD-EE-FF", "origin": "bonded"}],
        })
        assert r.status_code == 200
        addrs = {d["address"] for d in c.get("/api/identity/devices").json()["devices"]}
        assert "aa:bb:cc:dd:ee:ff" in addrs           # normalized + persisted


def test_post_ble_device_brings_up_source_live(tmp_path):
    # Ethics/live requirement: registering the first BLE device on an install with
    # no BLE source must make it a signal WITHOUT a restart -> the source appears.
    with _client(tmp_path) as c:
        assert "ble" not in {s["name"] for s in c.get("/api/system").json()["sources"]}
        c.post("/api/identity/devices", json={
            "person": "alice",
            "devices": [{"address": "aa:bb:cc:dd:ee:ff", "source": "ble"}],
        })
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert "ble" in sysrc and sysrc["ble"]["enabled"] is True


def test_delete_is_optout(tmp_path):
    seed = [{"address": "aa:bb:cc:dd:ee:ff", "person": "alice",
             "source": "ble", "origin": "bonded"}]
    with _client(tmp_path, seed=seed) as c:
        assert c.delete("/api/identity/devices/aa:bb:cc:dd:ee:ff").status_code == 200
        assert c.get("/api/identity/devices").json() == {"devices": []}
        assert c.delete("/api/identity/devices/aa:bb:cc:dd:ee:ff").status_code == 404


def test_bonded_flags_already_registered(tmp_path):
    seed = [{"address": "aa:bb:cc:dd:ee:ff", "person": "alice",
             "source": "ble", "origin": "bonded"}]
    bonded = [{"address": "aa:bb:cc:dd:ee:ff", "name": "Pixel"},
              {"address": "11:22:33:44:55:66", "name": "Housemate Watch"}]
    with _client(tmp_path, seed=seed, bonded=bonded) as c:
        got = {d["address"]: d for d in c.get("/api/identity/bonded").json()["devices"]}
        assert got["aa:bb:cc:dd:ee:ff"]["already_registered"] is True
        assert got["11:22:33:44:55:66"]["already_registered"] is False   # admin can uncheck


def test_post_junk_address_rejected_and_not_persisted(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/identity/devices", json={
            "person": "alice", "devices": [{"address": "not-a-mac"}]})
        assert r.status_code == 400
        assert c.get("/api/identity/devices").json() == {"devices": []}


def test_post_batch_is_atomic_on_junk(tmp_path):
    # One junk address rejects the WHOLE batch -> nothing persisted.
    with _client(tmp_path) as c:
        r = c.post("/api/identity/devices", json={"person": "alice", "devices": [
            {"address": "aa:bb:cc:dd:ee:ff"}, {"address": "garbage"}]})
        assert r.status_code == 400
        assert c.get("/api/identity/devices").json() == {"devices": []}


def test_post_bad_source_rejected(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/identity/devices", json={"person": "x", "devices": [
            {"address": "aa:bb:cc:dd:ee:ff", "source": "camera"}]})
        assert r.status_code == 400


def test_post_empty_person_rejected(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/identity/devices", json={
            "person": "", "devices": [{"address": "aa:bb:cc:dd:ee:ff"}]})
        assert r.status_code == 400


def test_delete_junk_address_400(tmp_path):
    with _client(tmp_path) as c:
        assert c.delete("/api/identity/devices/not-a-mac").status_code == 400


def test_post_requires_csrf_header(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    with TestClient(create_app(sources=[], identity_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/identity/devices", json={
            "person": "x", "devices": [{"address": "aa:bb:cc:dd:ee:ff"}]})
        assert r.status_code == 403


def test_delete_requires_csrf_header(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    store.add("aa:bb:cc:dd:ee:ff", "alice", "ble", "bonded")
    with TestClient(create_app(sources=[], identity_store=store)) as c:   # no X-Wavr-Local
        assert c.delete("/api/identity/devices/aa:bb:cc:dd:ee:ff").status_code == 403


def test_get_devices_no_csrf_needed(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    with TestClient(create_app(sources=[], identity_store=store)) as c:   # read, no header
        assert c.get("/api/identity/devices").status_code == 200
