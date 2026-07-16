"""Routines wired into the live app: a time trigger drives a REAL sink end to end
(store -> engine -> executor -> the app's actual WatchMode), proving the wiring, not
just the pure pieces. Uses set_watch (no external dependency, observable via /api/watch).
"""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.device_meta import DeviceMeta
from wavr.routines import RoutineStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}


def _app(store):
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=store)


class _FakeInv:
    """latest_inventory() returns objects with just the .mac/.vendor the
    notify_new_devices sink reads -- no ARP, no real scan."""

    def __init__(self, devices):
        self._d = devices

    def latest_inventory(self):
        return self._d

    def recent_alerts(self, limit=50):
        return []

    async def start(self):
        return None

    async def stop(self):
        return None


def _watch_on(app):
    with TestClient(app, headers=CSRF) as c:
        return c.get("/api/watch").json()["on"]


def test_schedule_routine_drives_the_real_watch_sink():
    store = RoutineStore(":memory:")
    # "at 00:00" -> any wall-clock time has reached it, so one tick fires it.
    r = store.add("night discreet", "schedule", trigger_params={"at": "00:00"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)
    asyncio.run(app.state.routines_tick())        # one deterministic time/deadline pass
    fired = store.get(r["id"])
    assert fired["last_status"] == "ok" and fired["last_fired"], \
        "the routine ran its set_watch action through the real sink and recorded ok"
    # Observe the concrete effect once (single lifespan): the app's real WatchMode flipped.
    assert _watch_on(app) is True, "the routine's set_watch action really flipped the watch"


def test_disabled_routine_does_not_fire():
    store = RoutineStore(":memory:")
    r = store.add("off", "schedule", trigger_params={"at": "00:00"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    # left DISABLED (routines boot off)
    app = _app(store)
    asyncio.run(app.state.routines_tick())
    assert _watch_on(app) is False, "a disabled routine never fires"
    assert store.get(r["id"])["last_fired"] is None


def test_empty_store_tick_is_a_noop():
    app = _app(RoutineStore(":memory:"))
    # No routines at all -> byte-identical to today: a tick does nothing and does not raise.
    asyncio.run(app.state.routines_tick())
    assert _watch_on(app) is False


def test_person_arrival_fires_a_routine_end_to_end():
    # "when I arrive -> discreet mode", driven through the REAL per-person tracker wired
    # into the app. Uses the person_presence seam so no full fusion state is needed; the
    # edge dispatches off-loop, so we drive it inside a running lifespan and let it settle.
    store = RoutineStore(":memory:")
    r = store.add("welcome me", "person_arrived", trigger_params={"person": "Augusto"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()   # skip the boot presence-edge warm-up
            app.state.person_presence.update(set())          # boot baseline: nobody home
            app.state.person_presence.update({"Augusto"})    # Augusto arrives -> edge fires
            await asyncio.sleep(0.1)                          # let the dispatched action run

    asyncio.run(_drive())
    fired = store.get(r["id"])
    assert fired["last_status"] == "ok" and fired["last_fired"], \
        "a real per-person arrival ran the routine through the app"


def test_house_arrival_fires_a_routine_end_to_end():
    # "when someone arrives -> discreet mode", through the dedicated house edge detector
    # fed off the ingest. Driven via the routine_house seam so no fusion state is needed;
    # the first handle is the boot baseline (no edge), the flip fires.
    store = RoutineStore(":memory:")
    r = store.add("welcome home", "house_arrived",
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()   # skip the boot presence-edge warm-up
            # Establish "away" as the determined baseline: away_grace (default 3)
            # consecutive vacant cycles. The first determination fires no edge (booting
            # away is not an arrival); the following occupied flip IS the arrival.
            for _ in range(4):
                app.state.routine_house.handle({"room": "sala", "occupied": False})
            app.state.routine_house.handle({"room": "sala", "occupied": True})   # arrives -> edge
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    fired = store.get(r["id"])
    assert fired["last_status"] == "ok" and fired["last_fired"], \
        "a real house arrival (away -> home) ran the routine through the app"


def test_house_condition_fires_without_mqtt_via_the_dedicated_tracker():
    # Regression (QA-found): a routine with a {house: "home"} CONDITION must fire off the
    # dedicated always-on house tracker, not the optional MQTT AwayMonitor (which is None
    # on a Core with no MQTT/ntfy). Before the fix, house_home read the None _away ->
    # UNKNOWN -> the routine silently never fired without MQTT.
    store = RoutineStore(":memory:")
    r = store.add("home welcome", "house_arrived", condition={"house": "home"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)   # no MQTT/ntfy -> _away is None

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()   # skip the boot presence-edge warm-up
            for _ in range(4):
                app.state.routine_house.handle({"room": "sala", "occupied": False})  # away baseline
            app.state.routine_house.handle({"room": "sala", "occupied": True})        # arrives -> home
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    assert store.get(r["id"])["last_fired"] is not None, \
        "the house condition read the dedicated tracker, so it fired even without MQTT"


def test_notify_new_devices_reports_only_devices_seen_while_away():
    # "when I arrive -> tell me what showed up while I was out", end to end through the
    # real sink: the away edge stamps the empty-since clock, a device whose first_seen
    # lands AFTER that stamp is the only one counted, and the push carries count + vendor.
    store = RoutineStore(":memory:")
    r = store.add("arrive digest", "house_arrived",
                  actions=[{"kind": "notify_new_devices", "params": {"message": "Home:"}}])
    store.set_enabled(r["id"], True)
    dm = DeviceMeta(":memory:")
    dm.seen("aa:aa:aa:aa:aa:aa")                     # an OLD device, seen before we leave
    notified: list[str] = []
    inv = _FakeInv([SimpleNamespace(mac="bb:bb:bb:bb:bb:bb", vendor="Acme Cameras"),
                    SimpleNamespace(mac="aa:aa:aa:aa:aa:aa", vendor="Old Corp")])
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=store, device_meta=dm, net_inventory=inv, notify=notified.append)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()
            app.state.routine_house.handle({"room": "sala", "occupied": True})   # HOME baseline
            for _ in range(6):
                app.state.routine_house.handle({"room": "sala", "occupied": False})  # leave -> away EDGE (stamps)
            dm.seen("bb:bb:bb:bb:bb:bb")             # a NEW device appears while we're out
            app.state.routine_house.handle({"room": "sala", "occupied": True})       # arrive -> fire
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    assert store.get(r["id"])["last_status"] == "ok"
    msg = next((m for m in notified if "new device" in m), None)
    assert msg is not None, f"expected a new-devices push, got {notified!r}"
    assert "1 new device" in msg and "Acme Cameras" in msg, msg
    assert "Home:" in msg, "the user's optional prefix is preserved"
    assert "Old Corp" not in msg and "aa:aa" not in msg, \
        "the pre-existing device is not counted, and no MAC leaks into the push"


def test_notify_new_devices_read_failure_is_honest_not_a_false_all_clear():
    # F1 (ADR-0003): if the device_meta read raises (locked SD-card db, the bug-bank #8
    # hazard) the push must say "couldn't check", never a false "no new devices" -- the
    # whole point of the action is what showed up while you were out.
    import sqlite3

    class _BoomMeta(DeviceMeta):
        def all(self):
            raise sqlite3.OperationalError("database is locked")

    store = RoutineStore(":memory:")
    r = store.add("arrive digest", "house_arrived",
                  actions=[{"kind": "notify_new_devices", "params": {}}])
    store.set_enabled(r["id"], True)
    notified: list[str] = []
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=store, device_meta=_BoomMeta(":memory:"),
        net_inventory=_FakeInv([]), notify=notified.append)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()
            app.state.routine_house.handle({"room": "sala", "occupied": True})   # home
            for _ in range(6):
                app.state.routine_house.handle({"room": "sala", "occupied": False})  # away -> stamp
            app.state.routine_house.handle({"room": "sala", "occupied": True})       # arrive -> fire
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    assert any("couldn't check" in m.lower() for m in notified), \
        f"a failed read must be reported honestly, got {notified!r}"
    assert not any("no new device" in m.lower() for m in notified), \
        "a read failure must never masquerade as an all-clear"


def test_room_fill_fires_a_routine_end_to_end():
    # "when the kitchen fills -> discreet mode", through the per-room edge detector fed
    # off the ingest. First determination is the baseline (no edge); the flip fires.
    store = RoutineStore(":memory:")
    r = store.add("kitchen light", "room_occupied", trigger_params={"room": "cozinha"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()   # skip the boot presence-edge warm-up
            app.state.routine_rooms.handle("cozinha", False)   # baseline: empty
            app.state.routine_rooms.handle("cozinha", True)    # fills -> edge
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    fired = store.get(r["id"])
    assert fired["last_status"] == "ok" and fired["last_fired"], \
        "a real room-fill (empty -> occupied) ran the routine through the app"


def test_boot_warmup_suppresses_a_spurious_arrival():
    # Review HIGH: at boot `latest` fills one room at a time, so a person already home in a
    # not-yet-reported room would fire a spurious "arrived" when that room reports. The
    # warm-up window suppresses presence edges until the trackers are house-complete. Here
    # we DON'T mark warm, so the "arrival" must be suppressed.
    store = RoutineStore(":memory:")
    r = store.add("welcome", "person_arrived", trigger_params={"person": "Augusto"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
            # deliberately NOT calling routines_mark_warm() -> boot warm-up is active
            app.state.person_presence.update(set())
            app.state.person_presence.update({"Augusto"})   # would be an arrival, but suppressed
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    assert store.get(r["id"])["last_fired"] is None, \
        "no presence routine fires during the boot warm-up window"


def test_someone_elses_arrival_does_not_fire_my_routine():
    store = RoutineStore(":memory:")
    r = store.add("welcome me", "person_arrived", trigger_params={"person": "Augusto"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
            app.state.routines_mark_warm()   # skip the boot presence-edge warm-up
            app.state.person_presence.update(set())
            app.state.person_presence.update({"Bea"})        # a DIFFERENT person arrives
            await asyncio.sleep(0.1)

    asyncio.run(_drive())
    assert store.get(r["id"])["last_fired"] is None, "only MY arrival fires my routine"


def test_schedule_fires_once_then_holds_same_day():
    store = RoutineStore(":memory:")
    r = store.add("once", "schedule", trigger_params={"at": "00:00"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)
    asyncio.run(app.state.routines_tick())          # fires
    first = store.get(r["id"])["last_fired"]
    assert first, "fired on the first tick"
    asyncio.run(app.state.routines_tick())          # same engine, same day -> held
    assert store.get(r["id"])["last_fired"] == first, \
        "schedule already fired today -> the second tick does not re-fire (last_fired unchanged)"
