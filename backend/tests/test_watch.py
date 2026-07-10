"""Watch/Guard mode ("Vigia") -- unit + egress-suppression + intrusion tests.

Covers the pure math in wavr.watch, the /api/alerts merge, the MCP strip, and the
end-to-end egress inversion (targets/identities/vitals never leave /api/state while
Watch is on, while counts + the intrusion room do).
"""
import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.watch import (WatchMode, IntrusionAlertLog, known_present_persons,
                        project_state, room_unrecognized, SUPPRESSED_FIELDS)


FULL = {
    "room": "sala", "occupied": True, "confidence": 0.82, "person_count": 2,
    "targets": [{"id": 1, "x": 1.0, "y": 2.0}, {"id": 2, "x": 3.0, "y": 0.5}],
    "identities": [{"person": "guestZeta", "source": "ble", "rssi": -50}],
    "vitals": {"breathing_bpm": 14.0, "heart_bpm": 61.0},
    "sources": [{"modality": "camera", "presence": True, "count": 2, "health": "fresh"}],
    "explanation": "camera: presente", "ts": "2026-07-10T00:00:00+00:00",
}


def test_watchmode_defaults_off_and_toggles():
    w = WatchMode()
    assert w.on is False           # privacy-first boot
    assert w.set(True) is True and w.on is True
    assert w.set(0) is False and w.on is False


def test_project_off_is_identity_map():
    # Watch OFF -> the exact same object, so Off/Presence/Precise are byte-identical.
    assert project_state(FULL, False) is FULL


def test_project_on_strips_geometry_keeps_counts_and_does_not_mutate():
    out = project_state(FULL, True, unrecognized=True)
    for f in SUPPRESSED_FIELDS:
        assert out[f] in ([], {}), (f, out[f])
    # counts + derived occupancy survive -- those are what Watch may surface
    assert out["person_count"] == 2
    assert out["occupied"] is True and out["confidence"] == 0.82
    assert out["watch"] is True and out["unrecognized"] is True
    # the per-source count is a number, not geometry -- allowed to stay
    assert out["sources"][0]["count"] == 2
    # original internal truth is untouched (latest must stay full)
    assert FULL["targets"] and FULL["identities"] and FULL["vitals"]


def test_project_on_leaks_no_coordinate_or_person_label():
    blob = repr(project_state(FULL, True, unrecognized=True))
    # no x/y coordinate value and no person label may appear anywhere in the payload
    assert "guestZeta" not in blob
    assert "breathing_bpm" not in blob and "heart_bpm" not in blob
    assert '"x"' not in blob and '"y"' not in blob


def test_known_present_persons_dedups_across_rooms():
    casa = {"identities": [{"person": "ana"}, {"person": "bea"}, {"person": "ana"}]}
    sala = {"identities": [{"person": "bea"}]}
    assert known_present_persons([casa, sala]) == {"ana", "bea"}
    # identity layer off -> no identities anywhere -> empty known set
    assert known_present_persons([{"identities": []}, {"room": "x"}]) == set()


def test_room_unrecognized_is_honest():
    assert room_unrecognized({"person_count": 2}, 1) is True     # surplus -> unknown
    assert room_unrecognized({"person_count": 2}, 2) is False    # all accounted for
    assert room_unrecognized({"person_count": 1}, 3) is False
    assert room_unrecognized({"person_count": None}, 0) is False  # no count -> never fires
    assert room_unrecognized({}, 0) is False
    assert room_unrecognized({"person_count": 1}, -5) is True     # negative known clamps to 0


def test_intrusion_log_edge_triggers_rearms_and_carries_no_geometry():
    log = IntrusionAlertLog(now_fn=lambda: "T")
    a1 = log.record("sala", True, 2, 1)
    assert a1 is not None
    d = a1.to_dict()
    assert d["kind"] == "intrusion" and d["severity"] == "alert" and d["room"] == "sala"
    assert d["person_count"] == 2 and d["known_present"] == 1
    assert set(d) == {"kind", "severity", "room", "person_count", "known_present", "ts"}
    # edge-triggered: still flagged -> no re-fire
    assert log.record("sala", True, 2, 1) is None
    # clears, then re-arms -> a later intrusion fires again
    assert log.record("sala", False, 0, 1) is None
    a2 = log.record("sala", True, 3, 1)
    assert a2 is not None
    assert len(log.recent_alerts()) == 2
    assert log.active_rooms() == {"sala"}


