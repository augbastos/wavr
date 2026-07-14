"""Build C4: wiring tests for the NEW derived-signal MQTT egress -- Watch's A2
intrusion (per-room + house-level), A4's per-room routine anomaly, and A10's
composed house-status verdict -- published via the EXISTING wavr.rules +
wavr.ha_discovery path (ADR-0005: Wavr stays a signal SOURCE, never an
automation engine). Mirrors test_house_status_wiring.py / test_watch.py's own
style: injected fakes stand in for I/O, the real app.py wiring is exercised.
"""
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import Identity, SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}


class _FakeRogueAlert:
    def __init__(self, ts):
        self.ts = ts

    def to_dict(self):
        return {"ts": self.ts, "mac": "aa:bb:cc:dd:ee:ff", "vendor": "Acme",
                "ip": "192.168.1.50", "device_type": "unknown", "hostname": None,
                "type_confidence": "low", "severity": "note"}


class _FakeInvService:
    def __init__(self, alerts=None):
        self._alerts = alerts or []

    def latest_inventory(self):
        return []

    def recent_alerts(self):
        return self._alerts

    async def start(self):
        return None

    async def stop(self):
        return None


class _FakeOccupancyLog:
    """Minimal double, mirrors test_house_status_wiring.py's own fixture."""

    def __init__(self, verdicts=None):
        self._verdicts = verdicts or {}

    def append_if_changed(self, *a, **k):
        return False

    def is_unusual(self, room, occupied, at=None, weeks=4.0, min_samples=3, threshold=0.5):
        return self._verdicts.get(room, {"unusual": None, "baseline_probability": None,
                                         "samples": 0, "hour": 0})


class _WatchSource:
    """Same shape as test_watch.py's own fixture: 'ana' known-present on casa (BLE),
    sala's camera counts TWO people -> sala holds one unrecognized person. sala
    REPEATS on a ~0.05s cadence (mirrors real per-frame camera publish) so
    wavr.watch.IntrusionAlertLog's consecutive-check debounce gets enough checks to
    arm within the tests' short poll windows."""

    async def events(self):
        now = datetime.now(timezone.utc).isoformat()
        yield SensingEvent(room="casa", modality="ble", presence=True, motion=0.0,
                           breathing_bpm=None, heart_bpm=None, confidence=0.7, ts=now,
                           identities=(Identity("ana", "ble", -50),))
        tgts = tuple(Target(id=i + 1, x=float(i), y=float(i) + 0.5) for i in range(2))
        while True:
            import asyncio
            now2 = datetime.now(timezone.utc).isoformat()
            yield SensingEvent(room="sala", modality="camera", presence=True, motion=1.0,
                               breathing_bpm=13.0, heart_bpm=60.0, confidence=0.95, ts=now2,
                               targets=tgts, count=2)
            await asyncio.sleep(0.05)


def _settle(client, rooms, tries=40):
    for _ in range(tries):
        st = client.get("/api/state").json()
        if all(r in st for r in rooms):
            return st
        time.sleep(0.05)
    return client.get("/api/state").json()


def _wait_until(predicate, tries=40, interval=0.05):
    """Poll `predicate()` instead of a fixed sleep -- the debounced intrusion edge
    needs a couple of the source's repeating ~0.05s ticks to arm."""
    for _ in range(tries):
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _build_watch_app(msgs, notes=None):
    os.environ["WAVR_IDENTITY_ENABLED"] = "1"
    try:
        return create_app(
            sources=[("watchsrc", lambda: _WatchSource(), True)],
            storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
            camera_store=CameraStore(":memory:"), net_inventory=_FakeInvService(),
            rules_publish=lambda t, p, r: msgs.append((t, p, r)),
            notify=(notes.append if notes is not None else None),
        )
    finally:
        os.environ.pop("WAVR_IDENTITY_ENABLED", None)


