"""2C notify fan-out (project_wavr_agentic_home_mission): Telegram alongside the
existing ntfy `_notify` on the away-edge and fall_suspected callbacks (app.py's
`_notify_all` closure), plus the opt-in daily-digest scheduler (`_digest_once`,
gated on its OWN "digest" connector row -- separate from "telegram"/ntfy alone).

Real create_app wiring end to end: injected fakes stand in for I/O (ntfy notify,
occupancy_log, device_meta); `wavr.connectors.notify.telegram.post_json` is
monkeypatched to a call recorder so no real network is ever attempted. Mirrors
test_fall_detect_wiring.py / test_notifier_wiring.py's own style (real pipeline,
fake transports).

Rogue-device is NOT separately covered here: it calls the SAME `_notify_all`
helper exercised below by fall/away, and (like the pre-existing ntfy-only
wiring it replaces) there is no app.py-level test seam to trigger a real
NetworkInventoryService scan without deep ARP mocking -- see
test_netinventory_service.py for the on_rogue callback MECHANISM's own
independent unit coverage.
"""
from __future__ import annotations

import asyncio
import sqlite3

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.connector_store import ConnectorStore
from wavr.device_meta import DeviceMeta
from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.occupancy_log import OccupancyLog
from wavr.storage import Storage


class _FakeInvService:
    def latest_inventory(self):
        return []

    def recent_alerts(self):
        return []

    async def start(self):
        return None

    async def stop(self):
        return None


class _FakePost:
    """Records every call instead of touching the network -- mirrors
    test_connector_notify.py's FakePost."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, payload, headers=None, timeout=None):
        self.calls.append({"url": url, "payload": payload})
        return {"ok": True}


async def _wait_until(predicate, tries: int = 100, delay: float = 0.01) -> None:
    """Poll `predicate()` until truthy or `tries` are exhausted -- the Telegram
    fan-out is dispatched via `asyncio.create_task(asyncio.to_thread(...))`, so a
    single `sleep(0)` isn't reliably enough for the thread-pool hop to land back
    on the event loop under load (observed flaky in the full suite run)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(delay)


def _telegram_enabled_store() -> ConnectorStore:
    s = ConnectorStore(":memory:")
    s.upsert("telegram", "generic", "Telegram Notify")
    s.set_enabled("telegram", True)
    return s


def _lying_events():
    tgt = (Target(id=1, x=1.0, y=1.0, posture="lying", confidence=0.9),)
    ev1 = SensingEvent(room="quarto", modality="camera", presence=True, motion=0.1,
                       breathing_bpm=None, heart_bpm=None, confidence=0.9,
                       ts="2026-07-10T00:00:00+00:00", targets=tgt, count=1)
    ev2 = SensingEvent(room="quarto", modality="camera", presence=True, motion=0.1,
                       breathing_bpm=None, heart_bpm=None, confidence=0.9,
                       ts="2026-07-10T00:00:01+00:00", targets=tgt, count=1)
    return ev1, ev2


# --------------------------------------------------------------------------- #
# fall_suspected -> _notify_all fan-out (ntfy always, Telegram gated)
# --------------------------------------------------------------------------- #

def test_fall_alert_reaches_ntfy_and_telegram_stays_silent_when_disabled(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("wavr.connectors.notify.telegram.post_json", fake_post)
    # Every store this test doesn't explicitly inject (identity/pin/assistant/known/
    # ha_import) still falls back to cfg.db_path ("wavr.db", cwd-relative) -- pin it
    # to :memory: so a test run never grows the gitignored local wavr.db.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_FALL_DETECT", "1")
    monkeypatch.setenv("WAVR_FALL_DWELL_S", "0")
    try:
        notified = []
        store = ConnectorStore(":memory:")   # "telegram" row absent -> is_enabled() False
        app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                         fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                         net_inventory=_FakeInvService(), notify=notified.append,
                         connector_store=store)
        ev1, ev2 = _lying_events()

        async def drive():
            await app.state.ingest(ev1)
            await app.state.ingest(ev2)
            await asyncio.sleep(0)   # let any create_task'd fan-out settle

        asyncio.run(drive())
        assert notified and "possivel queda" in notified[0]
        assert fake_post.calls == []     # telegram disabled -> zero network attempted
    finally:
        monkeypatch.delenv("WAVR_FALL_DETECT", raising=False)
        monkeypatch.delenv("WAVR_FALL_DWELL_S", raising=False)


