"""Routines spine: store validation, engine matching + the consent/sensing guards,
and the failure-tolerant executor. All in-memory, no HA/notifier/loop -- the three
pieces are pure and injectable by design.
"""
from datetime import datetime

import pytest

from wavr.routines import ActionExecutor, RoutineStore, RoutinesEngine


def _store():
    return RoutineStore(":memory:")


def _light_action(entity="light.sala", service="turn_on"):
    return {"kind": "ha_service", "params": {"domain": "light", "service": service,
                                             "entity_id": entity}}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_new_routine_starts_disabled():
    s = _store()
    r = s.add("arrive light", "house_arrived", actions=[_light_action()])
    assert r["enabled"] is False, "routines must boot OFF, like cameras"
    assert r["id"] and r["name"] == "arrive light"
    assert r["trigger_params"] == {} and r["actions"][0]["params"]["entity_id"] == "light.sala"


def test_json_columns_round_trip_as_objects():
    s = _store()
    r = s.add("room rule", "room_occupied", trigger_params={"room": "cozinha"},
              condition={"house": "home"},
              actions=[_light_action(), {"kind": "notify", "params": {"message": "hi"}}])
    got = s.get(r["id"])
    assert got["trigger_params"] == {"room": "cozinha"}
    assert got["condition"] == {"house": "home"}
    assert [a["kind"] for a in got["actions"]] == ["ha_service", "notify"]


@pytest.mark.parametrize("bad", [
    dict(name="", trigger_kind="house_arrived", actions=[_light_action()]),               # empty name
    dict(name="x", trigger_kind="nope", actions=[_light_action()]),                       # bad trigger
    dict(name="x", trigger_kind="room_occupied", actions=[_light_action()]),              # missing room param
    dict(name="x", trigger_kind="schedule", trigger_params={"at": "99:99"},
         actions=[_light_action()]),                                                      # bad time
    dict(name="x", trigger_kind="house_arrived", actions=[]),                             # no actions
    dict(name="x", trigger_kind="house_arrived", actions=[{"kind": "sensing_on"}]),       # forbidden action kind
    dict(name="x", trigger_kind="house_arrived",
         actions=[{"kind": "ha_service", "params": {"domain": "light"}}]),                # ha_service missing service
])
def test_invalid_payloads_raise(bad):
    with pytest.raises(ValueError):
        _store().add(**bad)


def test_set_enabled_update_delete_mark():
    s = _store()
    r = s.add("r", "house_left", actions=[_light_action(service="turn_off")])
    assert s.set_enabled(r["id"], True) is True
    assert s.get(r["id"])["enabled"] is True
    s.mark_fired(r["id"], "2026-07-16T23:00:00Z", "ok")
    assert s.get(r["id"])["last_status"] == "ok"
    assert s.update(r["id"], "r2", "house_arrived", {}, [_light_action()]) is not None
    assert s.get(r["id"])["name"] == "r2" and s.get(r["id"])["trigger_kind"] == "house_arrived"
    assert s.delete(r["id"]) is True and s.get(r["id"]) is None
    assert s.update("ghost", "n", "house_arrived", {}, [_light_action()]) is None


# --------------------------------------------------------------------------- #
# Engine — matching
# --------------------------------------------------------------------------- #
def _engine(store, sensing=True, home=None):
    return RoutinesEngine(store, sensing_on=lambda: sensing, house_home=lambda: home)


def test_house_edge_matches_only_enabled_of_the_right_kind():
    s = _store()
    arrive = s.add("a", "house_arrived", actions=[_light_action()]); s.set_enabled(arrive["id"], True)
    leave = s.add("l", "house_left", actions=[_light_action()]); s.set_enabled(leave["id"], True)
    s.add("disabled", "house_arrived", actions=[_light_action()])   # left OFF
    eng = _engine(s)
    fired = eng.on_house_edge(True)
    assert [r["id"] for r in fired] == [arrive["id"]], "only enabled house_arrived"
    assert [r["id"] for r in eng.on_house_edge(False)] == [leave["id"]]


def test_room_and_person_edges_match_their_param():
    s = _store()
    a = s.add("k", "room_occupied", trigger_params={"room": "cozinha"}, actions=[_light_action()])
    s.set_enabled(a["id"], True)
    b = s.add("me", "person_arrived", trigger_params={"person": "Augusto"}, actions=[_light_action()])
    s.set_enabled(b["id"], True)
    eng = _engine(s)
    assert [r["id"] for r in eng.on_room_edge("cozinha", True)] == [a["id"]]
    assert eng.on_room_edge("sala", True) == [], "different room -> no match"
    assert [r["id"] for r in eng.on_person_edge("Augusto", True)] == [b["id"]]
    assert eng.on_person_edge("Bea", True) == [], "different person -> no match"


