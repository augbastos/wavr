"""App-level wiring for Build A10's house-status composer (wavr.house_status):
GET /api/house-status through the real create_app, fusing the NETWORK layer
(same merge_alerts() GET /api/alerts uses -- an injected net_inventory fake)
with the PHYSICAL layer (Watch's A2 active-intrusion rooms via the real
create_app + fusion pipeline, and A4's occupancy-routine-anomaly via an injected
occupancy_log fake). Mirrors test_dhcp_monitor_wiring.py / test_watch.py's own
style: injected fakes stand in for I/O, the real wiring/route is exercised.
"""
import asyncio
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import Identity, SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}


class _FakeRogueAlert:
    def __init__(self, ts, severity="note"):
        self.ts = ts
        self.severity = severity

    def to_dict(self):
        return {"ts": self.ts, "mac": "aa:bb:cc:dd:ee:ff", "vendor": "Acme",
                "ip": "192.168.1.50", "device_type": "unknown", "hostname": None,
                "type_confidence": "low", "severity": self.severity}


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
    """Minimal double: `append_if_changed` no-ops (keeps _publish's A4 hook happy
    without a real sqlite table); `is_unusual` returns a caller-controlled verdict
    per room -- avoids needing weeks of real routine-baseline data just to
    exercise the /api/house-status wiring (the routine MATH itself is covered by
    test_occupancy_log.py)."""

    def __init__(self, verdicts=None):
        self._verdicts = verdicts or {}

    def append_if_changed(self, *a, **k):
        return False

    def is_unusual(self, room, occupied, at=None, weeks=4.0, min_samples=3, threshold=0.5):
        return self._verdicts.get(room, {"unusual": None, "baseline_probability": None,
                                         "samples": 0, "hour": 0})


class _CountingOccupancyLog(_FakeOccupancyLog):
    """Same double as `_FakeOccupancyLog`, but counts `is_unusual` calls -- lets a
    test prove the PERF TTL cache (app.py's `_HOUSE_STATUS_ROUTINE_TTL_S`/
    `_routine_cache`) actually amortizes the sweep, not just returns the right
    verdict once."""

    def __init__(self, verdicts=None):
        super().__init__(verdicts)
        self.calls = 0

    def is_unusual(self, room, occupied, at=None, weeks=4.0, min_samples=3, threshold=0.5):
        self.calls += 1
        return super().is_unusual(room, occupied, at=at, weeks=weeks,
                                  min_samples=min_samples, threshold=threshold)


class _OneRoomSource:
    """Publishes exactly one occupied 'sala' reading, no identities/targets --
    enough for A4's per-room is_unusual() sweep to have a room to check."""

    async def events(self):
        now = datetime.now(timezone.utc).isoformat()
        yield SensingEvent(room="sala", modality="camera", presence=True, motion=0.5,
                           breathing_bpm=None, heart_bpm=None, confidence=0.9, ts=now)
        while True:
            await asyncio.sleep(0.05)


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


def test_house_status_ok_when_nothing_wired():
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService())
    with TestClient(app) as client:
        body = client.get("/api/house-status").json()
    assert body["status"] == "ok" and body["score"] == 0 and body["reasons"] == []


def test_house_status_surfaces_recent_network_alert_same_as_api_alerts():
    now = datetime.now(timezone.utc).isoformat()
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService([_FakeRogueAlert(now)]))
    with TestClient(app) as client:
        alerts = client.get("/api/alerts").json()["alerts"]
        body = client.get("/api/house-status").json()
    assert alerts and alerts[0]["kind"] == "rogue_device"      # sanity: same source
    reasons = [r for r in body["reasons"] if r["kind"] == "rogue_device"]
    assert reasons and reasons[0]["layer"] == "network"
    assert body["status"] == "notice"                          # note-tier rogue_device


def test_house_status_omits_stale_network_alert():
    stale = "2020-01-01T00:00:00+00:00"
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService([_FakeRogueAlert(stale)]))
    with TestClient(app) as client:
        body = client.get("/api/house-status").json()
    assert body["status"] == "ok" and body["reasons"] == []


def test_house_status_surfaces_routine_anomaly():
    occ = _FakeOccupancyLog({"sala": {"unusual": True, "baseline_probability": 0.05,
                                      "samples": 10, "hour": 3}})
    app = create_app(sources=[("occsrc", lambda: _OneRoomSource(), True)],
                      storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                      camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService(), occupancy_log=occ)
    with TestClient(app) as client:
        _settle(client, ["sala"])
        body = client.get("/api/house-status").json()
    reasons = [r for r in body["reasons"] if r["kind"] == "routine_anomaly"]
    assert reasons and reasons[0]["layer"] == "physical" and reasons[0]["what"] == "sala occupancy is unusual for this hour"
    assert body["status"] == "notice"


