"""Every observation says which sensor produced it — or honestly says it cannot.

This is D-001, and it is load-bearing rather than cosmetic: reliability profiles
are per SENSOR (two cameras in one house behave differently), and ground-truth
measurement has to attribute each hit and miss to something. An event that is
anonymous within its modality makes both impossible.

The tests that matter here are the ones proving the id reaches the event from a
TRUSTED source — the enrolment row, the camera store — and never from the
device's own payload.
"""
import asyncio

import pytest

from wavr.events import SensingEvent


def _drain(source, n=1, timeout=2.0):
    """Pull `n` events off an async source."""
    async def run():
        out = []
        agen = source.events()
        try:
            for _ in range(n):
                out.append(await asyncio.wait_for(agen.__anext__(), timeout))
        finally:
            await agen.aclose()
        return out
    return asyncio.run(run())


# -- The field itself ---------------------------------------------------------

def test_the_default_is_empty_not_none():
    """Empty string, so every consumer can treat it as text without a guard.
    `None` would make `sensor_id or ""` appear at every call site."""
    ev = SensingEvent(room="sala", modality="network", presence=True, motion=0.0,
                      breathing_bpm=None, heart_bpm=None, confidence=0.5,
                      ts="2026-09-03T12:00:00+00:00")
    assert ev.sensor_id == ""


def test_it_survives_the_round_trip():
    ev = SensingEvent(room="sala", modality="camera", presence=True, motion=0.0,
                      breathing_bpm=None, heart_bpm=None, confidence=0.9,
                      ts="2026-09-03T12:00:00+00:00", sensor_id="hall-cam")
    assert ev.to_dict()["sensor_id"] == "hall-cam"


# -- A node's id comes from its enrolment row, never its payload --------------

def test_a_node_event_is_attributed_to_its_enrolled_id():
    from wavr.nodes import NodeStore, node_event

    store = NodeStore(":memory:")
    try:
        node_id, _ = store.request_join(name_hint="kitchen-radar",
                                        sensor_hint="ld2450")
        store.approve(node_id, "Kitchen radar", "ld2450", "Kitchen", "native")
        node = store.get(node_id)

        ev = node_event(node, {"presence": True, "targets": [{"id": 1, "x": 1.0, "y": 2.0}]})
        assert ev.sensor_id == node_id
    finally:
        store.close()


def test_a_node_cannot_declare_its_own_sensor_id():
    """The anti-spoof rule `nodes.py` already sets, extended to identity.

    A node that puts `sensor_id` in its telemetry must not be able to attribute
    its readings to a different, better-trusted sensor — that would let a cheap
    PIR inherit the reliability a calibrated camera earned.
    """
    from wavr.nodes import NodeStore, node_event

    store = NodeStore(":memory:")
    try:
        node_id, _ = store.request_join(name_hint="honest-pir",
                                        sensor_hint="pir")
        store.approve(node_id, "Hall PIR", "pir", "Hall", "native")
        node = store.get(node_id)

        ev = node_event(node, {
            "presence": True,
            "sensor_id": "living-room-camera",     # the lie
        })
        assert ev.sensor_id == node_id, "the enrolment row wins, always"
    finally:
        store.close()


# -- The wired radar shares its id with the coverage model --------------------

def test_the_wired_radar_uses_the_same_id_sensor_coverage_gives_it():
    """Two surfaces describing one physical sensor must agree on its name.

    If they drift, coverage reports the kitchen radar healthy while reliability
    has never heard of it, and neither surface is wrong on its own terms — which
    is the hardest kind of bug to see.
    """
    from wavr.sensor_coverage import collect_coverage
    from wavr.sources.mmwave import MmWaveSource

    src = MmWaveSource("Kitchen", "COM4")
    coverage = collect_coverage(serial_rooms=("Kitchen",))
    assert [c.sensor_id for c in coverage] == [src._sensor_id]


# -- A camera is attributed by the name the operator gave it ------------------

def test_a_camera_event_carries_the_operator_given_name():
    from wavr.sources.camera import CameraSource, Detection

    async def one_frame(url):
        yield object()

    src = CameraSource("Hall", "rtsp://x/y", name="hall-cam",
                       frames=one_frame,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       interval=0.0)
    events = _drain(src, 1)
    assert events[0].sensor_id == "hall-cam"
    assert events[0].modality == "camera"


def test_an_unnamed_camera_degrades_to_empty_rather_than_guessing():
    """A camera with no name gets no id — not its room, not its URL.

    Borrowing the room would make two cameras in one room indistinguishable, and
    the URL is a credential-bearing string that must never become an identifier.
    """
    from wavr.sources.camera import CameraSource, Detection

    async def one_frame(url):
        yield object()

    src = CameraSource("Hall", "rtsp://admin:secret@x/y", frames=one_frame,
                       detect=lambda f: Detection(count=1, confidence=0.9),
                       interval=0.0)
    ev = _drain(src, 1)[0]
    assert ev.sensor_id == ""
    assert "secret" not in ev.to_dict()["sensor_id"]