def test_intrusion_log_ring_is_bounded():
    log = IntrusionAlertLog(max_alerts=3, now_fn=lambda: "T")
    for i in range(10):
        log.record("r%d" % i, True, 2, 0)   # each new room fires once
    assert len(log.recent_alerts()) == 3


# --------------------------------------------------------------------------- #
# /api/alerts merge (router unit): intrusion rides the SAME stream/shape as the
# rogue-device / rogue-DHCP / gateway-identity monitors, one severity ladder.
# --------------------------------------------------------------------------- #

class _FakeInvService:
    def latest_inventory(self):
        return []

    def recent_alerts(self):
        return []


def test_alerts_stream_merges_intrusion():
    from wavr.api_inventory import build_inventory_router
    log = IntrusionAlertLog(now_fn=lambda: "2026-07-10T00:00:00+00:00")
    log.record("sala", True, 2, 1)
    app = FastAPI()
    app.include_router(build_inventory_router(_FakeInvService(), intrusion_log=log))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    kinds = [a["kind"] for a in body["alerts"]]
    assert "intrusion" in kinds
    intr = next(a for a in body["alerts"] if a["kind"] == "intrusion")
    assert intr["severity"] == "alert" and intr["room"] == "sala"
    # room-level + count-only: no geometry field ever rides the alert
    assert "x" not in intr and "y" not in intr and "targets" not in intr


def test_alerts_stream_omits_intrusion_when_unwired():
    from wavr.api_inventory import build_inventory_router
    app = FastAPI()
    app.include_router(build_inventory_router(_FakeInvService()))   # no intrusion_log
    with TestClient(app) as client:
        assert client.get("/api/alerts").json() == {"alerts": []}


# --------------------------------------------------------------------------- #
# MCP read tool never exposes per-person geometry/identity/vitals -- Watch relies
# on this being true regardless of mode (defense in depth for the MCP egress).
# --------------------------------------------------------------------------- #

class _FakeProvider:
    def list_rooms(self):
        return ["sala"]

    def room_state(self, room):
        return dict(FULL) if room == "sala" else None

    def house_map(self):
        return {}


def test_mcp_room_context_never_leaks_geometry():
    from wavr.mcp import get_room_context
    ctx = get_room_context(_FakeProvider(), "sala")
    assert "targets" not in ctx and "identities" not in ctx and "vitals" not in ctx
    # counts + occupancy are fine to expose
    assert ctx["person_count"] == 2 and ctx["occupied"] is True


# --------------------------------------------------------------------------- #
# End-to-end egress inversion + intrusion, via the real create_app wiring.
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.camera_store import CameraStore
from wavr.events import SensingEvent, Target, Identity

LOCAL = {"X-Wavr-Local": "1"}


class _WatchSource:
    """Emits one house-level identity (ana on casa) + one camera room counting TWO
    people in sala, then idles. So known-present = 1 but sala counts 2 -> sala holds
    an unrecognized person."""

    def __init__(self, count=2, person="ana"):
        self._count = count
        self._person = person

    async def events(self):
        now = datetime.now(timezone.utc).isoformat()
        yield SensingEvent(room="casa", modality="ble", presence=True, motion=0.0,
                           breathing_bpm=None, heart_bpm=None, confidence=0.7, ts=now,
                           identities=(Identity(self._person, "ble", -50),))
        now2 = datetime.now(timezone.utc).isoformat()
        tgts = tuple(Target(id=i + 1, x=float(i), y=float(i) + 0.5) for i in range(self._count))
        yield SensingEvent(room="sala", modality="camera", presence=True, motion=1.0,
                           breathing_bpm=13.0, heart_bpm=60.0, confidence=0.95, ts=now2,
                           targets=tgts, count=self._count)
        while True:
            await asyncio.sleep(0.05)


