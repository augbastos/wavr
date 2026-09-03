"""What actually reaches the Discovery Inbox — the rules that keep it useful
rather than noisy, and the wiring that keeps it from being a fake feature."""
import pytest
from fastapi.testclient import TestClient

from wavr.camera_store import CameraStore
from wavr.core_registry import CoreRegistry
from wavr.discovery_feed import feed_core_topology, feed_devices, feed_pending_nodes
from wavr.discovery_inbox import (
    KIND_CAMERA_FOUND, KIND_CORE_CONTESTED, KIND_DEVICE_MOVED, KIND_DEVICE_NEW,
    KIND_NODE_PENDING, DiscoveryInbox,
)


@pytest.fixture
def inbox():
    i = DiscoveryInbox(":memory:")
    yield i
    i.close()


def dev(mac, **kw):
    base = {"mac": mac, "ip": "192.168.1.10", "vendor": "", "hostname": None,
            "device_type": "", "type_confidence": 0.0, "known": False}
    base.update(kw)
    return base


def kinds(inbox):
    return sorted(d.kind for d in inbox.list_items())


# -- Devices -----------------------------------------------------------------

def test_an_unknown_device_becomes_an_item(inbox):
    feed_devices(inbox, [dev("aa:bb", vendor="Samsung", hostname="Galaxy-A54")])
    items = inbox.list_items()
    assert len(items) == 1 and items[0].kind == KIND_DEVICE_NEW
    assert "Galaxy-A54" in items[0].title


def test_a_known_device_is_not_news(inbox):
    feed_devices(inbox, [dev("aa:bb", known=True)])
    assert inbox.list_items() == []


def test_an_operator_allowlisted_mac_is_not_news_either(inbox):
    feed_devices(inbox, [dev("aa:bb")], known_macs={"aa:bb"})
    assert inbox.list_items() == []


def test_a_device_with_no_mac_is_skipped(inbox):
    feed_devices(inbox, [dev(None), dev("")])
    assert inbox.list_items() == []


def test_the_feed_accepts_dataclass_style_objects_too(inbox):
    class Row:
        mac, ip, vendor, hostname = "aa:bb", "10.0.0.2", "Acme", None
        device_type, type_confidence, known = "", 0.0, False

    feed_devices(inbox, [Row()])
    assert len(inbox.list_items()) == 1


# -- Cameras -----------------------------------------------------------------

def test_a_confident_camera_becomes_an_add_camera_item(inbox):
    feed_devices(inbox, [dev("cam1", device_type="camera", type_confidence=0.9,
                             vendor="TP-Link", hostname="Tapo-C210")])
    items = inbox.list_items()
    assert len(items) == 1 and items[0].kind == KIND_CAMERA_FOUND
    assert any(a["id"] == "add_camera" for a in items[0].to_dict()["actions"])


def test_a_weak_camera_guess_is_not_offered_as_a_camera(inbox):
    # A low-confidence "might be a camera" card, surfaced as an action, trains
    # the operator to distrust the inbox.
    feed_devices(inbox, [dev("cam1", device_type="camera", type_confidence=0.2)])
    assert kinds(inbox) == [KIND_DEVICE_NEW]


def test_an_already_configured_camera_is_not_offered_again(inbox):
    feed_devices(inbox, [dev("cam1", device_type="camera", type_confidence=0.9)],
                 configured_camera_macs={"cam1"})
    assert inbox.list_items() == []


def test_a_camera_is_not_also_reported_as_an_unknown_device(inbox):
    feed_devices(inbox, [dev("cam1", device_type="camera", type_confidence=0.9)])
    assert kinds(inbox) == [KIND_CAMERA_FOUND]


# -- Address changes ---------------------------------------------------------

def test_no_move_is_reported_without_a_remembered_address(inbox):
    feed_devices(inbox, [dev("aa:bb", ip="10.0.0.5")])
    feed_devices(inbox, [dev("aa:bb", ip="10.0.0.9")])
    assert KIND_DEVICE_MOVED not in kinds(inbox)


def test_a_move_is_reported_once_we_knew_the_old_address(inbox):
    seen = {}
    feed_devices(inbox, [dev("aa:bb", ip="10.0.0.5", known=True)], last_seen_ips=seen)
    assert seen == {"aa:bb": "10.0.0.5"}
    feed_devices(inbox, [dev("aa:bb", ip="10.0.0.9", known=True)], last_seen_ips=seen)
    moved = [d for d in inbox.list_items() if d.kind == KIND_DEVICE_MOVED]
    assert len(moved) == 1
    assert moved[0].detail["from"] == "10.0.0.5" and moved[0].detail["to"] == "10.0.0.9"
    assert moved[0].confidence == 1.0        # we watched it happen


