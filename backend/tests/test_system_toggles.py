"""Feature "system-toggles": the two System-tab master switches (Egress /
Network sensing) backing the receipt-only #egressList/#sensingList cards.
Persisted as reserved rows (kind='system') in the SAME ConnectorStore that
backs Connectors & Services -- default-ABSENT => allowed => byte-identical to
before this feature. Covers the store predicates + the M1 write gate
(require_local CSRF + require_root, same tier as /api/block); real downstream
enforcement (guarded_call, netinventory collectors, the 4 non-connector
egress AND-gates) is covered in their own existing test modules."""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.connector_store import ConnectorStore

CSRF = {"X-Wavr-Local": "1"}


# --------------------------------------------------------------------------- #
# ConnectorStore: the two master predicates, default-ALLOW/absent-row.
# --------------------------------------------------------------------------- #
def test_store_masters_default_allowed():
    s = ConnectorStore(":memory:")
    assert s.egress_allowed() is True
    assert s.sensing_allowed() is True


def test_store_masters_flip_and_persist(tmp_path):
    p = str(tmp_path / "toggles.db")
    s = ConnectorStore(p)
    s.upsert("sys:egress", "system", "egress")
    s.set_enabled("sys:egress", False)
    assert s.egress_allowed() is False
    assert s.sensing_allowed() is True         # independent of the other master
    # Persists across a fresh instance opening the same db file.
    s2 = ConnectorStore(p)
    assert s2.egress_allowed() is False
    # Flipping back on restores byte-identical (allowed) behaviour.
    s2.set_enabled("sys:egress", True)
    assert s2.egress_allowed() is True


def test_store_sensing_master_flips_independently(tmp_path):
    s = ConnectorStore(str(tmp_path / "t.db"))
    s.upsert("sys:sensing", "system", "network_sensing")
    s.set_enabled("sys:sensing", False)
    assert s.sensing_allowed() is False
    assert s.egress_allowed() is True


# --------------------------------------------------------------------------- #
# Route: GET/POST /api/system/toggles.
# --------------------------------------------------------------------------- #
def _client(tmp_path, monkeypatch, store=None):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    store = store or ConnectorStore(":memory:")
    app = create_app(sources=[], connector_store=store)
    return TestClient(app, headers=CSRF), store


def test_get_toggles_default_allowed(tmp_path, monkeypatch):
    c, _s = _client(tmp_path, monkeypatch)
    assert c.get("/api/system/toggles").json() == {"egress": True, "network_sensing": True}


def test_post_toggle_flips_and_reflects_in_get(tmp_path, monkeypatch):
    c, s = _client(tmp_path, monkeypatch)
    r = c.post("/api/system/toggles/egress", json={"enabled": False})
    assert r.status_code == 200
    assert r.json() == {"egress": False, "network_sensing": True}
    assert c.get("/api/system/toggles").json() == {"egress": False, "network_sensing": True}
    assert s.egress_allowed() is False

    r2 = c.post("/api/system/toggles/network_sensing", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json() == {"egress": False, "network_sensing": False}

    # Reversible: flipping back on restores the default-allowed state.
    r3 = c.post("/api/system/toggles/egress", json={"enabled": True})
    assert r3.json()["egress"] is True


def test_post_toggle_404_unknown_name(tmp_path, monkeypatch):
    c, _s = _client(tmp_path, monkeypatch)
    r = c.post("/api/system/toggles/camera", json={"enabled": False})
    assert r.status_code == 404


def test_post_toggle_403_without_local_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app = create_app(sources=[], connector_store=ConnectorStore(":memory:"))
    with TestClient(app) as c:                      # no X-Wavr-Local header
        r = c.post("/api/system/toggles/egress", json={"enabled": False})
        assert r.status_code == 403


def test_post_toggle_rejects_multidevice_central_peer(tmp_path, monkeypatch):
    # Same M1 tier as /api/block: a paired 'central' peer can change other state
    # (require_local lets it through header-less) but must NOT reach this
    # loopback-root-only primitive -- 403, not a masked 404/503.
    from wavr.storage import Storage
    from wavr.camera_store import CameraStore
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"),
                     connector_store=ConnectorStore(":memory:"))
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": "central"},
                        headers={"X-Wavr-Local": "1"}).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    # Sanity: this central peer genuinely CAN change other state (header-less)...
    assert peer.post("/api/system/toggle", json={"on": False}, headers=auth).status_code == 200
    # ...but is refused on the system-toggles master switch.
    assert peer.post("/api/system/toggles/egress", json={"enabled": False},
                     headers=auth).status_code == 403
    # The read-only receipt stays reachable to the peer (router-level auth only).
    assert peer.get("/api/system/toggles", headers=auth).status_code == 200


def test_status_features_reflect_the_masters(tmp_path, monkeypatch):
    c, _s = _client(tmp_path, monkeypatch)
    feats = c.get("/api/status").json()["features"]
    assert feats["egress_allowed"] is True and feats["sensing_allowed"] is True
    c.post("/api/system/toggles/egress", json={"enabled": False})
    feats = c.get("/api/status").json()["features"]
    assert feats["egress_allowed"] is False and feats["sensing_allowed"] is True