def test_watch_intrusion_publishes_retained_mqtt_on_then_off():
    msgs = []
    app = _build_watch_app(msgs)
    with TestClient(app) as client:
        _settle(client, ["sala", "casa"])
        client.post("/api/watch", json={"on": True}, headers=LOCAL)
        # wait for the debounce streak (a couple of the source's repeating ~0.05s
        # ticks) to arm both the per-room and house-level edge before checking MQTT.
        assert _wait_until(lambda: any(
            m[0] == "wavr/watch/rooms/sala/intrusion" and m[1] == "ON" for m in msgs))
        assert _wait_until(lambda: any(
            m[0] == "wavr/watch/house/intrusion" and m[1] == "ON" for m in msgs))

        room_on = [m for m in msgs if m[0] == "wavr/watch/rooms/sala/intrusion"]
        house_on = [m for m in msgs if m[0] == "wavr/watch/house/intrusion"]
        assert room_on and room_on[-1][1] == "ON" and room_on[-1][2] is True
        assert house_on and house_on[-1][1] == "ON" and house_on[-1][2] is True

        # a re-evaluation while STILL active must not re-publish (RulesEngine dedup).
        # Scoped to the intrusion topics themselves (not the raw `msgs` total): the
        # background source keeps ticking every ~0.05s to satisfy the debounce (see
        # _WatchSource above), so unrelated room-state republishes can legitimately
        # land in `msgs` in this window -- asserting the whole list stayed the same
        # length was a flaky proxy for the actual invariant under test.
        before_room = len(room_on)
        before_house = len(house_on)
        client.get("/api/watch")   # no state change -- no new fused event either
        assert len([m for m in msgs if m[0] == "wavr/watch/rooms/sala/intrusion"]) == before_room
        assert len([m for m in msgs if m[0] == "wavr/watch/house/intrusion"]) == before_house

        client.post("/api/watch", json={"on": False}, headers=LOCAL)
        room_off = [m for m in msgs if m[0] == "wavr/watch/rooms/sala/intrusion"]
        house_off = [m for m in msgs if m[0] == "wavr/watch/house/intrusion"]
        assert room_off[-1][1] == "OFF"    # cleared explicitly on Watch-off, not left stuck ON
        assert house_off[-1][1] == "OFF"


def test_watch_intrusion_topics_carry_no_geometry_or_identity():
    msgs = []
    app = _build_watch_app(msgs)
    with TestClient(app) as client:
        _settle(client, ["sala", "casa"])
        client.post("/api/watch", json={"on": True}, headers=LOCAL)
        time.sleep(0.2)
    blob = " ".join(f"{t} {p}" for t, p, _ in msgs).lower()
    for word in ("target", "pose", "vital", "position", "guestzeta", "ana"):
        assert word not in blob, f"privacy leak: {word!r} in derived MQTT stream"


def test_publish_derived_mqtt_house_status_and_routine_anomaly():
    now = datetime.now(timezone.utc).isoformat()
    msgs = []
    occ = _FakeOccupancyLog({"sala": {"unusual": True, "baseline_probability": 0.05,
                                      "samples": 10, "hour": 3}})
    app = create_app(
        sources=[("watchsrc", lambda: _WatchSource(), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"),
        net_inventory=_FakeInvService([_FakeRogueAlert(now)]),
        occupancy_log=occ,
        rules_publish=lambda t, p, r: msgs.append((t, p, r)),
    )
    with TestClient(app) as client:
        _settle(client, ["sala", "casa"])
        import asyncio
        asyncio.run(app.state.publish_derived_mqtt())

    anomaly = [m for m in msgs if m[0] == "wavr/rooms/sala/routine_anomaly"]
    assert anomaly and anomaly[-1][1] == "ON" and anomaly[-1][2] is True

    status_msgs = [m for m in msgs if m[0] == "wavr/house/status"]
    assert status_msgs
    import json
    body = json.loads(status_msgs[-1][1])
    assert body["status"] in ("notice", "alert")   # rogue-device alert present
    reasons = {r["kind"] for r in body["reasons"]}
    assert "routine_anomaly" in reasons and "rogue_device" in reasons
    assert status_msgs[-1][2] is True                    # retained


def test_publish_derived_mqtt_dedupes_unchanged_status():
    occ = _FakeOccupancyLog()   # every room "insufficient data" -> never unusual
    msgs = []
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService(), occupancy_log=occ,
                      rules_publish=lambda t, p, r: msgs.append((t, p, r)))
    with TestClient(app):
        import asyncio
        asyncio.run(app.state.publish_derived_mqtt())
        first = len([m for m in msgs if m[0] == "wavr/house/status"])
        asyncio.run(app.state.publish_derived_mqtt())
        second = len([m for m in msgs if m[0] == "wavr/house/status"])
    assert first == 1          # the initial "ok" baseline publishes once
    assert second == 1         # unchanged "ok" on the next tick never re-publishes


def test_publish_derived_mqtt_is_noop_without_rules():
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService())
    with TestClient(app):
        import asyncio
        asyncio.run(app.state.publish_derived_mqtt())   # must not raise
