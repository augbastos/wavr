"""Node-initiated enrollment, end to end.

    board powers on -> asks to join -> Discovery Inbox -> Admin approves
    -> board collects its credential -> telemetry is accepted

The property that must survive all of it: a board can ask to be let in, but it
cannot decide what it is or where it is. Room, sensor type and transport come
from a human at approval time; everything the board says about itself stays
labelled as a claim.

Structural note: the board and the operator are separate sessions, so each gets
its own app over the SAME on-disk database. Two `TestClient`s on ONE app would
run the lifespan twice and the MCP session manager refuses a second `.run()`.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.camera_store import CameraStore
from wavr.core_registry import CoreRegistry
from wavr.discovery_inbox import KIND_NODE_PENDING, DiscoveryInbox
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.nodes import JOIN_REQUEST_MAX, STATE_ACTIVE, STATE_PENDING, NodeStore
from wavr.settings_store import SettingsStore
from wavr.space_store import SpaceStore
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}
LAN = ("192.168.1.50", 4444)


@pytest.fixture
def core(monkeypatch, tmp_path):
    """`(build, db)`. `build(client=None)` returns a fresh app + TestClient over
    the same persistent database. `WAVR_NODES_ENABLED` requires multidevice."""
    from wavr.app import create_app

    db = str(tmp_path / "wavr.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")
    monkeypatch.setenv("WAVR_DB", db)

    def build(client=None):
        app = create_app(
            sources=[], storage=Storage(":memory:"), hub=Hub(),
            fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
            health_resolvers={}, space_store=SpaceStore(db),
            core_registry=CoreRegistry(db), settings_store=SettingsStore(db),
            discovery_inbox=DiscoveryInbox(db))
        kwargs = {"client": client} if client else {}
        return TestClient(app, **kwargs), app

    return build, db


def _inbox(db, kind=KIND_NODE_PENDING):
    """Read the inbox from outside any app, so an assertion never depends on a
    store some lifespan has already closed."""
    box = DiscoveryInbox(db)
    try:
        return [d for d in box.list_items() if d.kind == kind]
    finally:
        box.close()


def _nodes(db):
    store = NodeStore(db)
    try:
        return store.list()
    finally:
        store.close()


# -- The whole journey --------------------------------------------------------

def test_a_board_asks_to_join_and_an_admin_lets_it_in(core):
    build, db = core

    # 1. The board announces itself. It holds no credential and gets none.
    board, _ = build(client=LAN)
    with board:
        asked = board.post("/api/nodes/request", json={
            "name_hint": "esp32-a4f1", "sensor_hint": "ld2450",
            "cert_fingerprint": "AA:BB:CC:DD"})
        assert asked.status_code == 200
        body = asked.json()
        assert body["status"] == "pending"
        assert "token" not in body, "asking must never mint a credential"
        node_id, request_id = body["node_id"], body["request_id"]

        # Polling before a decision says so, and still hands over nothing.
        assert board.post("/api/nodes/claim", json={"request_id": request_id}
                          ).json() == {"status": "pending", "poll_after_ms": 5000}

    # 2. The operator sees it, with the board's claims labelled as claims.
    admin, _ = build()
    with admin:
        pending = admin.get("/api/nodes/pending", headers=LOCAL).json()["pending"]
        assert len(pending) == 1
        row = pending[0]
        assert row["state"] == STATE_PENDING
        assert row["name_hint"] == "esp32-a4f1" and row["sensor_hint"] == "ld2450"
        assert row["room"] == "", "a board does not get to choose its room"
        assert row["modality"] == "", "nor its modality"

        # 3. A human decides what it actually is.
        approved = admin.post(f"/api/nodes/{node_id}/approve", headers=LOCAL, json={
            "name": "Kitchen radar", "sensor_type": "ld2450", "room": "Kitchen"})
        assert approved.status_code == 200
        # The token is NOT on the admin screen — it goes to the board.
        assert "token" not in approved.json()

    # 4. What landed in the trusted columns is what the HUMAN said.
    node = [n for n in _nodes(db) if n.node_id == node_id][0]
    assert node.state == STATE_ACTIVE
    assert node.name == "Kitchen radar" and node.room == "Kitchen"
    assert node.modality == "mmwave"
    assert node.name_hint == "esp32-a4f1", "the claim is kept, still as a claim"


def test_the_board_collects_its_token_exactly_once(core):
    """Approve and claim on one Core process: the pickup token lives in memory
    (never on disk), so this is the realistic path."""
    build, db = core
    client, app = build()          # loopback: can both approve and, here, claim
    with client:
        body = client.post("/api/nodes/request",
                           json={"name_hint": "esp32", "sensor_hint": "ld2450"}).json()
        node_id, request_id = body["node_id"], body["request_id"]

        client.post(f"/api/nodes/{node_id}/approve", headers=LOCAL, json={
            "name": "Hall radar", "sensor_type": "ld2450", "room": "Hall"})

        got = client.post("/api/nodes/claim", json={"request_id": request_id}).json()
        assert got["status"] == "approved" and got["token"]
        token = got["token"]

        # Replaying the capability gets nothing.
        assert client.post("/api/nodes/claim",
                           json={"request_id": request_id}).status_code == 404

        # And the credential actually works.
        assert client.post("/api/nodes/heartbeat",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"node_id": node_id}).status_code == 200


def test_the_request_appears_in_the_discovery_inbox(core):
    build, db = core
    client, app = build(client=LAN)
    with client:
        client.post("/api/nodes/request", json={"name_hint": "esp32-a4f1",
                                                "sensor_hint": "ld2450"})
        app.state.discovery_once()

    items = _inbox(db)
    assert len(items) == 1
    assert any(a["id"] == "approve_node" for a in items[0].to_dict()["actions"])


def test_denying_clears_the_inbox_item(core):
    build, db = core
    client, app = build()
    with client:
        node_id = client.post("/api/nodes/request",
                              json={"name_hint": "esp32", "sensor_hint": "pir"}
                              ).json()["node_id"]
        app.state.discovery_once()
        assert _inbox(db), "it should be asking before we answer"

        assert client.post(f"/api/nodes/{node_id}/deny",
                           headers=LOCAL).status_code == 200
        assert client.get("/api/nodes/pending",
                          headers=LOCAL).json()["pending"] == []
    assert _inbox(db) == [], "a decided request must stop nagging"


def test_an_approved_node_is_not_re_raised_as_pending(core):
    build, db = core
    client, app = build()
    with client:
        node_id = client.post("/api/nodes/request",
                              json={"name_hint": "esp32", "sensor_hint": "ld2450"}
                              ).json()["node_id"]
        client.post(f"/api/nodes/{node_id}/approve", headers=LOCAL, json={
            "name": "Hall radar", "sensor_type": "ld2450", "room": "Hall"})
        for _ in range(3):
            app.state.discovery_once()
    assert _inbox(db) == []


# -- Bounds -------------------------------------------------------------------

def test_join_requests_are_rate_limited_per_ip(core):
    build, _ = core
    client, _app = build(client=LAN)
    with client:
        for _ in range(JOIN_REQUEST_MAX):
            assert client.post("/api/nodes/request",
                               json={"name_hint": "x"}).status_code == 200
        assert client.post("/api/nodes/request",
                           json={"name_hint": "x"}).status_code == 429


def test_an_off_subnet_board_cannot_even_ask(core):
    build, _ = core
    client, _app = build(client=("8.8.8.8", 1234))
    with client:
        assert client.post("/api/nodes/request",
                           json={"name_hint": "x"}).status_code == 403


def test_approve_and_deny_are_loopback_only(core):
    build, _ = core
    client, _app = build(client=LAN)
    with client:
        node_id = client.post("/api/nodes/request",
                              json={"name_hint": "x"}).json()["node_id"]
        # A LAN device — even one that could authenticate — must not be able to
        # approve a sensor into the house.
        assert client.post(f"/api/nodes/{node_id}/approve",
                           json={"name": "x", "sensor_type": "pir",
                                 "room": "y"}).status_code == 403
        assert client.post(f"/api/nodes/{node_id}/deny").status_code == 403


def test_approving_without_a_room_is_refused(core):
    build, _ = core
    client, _app = build()
    with client:
        node_id = client.post("/api/nodes/request",
                              json={"name_hint": "x"}).json()["node_id"]
        assert client.post(f"/api/nodes/{node_id}/approve", headers=LOCAL, json={
            "name": "Radar", "sensor_type": "ld2450",
            "room": "   "}).status_code == 422


def test_claiming_an_unknown_request_is_a_404(core):
    build, _ = core
    client, _app = build(client=LAN)
    with client:
        assert client.post("/api/nodes/claim",
                           json={"request_id": "nope"}).status_code == 404


def test_the_pending_list_is_capped():
    from wavr.nodes import MAX_PENDING_NODES

    store = NodeStore(":memory:")
    try:
        for i in range(MAX_PENDING_NODES + 10):
            store.request_join(f"board-{i}", "pir")
        assert len(store.list_pending()) <= MAX_PENDING_NODES
    finally:
        store.close()


def test_an_uncollected_token_returns_the_node_to_the_queue():
    """The token lives only in memory, so a Core restart loses it. The node must
    end up back in the approval queue rather than stranded holding nothing while
    the Core believes it is active."""
    store = NodeStore(":memory:")
    try:
        node_id, request_id = store.request_join("board", "pir")
        store.approve(node_id, "Hall PIR", "pir", "Hall")
        store._issued.clear()                      # noqa: SLF001 -- simulate a restart
        status, _, token = store.claim(request_id)
        assert status == "pending" and token == ""
        assert [n.node_id for n in store.list_pending()] == [node_id]
    finally:
        store.close()


def test_the_operator_minted_code_path_still_produces_no_pending_row():
    """The original, stronger flow is untouched: a node enrolled with a code the
    operator minted on a trusted screen is active immediately and never appears
    as a join request."""
    from wavr.nodes import NodeEnroller

    store = NodeStore(":memory:")
    try:
        enroller = NodeEnroller(store)
        code = enroller.mint_code("Kitchen radar", "ld2450", "Kitchen", "native")
        node_id, token = enroller.redeem(code, "AA:BB")
        assert token and store.list_pending() == []
        node = [n for n in store.list() if n.node_id == node_id][0]
        assert node.state == STATE_ACTIVE and node.room == "Kitchen"
    finally:
        store.close()