def test_device_seen_fires_on_appearance_only_and_matches_mac():
    s = _store()
    r = s.add("d", "device_seen", trigger_params={"mac": "AA:BB:CC:DD:EE:FF"},
              actions=[{"kind": "notify", "params": {"message": "device"}}])
    s.set_enabled(r["id"], True)
    eng = _engine(s)
    assert [x["id"] for x in eng.on_device_edge("aa:bb:cc:dd:ee:ff", True)] == [r["id"]]  # case-insensitive
    assert eng.on_device_edge("aa:bb:cc:dd:ee:ff", False) == [], "offline never fires device_seen"
    assert eng.on_device_edge("11:22:33:44:55:66", True) == [], "other mac -> no match"


# --------------------------------------------------------------------------- #
# Engine — the consent/sensing guard on conditions
# --------------------------------------------------------------------------- #
def test_house_condition_fires_only_when_state_matches():
    s = _store()
    r = s.add("away only", "schedule", trigger_params={"at": "23:00"},
              condition={"house": "away"}, actions=[{"kind": "set_watch", "params": {"on": True}}])
    s.set_enabled(r["id"], True)
    # via a house edge to isolate the condition eval (use on_house_edge with a cond routine)
    r2 = s.add("home cond", "house_arrived", condition={"house": "home"}, actions=[_light_action()])
    s.set_enabled(r2["id"], True)
    assert _engine(s, sensing=True, home=True).on_house_edge(True) != [], "condition met -> fires"
    assert _engine(s, sensing=True, home=False).on_house_edge(True) == [], "condition not met -> skip"


def test_presence_condition_does_not_fire_when_sensing_is_off():
    s = _store()
    r = s.add("cond", "house_arrived", condition={"house": "home"}, actions=[_light_action()])
    s.set_enabled(r["id"], True)
    # sensing OFF => UNKNOWN => must NOT fire, even though the edge itself arrived.
    assert _engine(s, sensing=False, home=True).on_house_edge(True) == [], \
        "never assert house state the sensor can't currently confirm"


# --------------------------------------------------------------------------- #
# Engine — time triggers
# --------------------------------------------------------------------------- #
def test_schedule_fires_once_per_day_after_the_time():
    s = _store()
    r = s.add("night", "schedule", trigger_params={"at": "23:00"},
              actions=[{"kind": "set_watch", "params": {"on": True}}])
    s.set_enabled(r["id"], True)
    eng = _engine(s)
    assert eng.tick(datetime(2026, 7, 16, 22, 59), None) == [], "before the time -> no fire"
    assert [x["id"] for x in eng.tick(datetime(2026, 7, 16, 23, 0), None)] == [r["id"]], "at the time -> fire"
    assert eng.tick(datetime(2026, 7, 16, 23, 30), None) == [], "same day -> no re-fire"
    assert [x["id"] for x in eng.tick(datetime(2026, 7, 17, 23, 0), None)] == [r["id"]], "next day -> fires again"


def test_tick_applies_the_condition_gate():
    # Review HIGH: tick() used to ignore conditions. A schedule with a {house: home}
    # condition must NOT fire when the house is away, and MUST fire when it is home.
    s = _store()
    r = s.add("home only", "schedule", trigger_params={"at": "00:00"},
              condition={"house": "home"}, actions=[_light_action()])
    s.set_enabled(r["id"], True)
    away = RoutinesEngine(s, sensing_on=lambda: True, house_home=lambda: False)
    assert away.tick(datetime(2026, 7, 16, 1, 0), False) == [], "condition away -> schedule skipped"
    home = RoutinesEngine(s, sensing_on=lambda: True, house_home=lambda: True)
    assert [x["id"] for x in home.tick(datetime(2026, 7, 16, 1, 0), True)] == [r["id"]]


def test_away_by_time_does_not_fire_when_sensing_is_off():
    # Review MEDIUM: "nobody home by X" asserts away, so it must not fire on an unknowable
    # (sensing-off) state, even if the last latched house state was away.
    s = _store()
    r = s.add("nobody", "house_away_by_time", trigger_params={"by": "00:00"},
              actions=[{"kind": "notify", "params": {"message": "x"}}])
    s.set_enabled(r["id"], True)
    off = RoutinesEngine(s, sensing_on=lambda: False, house_home=lambda: False)
    assert off.tick(datetime(2026, 7, 16, 1, 0), False) == [], "sensing off -> away unknowable -> no fire"


def test_schedule_does_not_refire_after_a_same_day_restart():
    # Review MEDIUM: the once-per-day guard must survive a restart via the persisted
    # last_fired, or a reboot after the schedule time re-fires it the same day.
    s = _store()
    r = s.add("night", "schedule", trigger_params={"at": "23:00"},
              actions=[{"kind": "set_watch", "params": {"on": True}}])
    s.set_enabled(r["id"], True)
    s.mark_fired(r["id"], datetime(2026, 7, 16, 23, 0).isoformat(), "ok")  # already fired today
    fresh = RoutinesEngine(s, sensing_on=lambda: True)                     # post-restart: empty _fired_day
    assert fresh.tick(datetime(2026, 7, 16, 23, 30), None) == [], "persisted last_fired blocks the re-fire"
    assert [x["id"] for x in fresh.tick(datetime(2026, 7, 17, 23, 30), None)] == [r["id"]], "next day fires"