def _build(identity_enabled, source_factory, notes=None):
    prev = os.environ.get("WAVR_IDENTITY_ENABLED")
    os.environ["WAVR_IDENTITY_ENABLED"] = "1" if identity_enabled else ""
    try:
        app = create_app(
            sources=[("watchsrc", source_factory, True)],
            storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
            camera_store=CameraStore(":memory:"),
            notify=(notes.append if notes is not None else None),
        )
    finally:
        if prev is None:
            os.environ.pop("WAVR_IDENTITY_ENABLED", None)
        else:
            os.environ["WAVR_IDENTITY_ENABLED"] = prev
    return app


def _settle(client, tries=40):
    import time
    for _ in range(tries):
        st = client.get("/api/state").json()
        if "sala" in st and "casa" in st:
            return st
        time.sleep(0.05)
    return client.get("/api/state").json()


def test_watch_off_by_default_state_unchanged():
    with TestClient(_build(True, lambda: _WatchSource())) as client:
        _settle(client)
        assert client.get("/api/watch").json()["on"] is False
        sala = client.get("/api/state").json()["sala"]
        # off -> full RoomState shape, geometry present, no watch keys
        assert sala["targets"] and "watch" not in sala and "unrecognized" not in sala


def test_watch_on_suppresses_family_geometry_at_state_egress():
    notes = []
    with TestClient(_build(True, lambda: _WatchSource(), notes)) as client:
        _settle(client)
        r = client.post("/api/watch", json={"on": True}, headers=LOCAL)
        assert r.status_code == 200 and r.json()["on"] is True
        st = client.get("/api/state").json()
        sala, casa = st["sala"], st["casa"]
        # family geometry / identity / vitals are GONE from the dashboard egress
        assert sala["targets"] == [] and sala["vitals"] == {}
        assert casa["identities"] == []
        # counts + the intrusion room DO surface
        assert sala["person_count"] == 2
        assert sala["watch"] is True and sala["unrecognized"] is True
        # /api/watch reports the honest intrusion state
        w = client.get("/api/watch").json()
        assert w["intrusion_detection"] is True and w["known_present"] == 1
        assert "sala" in w["unrecognized_rooms"]
        # a high-severity edge alert was pushed to the notifier
        assert any("Vigia" in n for n in notes)


def test_watch_intrusion_alert_in_stream_and_edge_triggered():
    with TestClient(_build(True, lambda: _WatchSource())) as client:
        _settle(client)
        client.post("/api/watch", json={"on": True}, headers=LOCAL)
        # let a couple of refuse/ingest cycles run -- must NOT spam duplicates
        import time; time.sleep(0.2)
        alerts = [a for a in client.get("/api/alerts").json()["alerts"]
                  if a.get("kind") == "intrusion"]
        assert len(alerts) == 1                      # edge-triggered, one per room
        assert alerts[0]["room"] == "sala" and alerts[0]["severity"] == "alert"


def test_watch_on_but_identity_off_suppresses_without_false_alert():
    # HONESTY: with the identity layer off Watch still hides geometry (fail-safe
    # privacy) but CANNOT tell known from unknown -> no intrusion claim, no alert.
    notes = []
    with TestClient(_build(False, lambda: _WatchSource(), notes)) as client:
        _settle(client)
        client.post("/api/watch", json={"on": True}, headers=LOCAL)
        st = client.get("/api/state").json()
        assert st["sala"]["targets"] == []           # still suppressed
        assert st["sala"]["unrecognized"] is False   # never a false intrusion
        w = client.get("/api/watch").json()
        assert w["intrusion_detection"] is False and w["unrecognized_rooms"] == []
        assert not any("Vigia" in n for n in notes)


def test_watch_toggle_requires_local_csrf_header():
    with TestClient(_build(True, lambda: _WatchSource())) as client:
        _settle(client)
        r = client.post("/api/watch", json={"on": True})   # no X-Wavr-Local
        assert r.status_code == 403
        assert client.get("/api/watch").json()["on"] is False


def test_status_surfaces_watch_flag():
    with TestClient(_build(True, lambda: _WatchSource())) as client:
        _settle(client)
        assert client.get("/api/status").json()["features"]["watch"] is False
        client.post("/api/watch", json={"on": True}, headers=LOCAL)
        assert client.get("/api/status").json()["features"]["watch"] is True
