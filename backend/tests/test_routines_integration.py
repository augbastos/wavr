"""Routines wired into the live app: a time trigger drives a REAL sink end to end
(store -> engine -> executor -> the app's actual WatchMode), proving the wiring, not
just the pure pieces. Uses set_watch (no external dependency, observable via /api/watch).
"""
import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.routines import RoutineStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}


def _app(store):
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        routine_store=store)


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


def test_someone_elses_arrival_does_not_fire_my_routine():
    store = RoutineStore(":memory:")
    r = store.add("welcome me", "person_arrived", trigger_params={"person": "Augusto"},
                  actions=[{"kind": "set_watch", "params": {"on": True}}])
    store.set_enabled(r["id"], True)
    app = _app(store)

    async def _drive():
        async with app.router.lifespan_context(app):
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
