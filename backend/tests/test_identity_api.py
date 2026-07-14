import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.identity_store import IdentityStore
from wavr.known_store import KnownStore
from wavr.netinventory_service import NetworkInventoryService


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


def test_post_oversized_batch_rejected_and_not_persisted(tmp_path):
    # Audit LOW: an unbounded devices[] batch could force normalize_mac/sanitize_name
    # + store.add() over an arbitrarily large list from one request.
    from wavr.api_identity import _MAX_DEVICES_PER_BATCH
    with _client(tmp_path) as c:
        devices = [{"address": f"aa:bb:cc:dd:ee:{i:02x}"} for i in range(_MAX_DEVICES_PER_BATCH + 1)]
        r = c.post("/api/identity/devices", json={"person": "alice", "devices": devices})
        assert r.status_code == 400
        assert c.get("/api/identity/devices").json() == {"devices": []}   # nothing persisted


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


def test_known_presence_empty_registry_is_honest(tmp_path):
    # No sources at all (sources=[]) -> no fused 'casa' state either -- the route
    # must return a well-shaped, all-absent summary rather than erroring.
    with _client(tmp_path) as c:
        body = c.get("/api/identity/known-presence").json()
        assert body["scope"] == "house"
        assert body["modality"] == "network"
        assert body["likely_home"] is False
        assert body["confidence_label"] == "coarse"
        assert body["corroborators"] == []


def test_known_presence_lists_registered_device_absent_when_unseen(tmp_path):
    seed = [{"address": "aa:bb:cc:dd:ee:ff", "person": "alice",
             "source": "network", "origin": "manual"}]
    with _client(tmp_path, seed=seed) as c:
        body = c.get("/api/identity/known-presence").json()
        assert body["corroborators"] == [
            {"person": "alice", "mac_prefix": "aa:bb:cc", "present": False, "details": None}
        ]


def test_patch_details_toggles_and_persists(tmp_path):
    seed = [{"address": "aa:bb:cc:dd:ee:ff", "person": "alice",
             "source": "network", "origin": "manual"}]
    with _client(tmp_path, seed=seed) as c:
        r = c.patch("/api/identity/devices/aa:bb:cc:dd:ee:ff/details", json={"on": True})
        assert r.status_code == 200
        row = next(d for d in r.json()["devices"] if d["address"] == "aa:bb:cc:dd:ee:ff")
        assert row["details"] is True


def test_patch_details_unknown_address_404(tmp_path):
    with _client(tmp_path) as c:
        r = c.patch("/api/identity/devices/aa:bb:cc:dd:ee:ff/details", json={"on": True})
        assert r.status_code == 404


def test_patch_details_requires_csrf_header(tmp_path):
    store = IdentityStore(str(tmp_path / "id.db"))
    store.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual")
    with TestClient(create_app(sources=[], identity_store=store)) as c:   # no X-Wavr-Local
        r = c.patch("/api/identity/devices/aa:bb:cc:dd:ee:ff/details", json={"on": True})
        assert r.status_code == 403


def test_post_with_details_true_is_reflected_in_known_presence(tmp_path):
    with _client(tmp_path) as c:
        c.post("/api/identity/devices", json={
            "person": "alice",
            "devices": [{"address": "aa:bb:cc:dd:ee:ff", "source": "network", "details": True}],
        })
        devices = c.get("/api/identity/devices").json()["devices"]
        assert devices[0]["details"] is True


# ---- cluster E: registering a 'network' device also marks it known ------------
# (audit fix: naming/assigning an already-recognized house device did NOT stop
# its rogue-device alert -- "known" only meant the static allowlist/explicit
# POST /api/inventory/known. Registering it here is a second already-explicit
# admin action; wiring the two together is not a new auto-trust path.)

def test_register_network_source_marks_known_and_suppresses_alert(tmp_path):
    async def _scan():
        return """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.23          AA-BB-CC-DD-EE-FF     dynamic
"""
    identity_store = IdentityStore(str(tmp_path / "id.db"))
    ks = KnownStore(":memory:")
    inv = NetworkInventoryService(known_macs=set(), scan=_scan, interval=0,
                                  known_provider=ks.known_macs)
    asyncio.run(inv.scan_once())
    assert any(a.mac == "aa:bb:cc:dd:ee:ff" for a in inv.recent_alerts())   # unknown -> alerts

    app = create_app(sources=[], identity_store=identity_store,
                     known_store=ks, net_inventory=inv)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/identity/devices", json={
            "person": "alice",
            "devices": [{"address": "aa:bb:cc:dd:ee:ff", "source": "network"}],
        })
        assert r.status_code == 200
        assert ks.is_known("aa:bb:cc:dd:ee:ff") is True
        alerts = c.get("/api/alerts").json()["alerts"]
        assert all(a["mac"] != "aa:bb:cc:dd:ee:ff" for a in alerts)   # dropped immediately

    asyncio.run(inv.scan_once())   # next scan cycle also stays quiet
    assert all(a.mac != "aa:bb:cc:dd:ee:ff" for a in inv.recent_alerts())


def test_register_ble_source_does_not_mark_known(tmp_path):
    # Only 'network'-source registrations cross-wire into known_store -- a BLE
    # registration is a different modality with no rogue-device alert concept.
    identity_store = IdentityStore(str(tmp_path / "id.db"))
    ks = KnownStore(":memory:")
    app = create_app(sources=[], identity_store=identity_store, known_store=ks)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/identity/devices", json={
            "person": "alice",
            "devices": [{"address": "aa:bb:cc:dd:ee:ff", "source": "ble"}],
        })
        assert r.status_code == 200
    assert ks.is_known("aa:bb:cc:dd:ee:ff") is False


def test_delete_does_not_rearm_known_state(tmp_path):
    # Un-labeling (opting a device out of the identity registry) must NOT
    # silently flip it back to unknown -- only an explicit POST /api/inventory/
    # known (or /known/bulk) toggles known state off.
    identity_store = IdentityStore(str(tmp_path / "id.db"))
    ks = KnownStore(":memory:")
    ks.set_known("aa:bb:cc:dd:ee:ff", True)
    identity_store.add("aa:bb:cc:dd:ee:ff", "alice", "network", "manual")
    app = create_app(sources=[], identity_store=identity_store, known_store=ks)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.delete("/api/identity/devices/aa:bb:cc:dd:ee:ff")
        assert r.status_code == 200
    assert ks.is_known("aa:bb:cc:dd:ee:ff") is True
