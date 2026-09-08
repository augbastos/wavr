"""Can a household FIND OUT that something is reaching outside their network?

Wavr's whole pitch is that it is local and mute by default. That claim is only
worth something if the surfaces that answer "is anything switched on?" can
actually say yes. Three of them could not, each for its own reason, and each
one was reassuring while it was wrong:

  * the runtime status (tray, menu bar, Android notification, `wavr status`,
    the shell chip) filtered connectors on a `reach` column that does not
    exist on the connector table at all -- `reach` belongs to PROVIDERS -- so
    the filter compared "" against a tuple containing "" and the egress list
    was ALWAYS empty;
  * the Privacy posture screen counted every enabled REGISTRY ROW, which
    counts inbound connectors and the reserved `sys:` toggle rows, and phrases
    the answer as "there are none enabled";
  * the Trust screen ("what leaves your network") was a hand-written array of
    four channels, so a connector shipped after it -- diagnostics auto-send,
    the Assistant's cloud engine, the daily digest, the enrich lookups --
    could be fully switched on and appear nowhere.

All three now read ONE producer (`_egress_now` in app.py), which applies the
same definition of "leaves your network" the Connectors screen already draws
with in `frontend/js/connectors.js`: an OUTBOUND connector whose scope does
not begin with "local".

The rest of the file covers the containment claims that the same read turned
up: a `guest` writing its own capability manifest, and the FastAPI schema
being readable by principals whose documented surface is one route.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import wavr.app as appmod
from wavr.app import _is_egress_connector, create_app
from wavr.camera_store import CameraStore
from wavr.connector_store import ConnectorStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}


def _client(**kwargs):
    kwargs.setdefault("sources", [])
    kwargs.setdefault("storage", Storage(":memory:"))
    kwargs.setdefault("camera_store", CameraStore(":memory:"))
    return TestClient(create_app(**kwargs), headers=LOCAL)


def _store_with(connector_id: str) -> ConnectorStore:
    """A registry with one generic connector deliberately switched ON.

    `upsert` never flips an already-persisted enable bit, so create_app's own
    startup upserts leave this row enabled -- the same path an operator takes
    through the Connectors screen.
    """
    store = ConnectorStore(":memory:")
    store.upsert(connector_id, "generic", connector_id)
    store.set_enabled(connector_id, True)
    return store


# --------------------------------------------------------------------------- #
# The one definition, on its own
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("desc,expected", [
    ({"direction": "outbound", "scope": "outbound-cloud: gemini"}, True),
    ({"direction": "outbound", "scope": "local, zero egress"}, False),
    ({"direction": "outbound", "scope": "LOCAL HA registry"}, False),
    ({"direction": "inbound", "scope": "outbound-cloud: gemini"}, False),
    ({"direction": "outbound", "scope": None}, True),
    ({"direction": "outbound"}, True),
    ({}, False),
])
def test_the_egress_rule_matches_the_connectors_screen(desc, expected):
    """`frontend/js/connectors.js`: `direction === "outbound" && !/^local/i`.
    A missing scope is NOT a local one -- an unknown reach must read as egress,
    which is the only direction a privacy claim may be wrong in."""
    assert _is_egress_connector(desc) is expected


# --------------------------------------------------------------------------- #
# 1. The runtime status can report egress at all
# --------------------------------------------------------------------------- #

def _egress_finding(body) -> dict | None:
    return next((f for f in body["findings"] if f["key"] == "egress"), None)


def test_runtime_status_names_a_switched_on_egress_connector(monkeypatch):
    """The finding "N connections to the outside is switched on" existed in
    `runtime_status.assess` and no caller could ever produce it: the list was
    built by reading a `reach` key off a store row that has no such column."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    with _client(connector_store=_store_with("telegram")) as c:
        body = c.get("/api/runtime").json()
    found = _egress_finding(body)
    assert found is not None, (
        "no egress finding at all -- the tray, the CLI and the Android "
        f"notification cannot mention egress: {body['findings']}")
    assert "telegram" in (found["detail"] or ""), found


