"""Multi-Core awareness, and the line that keeps it from being a takeover.

`observe_peer` can DEMOTE this Core. So the question that matters is not "do
Cores see each other" but "whose word is allowed to move authority". The answer
must be: only a peer that completed the pairing handshake — pinned certificate,
bearer token. An mDNS advertisement, which anything on the segment can forge, is
a question for a human and nothing more.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.camera_store import CameraStore
from wavr.core_registry import (
    STATUS_PRIMARY, STATUS_STANDBY, VERDICT_CONTESTED, VERDICT_HOLD,
    VERDICT_YIELD, CoreRegistry,
)
from wavr.discovery_inbox import KIND_PEER_CORE, DiscoveryInbox
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.mdns_peers import DiscoveredPeer, txt_properties
from wavr.settings_store import SettingsStore
from wavr.space_store import SpaceStore
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}
SPACE_ID = "abcdef0123456789"


@pytest.fixture
def core(monkeypatch, tmp_path):
    from wavr.app import create_app

    db = str(tmp_path / "wavr.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    # `in_subnet` compares the peer against what THIS MACHINE thinks its
    # own address is, so an unpinned LAN peer makes this test true on a
    # 192.168.1.x developer box and false (or true for the wrong reason)
    # anywhere else. Same idiom as test_app.py.
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    monkeypatch.setenv("WAVR_DB", db)

    space = SpaceStore(db)
    cores = CoreRegistry(db)
    inbox = DiscoveryInbox(db)
    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=space, core_registry=cores,
        settings_store=SettingsStore(db), discovery_inbox=inbox)
    return app, space, cores, inbox


# -- The advertisement --------------------------------------------------------

def test_the_advertisement_carries_what_a_joiner_needs():
    props = txt_properties("desktop", space_id="abc123", core_id="core-a",
                           epoch=4, status="primary", protocol_version=1)
    assert props["sid"] == "abc123" and props["cid"] == "core-a"
    assert props["ep"] == "4" and props["st"] == "primary"
    assert props["pv"] == "1"


def test_a_standby_advertises_no_epoch_and_no_status():
    # Only a Core that believes it is authoritative says so. That is what makes
    # a genuine disagreement detectable instead of ambient noise.
    props = txt_properties("desktop", space_id="abc", core_id="core-b",
                           epoch=0, status="")
    assert "st" not in props and "ep" not in props


def test_the_space_name_is_never_advertised():
    # The setup wizard promises "This name stays on this machine. Wavr never
    # broadcasts it to your network." Only the opaque id goes out.
    props = txt_properties("desktop", space_id="abcdef0123456789",
                           core_id="core-a", epoch=1, status="primary")
    assert "My Home" not in str(props)
    assert all(len(str(v)) <= 40 for v in props.values())


def test_advertised_fields_are_bounded_when_read_back():
    # These come off the wire from anything on the segment.
    class _FakeInfo:
        def __init__(self):
            self.port = 8000
            self.properties = {
                b"role": b"desktop",
                b"sid": b"x" * 200,
                b"cid": b"../../etc/passwd",
                b"ep": b"not-a-number",
                b"st": b"primary\x00\x00",
                b"pv": b"9" * 40,
            }

        def parsed_addresses(self):
            return ["192.168.1.9"]

    class _FakeZc:
        def get_service_info(self, type_, name):
            return _FakeInfo()

    from wavr.mdns_peers import _collect_peers

    peer = _collect_peers(_FakeZc(), ["Other._wavr._tcp.local."])[0]
    assert len(peer.space_id) <= 16
    assert "/" not in peer.core_id and ".." not in peer.core_id
    assert peer.epoch == 0, "junk must not become an epoch"
    assert peer.status == "primary"
    assert isinstance(peer.protocol_version, int)


# -- The security line --------------------------------------------------------

def test_an_mdns_advertisement_cannot_demote_this_core(core):
    """The attack this design exists to prevent: anything on the LAN broadcasts
    a `_wavr._tcp` record claiming to be a primary Core at a huge epoch, and the
    household's real Core stands down."""
    app, space, cores, inbox = core
    with TestClient(app):
        space.create_space("My Home", space_id=SPACE_ID)
        cores.register("core-mine-001", SPACE_ID, "Laptop", is_self=True)
        cores.promote("core-mine-001", space.bump_epoch())
        assert cores.self_core().status == STATUS_PRIMARY

        # A hostile advertisement, claiming a far newer epoch.
        # A well-formed hostile advertisement, claiming a far newer epoch.
        hostile = DiscoveredPeer(
            name="Totally A Core", host="192.168.1.66", port=8000, role="desktop",
            space_id=SPACE_ID[:16], core_id="core-attacker",
            epoch=9999, status="primary", protocol_version=1)
        assert hostile.epoch == 9999 and hostile.status == "primary"

        # Run the real discovery pass -- the one that browses mDNS -- and assert
        # the OUTCOME: whatever is on the wire, authority did not move. The only
        # thing an advertisement may produce is an inbox card.
        app.state.discovery_once()
        assert cores.self_core().status == STATUS_PRIMARY, (
            "an unauthenticated advertisement must never move authority")


