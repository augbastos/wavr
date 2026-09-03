"""The discovery pass, without an application.

The point of extracting it: this file needs four fakes and no FastAPI, no
uvicorn, no lifespan. Before, the only way to exercise the pass was to build a
whole app and wait for a background loop.

What is worth testing here is the ERROR POLICY, because it is the part that
decides whether a broken Core looks broken or looks quiet.
"""
import sqlite3

import pytest

from wavr.discovery_inbox import DiscoveryInbox
from wavr.services.discovery import run_discovery_pass


class _Cores:
    def __init__(self, *, fail=None):
        self.beats = []
        self._fail = fail

    def self_core(self):
        if self._fail == "self":
            raise sqlite3.Error("db locked")
        return type("C", (), {"core_id": "core-a"})()

    def heartbeat(self, core_id):
        if self._fail == "beat":
            raise sqlite3.Error("db locked")
        self.beats.append(core_id)

    def topology(self):
        if self._fail == "topology":
            raise sqlite3.Error("db locked")
        return {"contested": False, "cores": []}


class _Cameras:
    def __init__(self, rows=(), *, fail=False):
        self._rows, self._fail = list(rows), fail

    def list(self):
        if self._fail:
            raise sqlite3.Error("db locked")
        return self._rows


class _Inventory:
    def __init__(self, devices=(), *, fail=False):
        self._devices, self._fail = list(devices), fail

    def latest_inventory(self):
        if self._fail:
            raise OSError("the network went away")
        return self._devices


class _Nodes:
    def __init__(self, pending=(), *, fail=False):
        self._pending, self._fail = list(pending), fail

    def list_pending(self):
        if self._fail:
            raise sqlite3.Error("db locked")
        return self._pending


@pytest.fixture
def inbox():
    i = DiscoveryInbox(":memory:")
    yield i
    i.close()


def _run(inbox, **kw):
    kw.setdefault("cores", _Cores())
    kw.setdefault("cameras", _Cameras())
    kw.setdefault("inventory", _Inventory())
    kw.setdefault("last_seen_ips", {})
    return run_discovery_pass(inbox=inbox, **kw)


# -- What it does -------------------------------------------------------------

def test_the_lease_is_renewed_every_pass(inbox):
    """Without this the Core that is actually running reads as stale after 120s
    — including to itself."""
    cores = _Cores()
    _run(inbox, cores=cores)
    assert cores.beats == ["core-a"]


def test_a_pending_node_becomes_a_card(inbox):
    _run(inbox, nodes=_Nodes([{"node_id": "n1", "name_hint": "kitchen-radar",
                               "sensor_hint": "ld2450"}]))
    kinds = [i.kind for i in inbox.list_items()]
    assert kinds == ["node_pending"]


def test_the_two_core_halves_stay_separate(inbox):
    """Paired-peer polling can change who is authoritative; mDNS discovery may
    only ever raise a card. Keeping them as two callables is what stops a future
    change letting a forgeable advertisement move authority."""
    calls = []
    _run(inbox,
         observe_peers=lambda: calls.append("paired"),
         discover_unpaired=lambda: (calls.append("mdns"), 0)[1])
    assert calls == ["paired", "mdns"]


def test_nothing_is_raised_for_a_camera_already_configured(inbox):
    dev = {"mac": "aa:bb", "ip": "10.0.0.9", "device_type": "camera",
           "type_confidence": 0.9, "vendor": "TP-Link", "known": False}
    # Unconfigured -> offered.
    assert _run(inbox, inventory=_Inventory([dev])) >= 1
    inbox.close()

    fresh = DiscoveryInbox(":memory:")
    try:
        # Already added -> silent. The anti-nag property.
        _run(fresh, inventory=_Inventory([dev]),
             cameras=_Cameras([{"mac": "aa:bb"}]))
        assert [i.kind for i in fresh.list_items()] == []
    finally:
        fresh.close()


# -- The error policy, which is the reason this is its own module -------------

def test_one_broken_store_does_not_stop_the_others(inbox):
    cores = _Cores(fail="beat")
    seen = _run(inbox, cores=cores,
                nodes=_Nodes([{"node_id": "n1", "name_hint": "radar"}]))
    assert seen >= 1, "the node card was still raised"
    assert cores.beats == []


def test_an_unreadable_camera_list_does_not_re_offer_every_camera(inbox):
    """The failure the narrow `except sqlite3.Error` guards against.

    If the camera lookup degraded to an empty set on ANY exception, a typo in
    the method name would make every camera the operator already configured
    reappear in the inbox forever — and it would look like the feature working.
    A storage fault is tolerated; a wiring bug must not be.
    """
    dev = {"mac": "aa:bb", "ip": "10.0.0.9", "device_type": "camera",
           "type_confidence": 0.9, "vendor": "TP-Link", "known": False}
    # A real storage fault: tolerated, the pass continues.
    _run(inbox, inventory=_Inventory([dev]), cameras=_Cameras(fail=True))
    assert [i.kind for i in inbox.list_items()] == ["camera_found"]


def test_a_wrong_method_name_on_the_camera_store_is_not_swallowed(inbox):
    class Typo:
        def list(self):
            raise AttributeError("'CameraStore' object has no attribute 'list_cameras'")

    with pytest.raises(AttributeError):
        _run(inbox, cameras=Typo())


def test_a_network_failure_is_tolerated_because_it_is_expected(inbox):
    """The inventory reaches OFF this box, so it fails in ways SQLite does not.
    That one gets the broad guard, and the pass still completes."""
    seen = _run(inbox, inventory=_Inventory(fail=True),
                nodes=_Nodes([{"node_id": "n1", "name_hint": "radar"}]))
    assert seen >= 1


def test_a_peer_that_will_not_answer_does_not_end_the_pass(inbox):
    def angry():
        raise TimeoutError("peer unreachable")

    seen = _run(inbox, observe_peers=angry,
                nodes=_Nodes([{"node_id": "n1", "name_hint": "radar"}]))
    assert seen >= 1


def test_the_optional_halves_are_genuinely_optional(inbox):
    """A Core with nodes and peers switched off must still run a pass."""
    assert _run(inbox) == 0
