"""GET /api/transparency (feature #6 "What Wavr knows about you") -- the trust
screen a non-technical user reads to decide whether to keep using Wavr. Every
assertion here proves HONESTY, not just a 200: each egress row's `on` must
track the SAME chokepoint its own feature actually enforces (never a softer,
rosier read), and each count must come from the SAME store its own existing
endpoint already counts from. Real create_app wiring end to end (mirrors
test_notify_fanout.py / test_routines_integration.py's own style: real
pipeline, injected stores/fakes standing in for I/O).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.connector_store import ConnectorStore
from wavr.device_meta import DeviceMeta
from wavr.identity_store import IdentityStore
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}   # state-changing routes require this header (CSRF guard)


def _client(**kwargs):
    # Every store this helper doesn't receive still falls back to cfg.db_path
    # ("wavr.db", cwd-relative) -- pin it to :memory: (mirrors test_notify_fanout.py)
    # so a test run never grows the gitignored local wavr.db.
    kwargs.setdefault("sources", [])
    kwargs.setdefault("storage", Storage(":memory:"))
    kwargs.setdefault("camera_store", CameraStore(":memory:"))
    return TestClient(create_app(**kwargs), headers=LOCAL)


# --------------------------------------------------------------------------- #
# Baseline shape + default-OFF posture
# --------------------------------------------------------------------------- #

def test_default_shape_and_all_egress_off(monkeypatch, tmp_path):
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "nonexistent.json"))  # -> DEFAULT_MAP
    with _client() as c:
        r = c.get("/api/transparency")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"sensing_on", "cameras", "counts", "egress"}
        assert body["sensing_on"] is True          # SourceManager boots running
        assert body["cameras"] == []
        assert body["counts"] == {"people_known": 0, "devices_seen": 0, "rooms": 3}
        channels = {row["channel"]: row["on"] for row in body["egress"]}
        assert channels == {
            "Home Assistant control": False,
            "Telegram notifications": False,
            "ntfy notifications": False,
            "Cloud AI narrator": False,
        }
        for row in body["egress"]:
            assert set(row) == {"channel", "on", "detail"}
            assert isinstance(row["detail"], str) and row["detail"]


# --------------------------------------------------------------------------- #
# 1. Telegram: off by default, on once the connector is enabled
# --------------------------------------------------------------------------- #

def test_telegram_egress_reflects_the_real_connector_gate(monkeypatch):
    monkeypatch.setenv("WAVR_DB", ":memory:")
    store = ConnectorStore(":memory:")   # "telegram" row absent -> is_enabled() False
    with _client(connector_store=store) as c:
        off = c.get("/api/transparency").json()
        row = next(x for x in off["egress"] if x["channel"] == "Telegram notifications")
        assert row["on"] is False

    store2 = ConnectorStore(":memory:")
    store2.upsert("telegram", "generic", "Telegram Notify")
    store2.set_enabled("telegram", True)
    with _client(connector_store=store2) as c:
        on = c.get("/api/transparency").json()
        row = next(x for x in on["egress"] if x["channel"] == "Telegram notifications")
        assert row["on"] is True


# --------------------------------------------------------------------------- #
# 2. sensing_on reflects the real SourceManager running state (the SAME source
#    the routines engine + dashboard read: manager.status().get("running"))
# --------------------------------------------------------------------------- #

def test_sensing_on_reflects_manager_running_state():
    with _client() as c:
        assert c.get("/api/transparency").json()["sensing_on"] is True
        r = c.post("/api/system/toggle", json={"on": False})
        assert r.status_code == 200
        assert c.get("/api/transparency").json()["sensing_on"] is False
        c.post("/api/system/toggle", json={"on": True})
        assert c.get("/api/transparency").json()["sensing_on"] is True


# --------------------------------------------------------------------------- #
# 3. Cameras reflect the real on/off SourceManager state (cameras always boot
#    OFF -- camera_store.py's own invariant -- so a freshly-registered camera
#    must read off, and flipping its source must flip the row).
# --------------------------------------------------------------------------- #

def test_cameras_reflect_real_on_off_state(tmp_path):
    store = CameraStore(str(tmp_path / "cams.db"))
    store.add("cam_quarto", "quarto", "rtsp://u:pw@10.0.0.5/s1", 0.5)
    with _client(camera_store=store) as c:
        body = c.get("/api/transparency").json()
        assert body["cameras"] == [{"name": "cam_quarto", "on": False}]   # boot-OFF

        r = c.post("/api/sources/cam_quarto/toggle", json={"enabled": True})
        assert r.status_code == 200
        body2 = c.get("/api/transparency").json()
        assert body2["cameras"] == [{"name": "cam_quarto", "on": True}]

        # never a credential/rtsp leak on this trust screen
        raw = json.dumps(body2).lower()
        for leak in ("rtsp", "pw", "10.0.0.5"):
            assert leak not in raw


# --------------------------------------------------------------------------- #
# 4. Counts are the real counts: seed N devices/rooms/identities -> match.
# --------------------------------------------------------------------------- #

def test_counts_match_seeded_real_data(tmp_path, monkeypatch):
    identity = IdentityStore(str(tmp_path / "id.db"))
    identity.add("aa:bb:cc:dd:ee:01", "alice")
    identity.add("aa:bb:cc:dd:ee:02", "carla")
    identity.add_anonymous("aa:bb:cc:dd:ee:03")   # consented, unnamed -> NOT counted as known

    devices = DeviceMeta(str(tmp_path / "dm.db"))
    for mac in ("11:22:33:44:55:01", "11:22:33:44:55:02", "11:22:33:44:55:03"):
        devices.seen(mac)

    house = {
        "version": 2, "units": "m",
        "floors": [
            {"id": "f0", "name": "Terreo", "level": 0, "rooms": [
                {"id": "r1", "name": "sala", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
                {"id": "r2", "name": "quarto", "polygon": [[2, 0], [3, 0], [3, 1], [2, 1]]},
            ], "walls": [], "features": [], "zones": [], "backdrop": None},
            {"id": "f1", "name": "1o andar", "level": 1, "rooms": [
                {"id": "r3", "name": "escritorio", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            ], "walls": [], "features": [], "zones": [], "backdrop": None},
        ],
    }
    house_path = tmp_path / "house.json"
    house_path.write_text(json.dumps(house), encoding="utf-8")
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(house_path))
    monkeypatch.setenv("WAVR_DB", ":memory:")

    with _client(identity_store=identity, device_meta=devices) as c:
        counts = c.get("/api/transparency").json()["counts"]
        assert counts == {"people_known": 2, "devices_seen": 3, "rooms": 3}


# --------------------------------------------------------------------------- #
# Home Assistant control: on ONLY when mcp_control is opt-in AND a real client
# resolves (ha_url + ha_token both set) -- matches _connector_catalog's
# hactl_env exactly.
# --------------------------------------------------------------------------- #

def test_ha_control_egress_requires_both_the_flag_and_a_resolvable_client(monkeypatch):
    monkeypatch.setenv("WAVR_DB", ":memory:")
    # mcp_control on, but no HA creds -> client_from_config(cfg) is None -> honestly off.
    monkeypatch.setenv("WAVR_MCP_CONTROL", "true")
    monkeypatch.delenv("WAVR_HA_URL", raising=False)
    monkeypatch.delenv("WAVR_HA_TOKEN", raising=False)
    with _client() as c:
        row = next(x for x in c.get("/api/transparency").json()["egress"]
                   if x["channel"] == "Home Assistant control")
        assert row["on"] is False

    # HA creds present, but mcp_control off -> still honestly off (read-only import
    # only, no control granted).
    monkeypatch.setenv("WAVR_HA_URL", "http://ha.local:8123")
    monkeypatch.setenv("WAVR_HA_TOKEN", "tok")
    monkeypatch.setenv("WAVR_MCP_CONTROL", "false")
    with _client() as c:
        row = next(x for x in c.get("/api/transparency").json()["egress"]
                   if x["channel"] == "Home Assistant control")
        assert row["on"] is False

    # Both present -> on.
    monkeypatch.setenv("WAVR_MCP_CONTROL", "true")
    with _client() as c:
        row = next(x for x in c.get("/api/transparency").json()["egress"]
                   if x["channel"] == "Home Assistant control")
        assert row["on"] is True

    for v in ("WAVR_MCP_CONTROL", "WAVR_HA_URL", "WAVR_HA_TOKEN"):
        monkeypatch.delenv(v, raising=False)


# --------------------------------------------------------------------------- #
# 5. Cloud AI narrator -- THE honesty nuance: a LOCAL (ollama) narrator is
#    zero-egress and must report False; a CLOUD provider (even a cheap/fast
#    one) must report True once it is actually live. Neither
#    make_ollama_generate nor make_gemini_generate does any network/import
#    work at construction time (see narrator.py) -- building a real Narrator
#    here is safe, no network is ever attempted by this test.
# --------------------------------------------------------------------------- #

def test_no_narrator_configured_reports_cloud_off():
    with _client() as c:
        row = next(x for x in c.get("/api/transparency").json()["egress"]
                   if x["channel"] == "Cloud AI narrator")
        assert row["on"] is False


def test_local_ollama_narrator_reports_cloud_off(monkeypatch):
    # The honesty test: fully ENABLED narration, but the LOCAL provider -- must
    # still read False here, since narrator.py's own docstring is explicit that
    # Ollama is "ZERO external egress".
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_NARRATE_ENABLED", "1")
    monkeypatch.setenv("WAVR_NARRATE_PROVIDER", "ollama")
    try:
        with _client() as c:
            row = next(x for x in c.get("/api/transparency").json()["egress"]
                       if x["channel"] == "Cloud AI narrator")
            assert row["on"] is False
    finally:
        for v in ("WAVR_NARRATE_ENABLED", "WAVR_NARRATE_PROVIDER"):
            monkeypatch.delenv(v, raising=False)


def test_cloud_narrator_reports_on_once_actually_live(monkeypatch):
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_NARRATE_ENABLED", "1")
    monkeypatch.setenv("WAVR_NARRATE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key-never-used")
    try:
        with _client() as c:
            row = next(x for x in c.get("/api/transparency").json()["egress"]
                       if x["channel"] == "Cloud AI narrator")
            assert row["on"] is True
    finally:
        for v in ("WAVR_NARRATE_ENABLED", "WAVR_NARRATE_PROVIDER", "GEMINI_API_KEY"):
            monkeypatch.delenv(v, raising=False)


def test_cloud_narrator_off_when_connectors_override_revokes_it(monkeypatch):
    # A deliberate Connectors-screen "off" override must revoke even a fully
    # built cloud narrator immediately -- mirrors POST /api/narrate's own FIRST
    # check (narr_ov == "off" short-circuits before anything else).
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_NARRATE_ENABLED", "1")
    monkeypatch.setenv("WAVR_NARRATE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key-never-used")
    store = ConnectorStore(":memory:")
    store.upsert("narrator", "builtin", "LLM Narrator")
    store.set_enabled("narrator", False)   # deliberate kill-switch
    try:
        with _client(connector_store=store) as c:
            row = next(x for x in c.get("/api/transparency").json()["egress"]
                       if x["channel"] == "Cloud AI narrator")
            assert row["on"] is False
    finally:
        for v in ("WAVR_NARRATE_ENABLED", "WAVR_NARRATE_PROVIDER", "GEMINI_API_KEY"):
            monkeypatch.delenv(v, raising=False)


# --------------------------------------------------------------------------- #
# Gate parity with GET /api/state: same scope, so an 'agent'-scope device
# (which /api/state also denies) must be denied here too -- never a looser bar.
# --------------------------------------------------------------------------- #

def test_gated_same_as_api_state_denies_a_non_loopback_peer_without_a_token():
    app = create_app(sources=[], storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    r_state = peer.get("/api/state")
    r_trans = peer.get("/api/transparency")
    # Multidevice is off in this config -> both routes reject a non-loopback peer
    # identically (same middleware gate the module docstring at ~1817 describes).
    assert r_state.status_code == r_trans.status_code
    assert r_trans.status_code in (401, 403)