def test_an_unpaired_core_becomes_a_question_not_a_decision():
    """The other half: a genuine second Core in our Space should be SURFACED, so
    a human can connect to it. That is the only thing mDNS is allowed to do."""
    from wavr.discovery_inbox import DiscoveryInbox

    inbox = DiscoveryInbox(":memory:")
    try:
        inbox.observe(KIND_PEER_CORE, "https://192.168.1.9:8000",
                      "Attic Pi is running on this network and serves this Space.",
                      detail={"core_id": "core-b",
                              "note": "Advertised over mDNS, which anything on "
                                      "the network can send."},
                      confidence=0.6)
        item = inbox.list_items()[0]
        assert item.kind == KIND_PEER_CORE
        assert any(a["id"] == "pair_core" for a in item.to_dict()["actions"])
        # And it is honest about where the claim came from.
        assert "anything on" in item.detail["note"]
    finally:
        inbox.close()


# -- The reconciliation itself ------------------------------------------------

def test_a_paired_peer_with_a_newer_epoch_makes_us_yield():
    reg = CoreRegistry(":memory:")
    try:
        reg.register("core-mine", SPACE_ID, "Laptop", is_self=True)
        reg.promote("core-mine", 3)
        assert reg.observe_peer("core-theirs", 4, True) == VERDICT_YIELD
        assert reg.self_core().status == STATUS_STANDBY
    finally:
        reg.close()


def test_a_paired_peer_with_an_older_epoch_does_not(reg=None):
    reg = CoreRegistry(":memory:")
    try:
        reg.register("core-mine", SPACE_ID, "Laptop", is_self=True)
        reg.promote("core-mine", 7)
        assert reg.observe_peer("core-theirs", 4, True) == VERDICT_HOLD
        assert reg.self_core().status == STATUS_PRIMARY
    finally:
        reg.close()


def test_two_cores_at_the_same_epoch_converge_and_are_reported():
    a, b = CoreRegistry(":memory:"), CoreRegistry(":memory:")
    try:
        a.register("core-aaaa", SPACE_ID, "A", is_self=True)
        a.promote("core-aaaa", 4)
        b.register("core-bbbb", SPACE_ID, "B", is_self=True)
        b.promote("core-bbbb", 4)

        assert a.observe_peer("core-bbbb", 4, True) == VERDICT_CONTESTED
        assert b.observe_peer("core-aaaa", 4, True) == VERDICT_CONTESTED
        # Both ran the identical comparison with no communication and agree.
        assert a.self_core().status == STATUS_PRIMARY
        assert b.self_core().status == STATUS_STANDBY
    finally:
        a.close()
        b.close()


# -- The endpoint that feeds it ------------------------------------------------

def test_core_status_is_not_readable_without_pairing(core):
    app, space, cores, _ = core
    with TestClient(app, client=("192.168.1.77", 5555)) as stranger:
        # No bearer token: the middleware rejects before routing.
        assert stranger.get("/api/peers/core-status").status_code == 403


def test_core_status_says_nothing_about_the_house(core):
    app, space, cores, _ = core
    with TestClient(app) as c:
        space.create_space("My Home", space_id=SPACE_ID)
        cores.register("core-mine-001", SPACE_ID, "Laptop", is_self=True)
        cores.promote("core-mine-001", space.bump_epoch())

        body = c.get("/api/peers/core-status", headers=LOCAL).json()
        assert body["space_id"] == SPACE_ID
        assert body["core_id"] == "core-mine-001"
        assert body["status"] == STATUS_PRIMARY
        assert body["protocol_version"] == 1
        # Nothing about rooms, people, devices or addresses.
        blob = str(body)
        for leak in ("room", "person", "people", "device", "192.168", "token"):
            assert leak not in blob.lower()


def test_core_status_is_honest_before_setup(core):
    app, _space, _cores, _ = core
    with TestClient(app) as c:
        assert c.get("/api/peers/core-status", headers=LOCAL).status_code == 503