def test_non_house_condition_is_rejected_at_write_time():
    # Review LOW: a condition the engine can't evaluate used to be accepted and then
    # silently fail OPEN. It must be rejected up front instead.
    with pytest.raises(ValueError):
        _store().add("x", "house_arrived", condition={"person": "aug", "state": "home"},
                     actions=[_light_action()])
    with pytest.raises(ValueError):
        _store().add("x", "house_arrived", condition={"house": "maybe"}, actions=[_light_action()])


def test_away_by_time_fires_only_when_house_is_away():
    s = _store()
    r = s.add("nobody", "house_away_by_time", trigger_params={"by": "00:00"},
              actions=[{"kind": "notify", "params": {"message": "nobody home"}}])
    s.set_enabled(r["id"], True)
    eng = _engine(s)
    assert eng.tick(datetime(2026, 7, 16, 0, 1), True) == [], "house home -> no alert"
    assert [x["id"] for x in eng.tick(datetime(2026, 7, 16, 0, 1), False)] == [r["id"]], "house away -> alert"


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
def test_executor_runs_each_sink():
    calls = {"ha": [], "notify": [], "watch": []}
    ex = ActionExecutor(
        ha_call=lambda d, s, e: calls["ha"].append((d, s, e)),
        notify=lambda m: calls["notify"].append(m),
        watch_set=lambda on: calls["watch"].append(on))
    status = ex.run([_light_action(), {"kind": "notify", "params": {"message": "hi"}},
                     {"kind": "set_watch", "params": {"on": True}}])
    assert status == "ok"
    assert calls["ha"] == [("light", "turn_on", "light.sala")]
    assert calls["notify"] == ["hi"] and calls["watch"] == [True]


def test_one_action_failing_does_not_stop_the_rest():
    calls = []
    def boom(*a): raise RuntimeError("HA down")
    ex = ActionExecutor(ha_call=boom, notify=lambda m: calls.append(m))
    status = ex.run([_light_action(), {"kind": "notify", "params": {"message": "still runs"}}])
    assert status == "partial", "one failed, one ok"
    assert calls == ["still runs"], "the notify still ran after the HA failure"


def test_all_failing_is_failed_and_missing_sink_raises_per_action():
    ex = ActionExecutor()   # no sinks wired at all
    assert ex.run([_light_action()]) == "failed", "no HA sink -> that action fails"
    assert ex.run([{"kind": "notify", "params": {"message": "x"}}]) == "failed"


def test_executor_has_no_sensing_or_camera_sink():
    # Structural guarantee: the executor exposes ONLY ha/notify/watch. There is no
    # code path to turn sensing or a camera on, so a routine can never re-arm either.
    ex = ActionExecutor(ha_call=lambda *a: None)
    assert ex.run([{"kind": "sensing_on", "params": {}}]) == "failed"
    assert ex.run([{"kind": "camera_on", "params": {"name": "quarto"}}]) == "failed"


def test_notify_new_devices_needs_no_params_and_routes_to_its_sink():
    # The action's body is computed at fire time, so `message` is optional (a prefix).
    s = RoutineStore(":memory:")
    r = s.add("arrive digest", "house_arrived",
              actions=[{"kind": "notify_new_devices", "params": {}}])  # no required params
    assert r["actions"][0]["kind"] == "notify_new_devices"
    got = []
    ex = ActionExecutor(new_devices_notify=lambda p: got.append(p))
    assert ex.run([{"kind": "notify_new_devices", "params": {"message": "Hi"}}]) == "ok"
    assert got == [{"message": "Hi"}], "the raw params reach the sink for it to compute the body"
    # missing sink -> that action fails, never crashes the loop
    assert ActionExecutor().run([{"kind": "notify_new_devices", "params": {}}]) == "failed"


# --------------------------------------------------------------------------- #
# The on_edge hooks fire on the REAL monitors (additive, guarded like the event)
# --------------------------------------------------------------------------- #
def test_away_monitor_fires_on_edge_not_on_first_determination():
    from wavr.away import AwayMonitor
    edges = []
    m = AwayMonitor(away_grace=1, on_edge=lambda home: edges.append(home))
    m.handle({"room": "sala", "occupied": True})    # first determination -> home, NO edge
    assert edges == [] and m.home is True, "no spurious edge at boot; home property tracks state"
    m.handle({"room": "sala", "occupied": False})   # away edge
    assert edges == [False] and m.home is False
    m.handle({"room": "sala", "occupied": True})    # arrived edge
    assert edges == [False, True]


def test_rules_engine_fires_on_room_edge():
    from wavr.rules import RulesEngine
    edges = []
    r = RulesEngine(publish=lambda *a: None, on_edge=lambda room, occ: edges.append((room, occ)))
    r.handle({"room": "cozinha", "occupied": True, "confidence": 1.0, "ts": "t"})   # first, no edge
    r.handle({"room": "cozinha", "occupied": False, "confidence": 1.0, "ts": "t"})  # flip -> edge
    r.handle({"room": "cozinha", "occupied": True, "confidence": 1.0, "ts": "t"})   # flip -> edge
    assert edges == [("cozinha", False), ("cozinha", True)]