def test_house_status_omits_normal_or_insufficient_data_occupancy():
    occ = _FakeOccupancyLog({"sala": {"unusual": False, "baseline_probability": 0.8,
                                      "samples": 10, "hour": 3}})
    app = create_app(sources=[("occsrc", lambda: _OneRoomSource(), True)],
                      storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                      camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService(), occupancy_log=occ)
    with TestClient(app) as client:
        _settle(client, ["sala"])
        body = client.get("/api/house-status").json()
    assert body["status"] == "ok" and body["reasons"] == []


def test_house_status_surfaces_active_intrusion_and_clears_when_watch_turned_off(monkeypatch):
    monkeypatch.setenv("WAVR_IDENTITY_ENABLED", "1")
    try:
        app = create_app(sources=[("watchsrc", lambda: _WatchSource(), True)],
                          storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                          camera_store=CameraStore(":memory:"), net_inventory=_FakeInvService())
        with TestClient(app) as client:
            _settle(client, ["sala", "casa"])
            client.post("/api/watch", json={"on": True}, headers=LOCAL)
            # wait for the debounce streak (a couple of the source's repeating
            # ~0.05s ticks) to arm the edge-triggered intrusion alert.
            assert _wait_until(lambda: any(
                r["kind"] == "intrusion"
                for r in client.get("/api/house-status").json()["reasons"]))
            body = client.get("/api/house-status").json()
            intr = [r for r in body["reasons"] if r["kind"] == "intrusion"]
            # sala (2 > known 1) AND the house-level aggregate (house total 2 > known
            # 1) both cross the debounce threshold on the same repeating-source tick,
            # so their `ts` ties and `reasons.sort()`'s tie-break order is incidental
            # -- assert the per-room reason is PRESENT rather than assuming its index.
            sala_reason = next((r for r in intr if r["what"] == "unrecognized person in sala"), None)
            assert sala_reason is not None and sala_reason["layer"] == "physical"
            assert body["status"] == "alert" and body["score"] == 4

            # turning Watch back off resets the edge state -- a resolved intrusion
            # must not keep pinning the LIVE composite at alert.
            client.post("/api/watch", json={"on": False}, headers=LOCAL)
            body2 = client.get("/api/house-status").json()
            assert not [r for r in body2["reasons"] if r["kind"] == "intrusion"]
    finally:
        monkeypatch.delenv("WAVR_IDENTITY_ENABLED", raising=False)


def test_house_status_scope_gated_like_api_state():
    app = create_app(sources=[], camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService())
    with TestClient(app) as client:
        assert client.get("/api/house-status").status_code == 200


# ---------------------------------------------------------------------------
# PERF: the routine/is_unusual sweep is amortized over a short TTL
# (app.py's `_HOUSE_STATUS_ROUTINE_TTL_S`) -- was previously completely
# untested; a call-count spy on `is_unusual` is the only way to actually prove
# the sweep is being skipped rather than just re-returning the same verdict.
# ---------------------------------------------------------------------------

def test_house_status_routine_sweep_is_cached_within_ttl():
    occ = _CountingOccupancyLog({"sala": {"unusual": True, "baseline_probability": 0.05,
                                          "samples": 10, "hour": 3}})
    app = create_app(sources=[("occsrc", lambda: _OneRoomSource(), True)],
                      storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                      camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService(), occupancy_log=occ)
    with TestClient(app) as client:
        _settle(client, ["sala"])
        first = client.get("/api/house-status").json()
        calls_after_first = occ.calls
        assert calls_after_first >= 1                       # the sweep DID run once
        second = client.get("/api/house-status").json()
    assert occ.calls == calls_after_first                    # 2nd call within TTL: no re-sweep
    reasons1 = [r for r in first["reasons"] if r["kind"] == "routine_anomaly"]
    reasons2 = [r for r in second["reasons"] if r["kind"] == "routine_anomaly"]
    assert reasons1 and reasons2                              # cached verdict still surfaced


def test_house_status_routine_sweep_recomputes_after_ttl_expires(monkeypatch):
    import wavr.app as app_module
    monkeypatch.setattr(app_module, "_HOUSE_STATUS_ROUTINE_TTL_S", 0.05)
    occ = _CountingOccupancyLog({"sala": {"unusual": True, "baseline_probability": 0.05,
                                          "samples": 10, "hour": 3}})
    app = create_app(sources=[("occsrc", lambda: _OneRoomSource(), True)],
                      storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
                      camera_store=CameraStore(":memory:"),
                      net_inventory=_FakeInvService(), occupancy_log=occ)
    with TestClient(app) as client:
        _settle(client, ["sala"])
        client.get("/api/house-status")
        calls_after_first = occ.calls
        time.sleep(0.1)                                       # past the (patched) TTL
        client.get("/api/house-status")
    assert occ.calls > calls_after_first                      # genuinely re-swept, not stuck stale