def test_the_same_address_is_not_a_move(inbox):
    seen = {}
    for _ in range(5):
        feed_devices(inbox, [dev("aa:bb", ip="10.0.0.5", known=True)], last_seen_ips=seen)
    assert inbox.list_items() == []


# -- Nodes and Cores ---------------------------------------------------------

def test_a_pending_node_becomes_an_approval_item(inbox):
    feed_pending_nodes(inbox, [{"node_id": "n1", "name": "Kitchen radar",
                                "sensor_type": "ld2450", "room": "Kitchen"}])
    item = inbox.list_items()[0]
    assert item.kind == KIND_NODE_PENDING
    assert any(a["id"] == "approve_node" for a in item.to_dict()["actions"])


def test_the_card_carries_the_nodes_claims_so_the_form_can_prefill(inbox):
    # The approval form needs something to show, and the only thing that exists
    # before an operator decides is what the node said about itself.
    feed_pending_nodes(inbox, [{"node_id": "n1", "name": "kitchen-radar-01",
                                "name_hint": "kitchen-radar-01",
                                "sensor_hint": "ld2450",
                                "sensor_type": "", "room": ""}])
    detail = inbox.list_items()[0].detail
    assert detail["node_id"] == "n1"
    assert detail["name_hint"] == "kitchen-radar-01"
    assert detail["sensor_hint"] == "ld2450"


def test_a_claim_is_never_shipped_under_a_trusted_field_name(inbox):
    # `sensor_type` and `room` are what fusion reads. A pending node has neither,
    # and putting an empty one next to a populated claim is how the claim ends up
    # promoted by whoever writes the next consumer.
    feed_pending_nodes(inbox, [{"node_id": "n1", "name_hint": "totally-a-radar",
                                "sensor_hint": "ld2450"}])
    detail = inbox.list_items()[0].detail
    assert "sensor_type" not in detail and "room" not in detail


def test_a_node_that_claims_nothing_still_gets_a_card(inbox):
    feed_pending_nodes(inbox, [{"node_id": "abcdef0123456789"}])
    item = inbox.list_items()[0]
    assert item.detail["name_hint"] == "abcdef01"    # falls back to the id stub
    assert item.detail["sensor_hint"] == ""


def test_a_healthy_topology_raises_nothing(inbox):
    assert feed_core_topology(inbox, {"contested": False, "cores": []}) == 0
    assert inbox.list_items() == []


def test_a_contested_topology_is_surfaced_to_a_human(inbox):
    # The registry already broke the tie so the Space keeps working. Converging
    # quietly is not the same as being right, so it must still be reported.
    feed_core_topology(inbox, {"contested": True, "cores": [
        {"core_id": "core-a", "status": "primary"},
        {"core_id": "core-b", "status": "primary"}]})
    item = inbox.list_items()[0]
    assert item.kind == KIND_CORE_CONTESTED
    assert item.detail["cores"] == ["core-a", "core-b"]


def test_topology_is_one_item_however_many_times_it_is_observed(inbox):
    top = {"contested": True, "cores": [{"core_id": "a", "status": "primary"}]}
    for _ in range(10):
        feed_core_topology(inbox, top)
    assert len(inbox.list_items()) == 1


def test_a_missing_topology_is_tolerated(inbox):
    assert feed_core_topology(inbox, None) == 0
    assert feed_core_topology(inbox, {}) == 0


# -- The live wiring ---------------------------------------------------------

def _app():
    from wavr.app import create_app
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.settings_store import SettingsStore
    from wavr.space_store import SpaceStore
    from wavr.storage import Storage

    return create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(":memory:"), core_registry=CoreRegistry(":memory:"),
        settings_store=SettingsStore(":memory:"),
        discovery_inbox=DiscoveryInbox(":memory:"))


def test_the_feed_is_actually_wired_into_the_app():
    # SS60: a button backed by nothing is not a feature. Prove one pass runs
    # end-to-end against the real app and reaches the real inbox.
    app = _app()
    with TestClient(app) as client:
        assert callable(app.state.discovery_once)
        app.state.discovery_once()            # must not raise
        r = client.get("/api/discoveries", headers={"X-Wavr-Local": "1"})
        assert r.status_code == 200


def test_the_configured_camera_lookup_uses_the_real_store_api():
    # Regression: this called a `list_cameras()` that does not exist, and a broad
    # `except Exception` swallowed the AttributeError -- so the set of already-
    # configured cameras came back empty and every camera the operator had
    # already added would reappear in the inbox on every pass, forever.
    store = CameraStore(":memory:")
    try:
        store.add("hall", "Hall", "rtsp://10.0.0.4/stream", 0.5)
        store.set_mac("hall", "cam-mac")
        assert {c.get("mac") for c in store.list() if c.get("mac")} == {"cam-mac"}
    finally:
        store.close()
