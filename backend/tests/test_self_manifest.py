"""A device describes itself — and can describe nothing else.

The Capability Manifest had a writer only an admin could reach, so in every real
install the column stayed NULL and both the Devices screen and the
`get_device_context` MCP tool were structurally incapable of showing anything.
This is the path a companion actually uses.
"""
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from wavr.api_space import build_self_manifest_router
from wavr.devices import DeviceStore
from wavr.space_store import SpaceStore


@pytest.fixture
def scene(tmp_path):
    db = str(tmp_path / "wavr.db")
    devices = DeviceStore(db)
    space = SpaceStore(db)
    space.create_space("My Home")
    yield devices, space
    devices.close()
    space.close()


def _client(devices, space, gated=True):
    app = FastAPI()
    app.include_router(build_self_manifest_router(
        devices=devices, space_store=space,
        deps=[Depends(lambda: None)] if gated else None))
    return TestClient(app)


def _pair(devices, name="phone", role="user"):
    """`DeviceStore.add` returns (device_id, token) — the token exactly once."""
    device_id, token = devices.add(name=name, role=role)
    return SimpleNamespace(device_id=device_id, token=token)


MANIFEST = {"platform": "android", "capabilities": {"camera": True, "ble": True},
            "functions_supported": ["client", "node"], "protocol_version": 1}


def test_a_device_describes_itself(scene):
    devices, space = scene
    dev = _pair(devices)
    with _client(devices, space) as c:
        r = c.put("/api/devices/me/manifest", json=MANIFEST,
                  headers={"Authorization": f"Bearer {dev.token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["device_id"] == dev.device_id
        assert body["manifest"]["platform"] == "android"
        # The point of storing it: Wavr can now suggest what this device could be.
        assert "node" in body["recommendation"]["functions"]

    stored = space.get_manifest(dev.device_id)
    assert stored is not None and stored.capability("camera") is True


def test_the_id_comes_from_the_credential_not_the_url(scene):
    """Why this route can be exposed below admin at all.

    There is no device id in the path, so there is no way to aim it at anyone
    else's row. A second device's manifest is untouched no matter what the first
    one sends.
    """
    devices, space = scene
    mine, theirs = _pair(devices, "mine"), _pair(devices, "theirs")
    with _client(devices, space) as c:
        c.put("/api/devices/me/manifest", json=MANIFEST,
              headers={"Authorization": f"Bearer {mine.token}"})
    assert space.get_manifest(mine.device_id) is not None
    assert space.get_manifest(theirs.device_id) is None


def test_an_unknown_capability_stays_unknown_never_false(scene):
    """The honesty rule survives the round trip.

    A browser that cannot tell whether the phone has Bluetooth omits the key.
    Storing that as `false` would turn "I could not check" into "it does not have
    one" — a confident wrong answer about someone's hardware.
    """
    devices, space = scene
    dev = _pair(devices)
    with _client(devices, space) as c:
        c.put("/api/devices/me/manifest",
              json={"platform": "ios", "capabilities": {"camera": True}},
              headers={"Authorization": f"Bearer {dev.token}"})
    m = space.get_manifest(dev.device_id)
    assert m.capability("camera") is True
    assert m.capability("ble") is None, "absent must read as unknown"
    assert m.capability("mmwave") is None


def test_a_manifest_grants_no_authority(scene):
    """A device claiming to be a Core gains nothing by saying so."""
    devices, space = scene
    dev = _pair(devices, role="user")
    with _client(devices, space) as c:
        c.put("/api/devices/me/manifest",
              json={"platform": "android", "capabilities": {},
                    "functions_supported": ["core", "client"]},
              headers={"Authorization": f"Bearer {dev.token}"})
    # The claim is stored as a claim. The device's ASSIGNED functions are
    # untouched, and its auth role is still whatever it was issued.
    assert space.get_functions(dev.device_id) is None
    assert devices.get(dev.device_id).role == "user"


def test_a_bad_token_writes_nothing(scene):
    devices, space = scene
    with _client(devices, space) as c:
        r = c.put("/api/devices/me/manifest", json=MANIFEST,
                  headers={"Authorization": "Bearer nope"})
        assert r.status_code == 400


def test_loopback_root_has_no_row_to_describe(scene):
    """Root is not a paired device. The Core's own capabilities come from
    `scan_host()` at setup, so this refuses rather than inventing an id."""
    devices, space = scene
    with _client(devices, space) as c:
        assert c.put("/api/devices/me/manifest", json=MANIFEST).status_code == 400


def test_junk_capabilities_are_discarded_not_believed(scene):
    """A malformed claim degrades to "unknown", which is the safe direction.

    The parser deliberately does not 422 here: a device that sent nonsense in one
    field still has a platform worth recording, and refusing the whole manifest
    would leave the row NULL — indistinguishable from a device that never
    reported. What must never happen is the junk being stored or believed.
    """
    devices, space = scene
    dev = _pair(devices)
    with _client(devices, space) as c:
        r = c.put("/api/devices/me/manifest",
                  json={"platform": "android", "capabilities": "not-an-object",
                        "functions_supported": ["client", "wizard-king"]},
                  headers={"Authorization": f"Bearer {dev.token}"})
        assert r.status_code == 200
    m = space.get_manifest(dev.device_id)
    assert m.capabilities == {}, "nothing junk was kept"
    assert m.capability("camera") is None
    assert m.functions_supported == ("client",), "an invented function is dropped"


def test_a_manifest_that_is_not_an_object_is_refused(scene):
    devices, space = scene
    dev = _pair(devices)
    with _client(devices, space) as c:
        r = c.put("/api/devices/me/manifest", json=["not", "an", "object"],
                  headers={"Authorization": f"Bearer {dev.token}"})
        assert r.status_code == 422
    assert space.get_manifest(dev.device_id) is None


def test_the_route_fails_closed_with_no_gate_wired(scene):
    devices, space = scene
    dev = _pair(devices)
    with _client(devices, space, gated=False) as c:
        r = c.put("/api/devices/me/manifest", json=MANIFEST,
                  headers={"Authorization": f"Bearer {dev.token}"})
        assert r.status_code == 403