def test_runtime_status_stays_quiet_when_nothing_reaches_out(monkeypatch):
    """The other half: a default install must NOT grow a scary finding."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    with _client() as c:
        body = c.get("/api/runtime").json()
    assert _egress_finding(body) is None, body["findings"]


def test_runtime_status_ignores_a_local_only_connector(monkeypatch):
    """HA import is a LAN read, not egress, and it must never be counted."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_HA_IMPORT", "1")
    monkeypatch.setenv("WAVR_HA_URL", "http://ha.local:8123")
    monkeypatch.setenv("WAVR_HA_TOKEN", "tok")
    try:
        with _client() as c:
            body = c.get("/api/runtime").json()
        assert _egress_finding(body) is None, body["findings"]
    finally:
        for v in ("WAVR_HA_IMPORT", "WAVR_HA_URL", "WAVR_HA_TOKEN"):
            monkeypatch.delenv(v, raising=False)


def test_the_egress_master_switch_silences_the_finding(monkeypatch):
    """Turning the System-tab egress master OFF really does stop the Telegram
    sender (`make_telegram_send` re-reads `egress_allowed()` per call), so the
    status must stop claiming that connection is reaching out."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    store = _store_with("telegram")
    store.upsert("sys:egress", "system", "Egress master")
    store.set_enabled("sys:egress", False)
    with _client(connector_store=store) as c:
        body = c.get("/api/runtime").json()
    assert _egress_finding(body) is None, body["findings"]


# --------------------------------------------------------------------------- #
# 2. The Privacy screen answers from the same producer
# --------------------------------------------------------------------------- #

def test_privacy_posture_counts_the_connector_that_is_reaching_out(monkeypatch):
    """"there are none enabled" was printed with the diagnostics sender, the
    cloud Assistant and Telegram all reachable."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    with _client(connector_store=_store_with("telegram")) as c:
        body = c.get("/api/privacy/posture").json()
    assert body["external_connections"] == 1, body
    assert "none enabled" not in body["note"], body["note"]


def test_privacy_posture_does_not_count_a_system_toggle_row(monkeypatch):
    """The reserved `sys:` rows live in the SAME table. An operator who has
    ever touched a master switch must not be told they have an external
    connection because of it."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    store = ConnectorStore(":memory:")
    store.upsert("sys:sensing", "system", "Sensing master")
    store.set_enabled("sys:sensing", True)
    with _client(connector_store=store) as c:
        body = c.get("/api/privacy/posture").json()
    assert body["external_connections"] == 0, body
    assert "none enabled" in body["note"], body["note"]


def test_privacy_and_runtime_cannot_disagree(monkeypatch):
    """The two screens are the reason the producer is shared: one saying
    "none" while the other names two is the failure this prevents."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    store = _store_with("telegram")
    store.upsert("open-meteo", "generic", "Open-Meteo Weather")
    store.set_enabled("open-meteo", True)
    with _client(connector_store=store) as c:
        posture = c.get("/api/privacy/posture").json()
        found = _egress_finding(c.get("/api/runtime").json())
    assert found is not None
    assert posture["external_connections"] == len(found["detail"].split(", "))
    assert posture["external_connections"] == 2, posture


# --------------------------------------------------------------------------- #
# 3. The Trust screen enumerates the registry, not a hand-written four
# --------------------------------------------------------------------------- #