def test_fall_alert_fans_out_to_telegram_when_enabled(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("wavr.connectors.notify.telegram.post_json", fake_post)
    # Every store this test doesn't explicitly inject (identity/pin/assistant/known/
    # ha_import) still falls back to cfg.db_path ("wavr.db", cwd-relative) -- pin it
    # to :memory: so a test run never grows the gitignored local wavr.db.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setenv("WAVR_FALL_DETECT", "1")
    monkeypatch.setenv("WAVR_FALL_DWELL_S", "0")
    try:
        app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                         fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                         net_inventory=_FakeInvService(),
                         connector_store=_telegram_enabled_store())
        ev1, ev2 = _lying_events()

        async def drive():
            await app.state.ingest(ev1)
            await app.state.ingest(ev2)
            await _wait_until(lambda: fake_post.calls)

        asyncio.run(drive())
        assert len(fake_post.calls) == 1
        payload = fake_post.calls[0]["payload"]
        assert "fall_suspected" in payload["text"]   # kind/severity/room -- the allowlist
        assert "quarto" in payload["text"]
        assert "ALERT" in payload["text"]
        # never a target position/posture/frame (ADR-0002/ADR-0003)
        for leak in ("x=1.0", "y=1.0", "lying", "posture", "frame"):
            assert leak not in payload["text"]
    finally:
        for k in ("WAVR_TELEGRAM_TOKEN", "WAVR_TELEGRAM_CHAT_ID",
                  "WAVR_FALL_DETECT", "WAVR_FALL_DWELL_S"):
            monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------- #
# away-edge (arrived/left) -> _notify_all fan-out, gated at boot on
# (_rules_publish or _notify or telegram enabled)
# --------------------------------------------------------------------------- #

async def test_away_edge_fans_out_to_telegram_when_enabled_even_without_ntfy(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("wavr.connectors.notify.telegram.post_json", fake_post)
    # Every store this test doesn't explicitly inject (identity/pin/assistant/known/
    # ha_import) still falls back to cfg.db_path ("wavr.db", cwd-relative) -- pin it
    # to :memory: so a test run never grows the gitignored local wavr.db.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("WAVR_AWAY_GRACE", raising=False)   # default grace = 3
    try:
        hub = Hub()
        # No `notify=` injected (ntfy path stays None) -- Telegram alone (boot-time
        # enabled) must still be enough for AwayMonitor to be built and wired.
        app = create_app(sources=[], hub=hub, storage=Storage(":memory:"),
                         fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                         connector_store=_telegram_enabled_store())
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert len(hub._subscribers) == 1   # the away monitor is running
            await hub.publish({"room": "sala", "occupied": True, "confidence": 0.9,
                               "vitals": {}, "sources": [], "explanation": "",
                               "ts": "2026-07-10T10:00:00+00:00"})
            await asyncio.sleep(0.02)
            for _ in range(3):
                await hub.publish({"room": "sala", "occupied": False, "confidence": 0.9,
                                   "vitals": {}, "sources": [], "explanation": "",
                                   "ts": "2026-07-10T10:00:00+00:00"})
            await _wait_until(lambda: fake_post.calls)
        assert len(fake_post.calls) == 1
        assert "casa vazia" in fake_post.calls[0]["payload"]["text"]
    finally:
        monkeypatch.delenv("WAVR_TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("WAVR_TELEGRAM_CHAT_ID", raising=False)


# --------------------------------------------------------------------------- #
# Daily digest scheduler (app.state.digest_once): gated on its OWN "digest"
# connector row -- separate from "telegram" alone.
# --------------------------------------------------------------------------- #

def test_digest_once_is_noop_when_digest_connector_disabled(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("wavr.connectors.notify.telegram.post_json", fake_post)
    # Every store this test doesn't explicitly inject (identity/pin/assistant/known/
    # ha_import) still falls back to cfg.db_path ("wavr.db", cwd-relative) -- pin it
    # to :memory: so a test run never grows the gitignored local wavr.db.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", "chat")
    try:
        # "telegram" enabled but "digest" is NOT -- proves the two are independently
        # gated (enabling Telegram for alerts does not imply the proactive digest push).
        app = create_app(sources=[], storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
                         net_inventory=_FakeInvService(),
                         connector_store=_telegram_enabled_store())
        result = asyncio.run(app.state.digest_once())
        assert result == {"ok": False, "status": "disabled", "via": None}
        assert fake_post.calls == []
    finally:
        monkeypatch.delenv("WAVR_TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("WAVR_TELEGRAM_CHAT_ID", raising=False)


def test_digest_once_survives_a_store_blip_instead_of_killing_the_scheduler(monkeypatch):
    # "The gate above the guard". The digest's connector gate is a RAW sqlite read
    # (ConnectorStore.get -> SELECT, unguarded) and it sat one line ABOVE the try that guards
    # everything else in _digest_once. wavr.db is shared by five stores on SD-card-backed
    # sqlite on a Core that runs for weeks, so "database is locked" is a real recurring error
    # class there -- and a single blip propagated OUT of _digest_once, exited _digest_loop's
    # unguarded `while True`, and killed the scheduler for the whole process lifetime with
    # ZERO log output: the task's exception was never retrieved, the strong reference kept it
    # from being GC'd so asyncio's own "exception was never retrieved" fallback never fired,
    # and shutdown's suppress(CancelledError, Exception) ate the last chance to see it. The
    # tick runs once per 24h, so nothing ever retried.
    # Existing coverage drives _digest_once via the seam only and never proves it absorbs a
    # raise -- which is exactly how this hid.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    store = _telegram_enabled_store()
    app = create_app(sources=[], storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
                     net_inventory=_FakeInvService(), connector_store=store)

    # Blip the gate AFTER create_app -- its own startup reads must succeed first.
    def _locked(_cid):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "is_enabled", _locked)

    result = asyncio.run(app.state.digest_once())
    assert result == {"ok": False, "status": "error", "via": None}, (
        "a store blip must not escape _digest_once -- it exits _digest_loop's `while True` "
        "and kills the daily digest for the whole process lifetime, with no log"
    )


def test_digest_once_sends_via_telegram_when_both_connectors_enabled(monkeypatch):
    fake_post = _FakePost()
    monkeypatch.setattr("wavr.connectors.notify.telegram.post_json", fake_post)
    # Every store this test doesn't explicitly inject (identity/pin/assistant/known/
    # ha_import) still falls back to cfg.db_path ("wavr.db", cwd-relative) -- pin it
    # to :memory: so a test run never grows the gitignored local wavr.db.
    monkeypatch.setenv("WAVR_DB", ":memory:")
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", "chat")
    try:
        store = _telegram_enabled_store()
        store.upsert("digest", "generic", "Daily Digest (proactive push)")
        store.set_enabled("digest", True)
        device_meta = DeviceMeta(":memory:")
        device_meta.seen("aa:bb:cc:dd:ee:ff")   # one "new" device, first_seen = now
        app = create_app(sources=[], storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
                         net_inventory=_FakeInvService(), connector_store=store,
                         device_meta=device_meta,
                         occupancy_log=OccupancyLog(":memory:", retention_days=None))
        result = asyncio.run(app.state.digest_once())
        assert result["ok"] is True and result["via"] == "telegram"
        assert len(fake_post.calls) == 1
        text = fake_post.calls[0]["payload"]["text"]
        assert "1 new device" in text
        # house-level counts/schedule only -- never identity/geometry/room names
        for leak in ("mac", "aa:bb:cc:dd:ee:ff", "coordinate", "target"):
            assert leak not in text.lower()
    finally:
        monkeypatch.delenv("WAVR_TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("WAVR_TELEGRAM_CHAT_ID", raising=False)