def test_transparency_shows_a_connector_the_hardcoded_list_never_knew(monkeypatch):
    """The four-row array predates diagnostics auto-send, the daily digest,
    the Assistant's cloud engine and the enrich lookups. Any of them could be
    fully on and the trust screen would still list four channels, all off."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    with _client(connector_store=_store_with("open-meteo")) as c:
        rows = c.get("/api/transparency").json()["egress"]
    channels = {r["channel"]: r["on"] for r in rows}
    assert any(v for v in channels.values()), channels
    assert "Open-Meteo Weather" in channels, channels
    assert channels["Open-Meteo Weather"] is True, channels
    for row in rows:
        assert set(row) == {"channel", "on", "detail"}
        assert isinstance(row["detail"], str) and row["detail"]


def test_transparency_keeps_the_four_channels_a_household_learned(monkeypatch):
    """Deriving the list must not silently drop the rows people already look
    for -- an absent row reads as "not a thing Wavr does", not as "off"."""
    monkeypatch.setenv("WAVR_DB", ":memory:")
    with _client() as c:
        channels = {r["channel"] for r in c.get("/api/transparency").json()["egress"]}
    assert {"Home Assistant control", "Telegram notifications",
            "ntfy notifications", "Cloud AI narrator"} <= channels, channels


# --------------------------------------------------------------------------- #
# 4. Containment: a guest describes nothing but itself
# --------------------------------------------------------------------------- #

async def _fake_mac(host):
    return "aa:bb:cc:dd:ee:01"


@pytest.fixture
def lan(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        companion_resolve_mac=_fake_mac)


def _paired(app, role, host):
    root = TestClient(app)
    code = root.post("/api/pair-code", json={"role": role}, headers=LOCAL).json()["code"]
    peer = TestClient(app, client=(host, 12345))
    body = peer.post("/api/pair", json={"code": code, "device_name": role}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}


def _guest(app, host="192.168.1.51"):
    root = TestClient(app)
    inv = root.post("/api/guest/invite", json={"hours": 4}, headers=LOCAL).json()
    peer = TestClient(app, client=(host, 22222))
    body = peer.post("/api/pair", json={"code": inv["code"], "device_name": "g"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}


def test_a_guest_cannot_write_its_own_capability_manifest(lan):
    """auth.py documents the guest's containment twice: "its ONLY reachable
    surface is the presence:write register-companion route". PUT
    /api/devices/me/manifest carried require_authenticated and no scope, and
    can_view() includes 'guest' -- so a phone let in for the evening could
    write a persistent row that steers what Wavr offers that device."""
    peer, auth = _guest(lan)
    r = peer.put("/api/devices/me/manifest",
                 json={"platform": "android", "capabilities": ["screen"]},
                 headers=auth)
    assert r.status_code == 403, r.text


def test_an_ordinary_companion_still_describes_itself(lan):
    """The tightening must not take the feature with it: a 'user' phone is
    exactly who this route was exposed below admin for."""
    peer, auth = _paired(lan, "user", "192.168.1.50")
    r = peer.put("/api/devices/me/manifest",
                 json={"platform": "android", "capabilities": ["screen"]},
                 headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["platform"] == "android"


# --------------------------------------------------------------------------- #
# 5. Containment: the API schema is not a public map of the house
# --------------------------------------------------------------------------- #

def test_the_schema_is_not_readable_by_an_agent_or_a_guest(lan):
    """`/openapi.json` carried no dependency, so every route, body shape and
    parameter of the admin surface was readable by an 'agent' (documented
    surface: /mcp alone) and by a 'guest' (documented surface: one presence
    route)."""
    for peer, auth in (_guest(lan), _paired(lan, "agent", "192.168.1.60")):
        assert peer.get("/openapi.json", headers=auth).status_code == 403


def test_the_operator_can_still_read_the_schema(lan):
    """Gated, not deleted -- the loopback operator is who it is for."""
    r = TestClient(lan).get("/openapi.json", headers=LOCAL)
    assert r.status_code == 200 and r.json()["info"]["title"] == "Wavr"


def test_the_cdn_backed_doc_uis_are_gone(lan):
    """Swagger UI and ReDoc load their JS and CSS from a public CDN. On a
    product whose claim is that nothing leaves the network, an operator
    opening the built-in docs would make an outbound request the Connectors
    screen never listed."""
    c = TestClient(lan)
    assert c.get("/docs", headers=LOCAL).status_code == 404
    assert c.get("/redoc", headers=LOCAL).status_code == 404


# --------------------------------------------------------------------------- #
# 6. A comment that describes a gate the route does not have
# --------------------------------------------------------------------------- #

def test_the_reference_experience_comment_matches_the_route():
    """The block comment claimed the pages were "Gated on the switch AND on
    `require_local` + `admin`". They are gated on developer mode and a fixed
    name list, and deliberately nothing else -- a top-level browser
    navigation cannot send `X-Wavr-Local`, which is the whole reason the
    header gate was removed. A comment that describes a gate the code does not
    have is how the next reader concludes the surface is safer than it is.
    """
    src = Path(appmod.__file__).read_text(encoding="utf-8")
    block = src[src.index("# -- Reference experiences, developer mode only"):]
    block = block[:block.index("_EXPERIENCE_PAGES = (")]
    assert "require_local" not in block, block
    assert "admin" not in block, block
