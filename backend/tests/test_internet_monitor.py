import asyncio
import inspect

from wavr.internet_monitor import InternetMonitor, guess_gateway, make_checker


def _flaky(results):
    """Injectable check transport: pops True/False off `results` per call (no
    real network). Raises IndexError if called more times than provided --
    tests size the list to exactly the number of expected checks."""
    it = iter(results)

    async def check() -> bool:
        return next(it)
    return check


# ---- guess_gateway --------------------------------------------------------------

def test_guess_gateway_uses_dot_one_of_local_24():
    assert guess_gateway("192.168.1.42") == "192.168.1.1"


def test_guess_gateway_none_for_empty_ip():
    assert guess_gateway("") is None


def test_guess_gateway_falls_back_to_local_ipv4_when_unset(monkeypatch):
    import wavr.internet_monitor as mod
    monkeypatch.setattr(mod, "_local_ipv4", lambda: None)
    assert guess_gateway() is None
    monkeypatch.setattr(mod, "_local_ipv4", lambda: "10.0.0.55")
    assert guess_gateway() == "10.0.0.1"


# ---- status() shape --------------------------------------------------------------

async def test_status_shape_before_any_check():
    m = InternetMonitor(check=_flaky([]))
    assert m.status() == {"ok": None, "since": None}


# ---- first determination: settles without notifying ------------------------------

async def test_first_reachable_settles_ok_without_notify():
    notified = []
    m = InternetMonitor(check=_flaky([True]), notify=notified.append)
    await m.check_once()
    assert m.status()["ok"] is True
    assert m.status()["since"] is not None
    assert notified == []


async def test_first_unreachable_settles_down_after_debounce_without_notify():
    notified = []
    m = InternetMonitor(check=_flaky([False, False, False]), fail_threshold=3,
                         notify=notified.append)
    await m.check_once()
    await m.check_once()
    assert m.status()["ok"] is None            # not yet debounced
    await m.check_once()
    assert m.status()["ok"] is False           # debounce met -> settles
    assert notified == []                      # but no transition notify (first-ever)


# ---- debounced down transition ----------------------------------------------------

async def test_down_transition_requires_n_consecutive_fails():
    notified = []
    m = InternetMonitor(check=_flaky([True, False, False, False]), fail_threshold=3,
                         notify=notified.append)
    await m.check_once()                        # settle up
    await m.check_once()                        # fail 1/3
    assert m.status()["ok"] is True             # a single dropped check must not alert
    await m.check_once()                        # fail 2/3
    assert m.status()["ok"] is True
    await m.check_once()                        # fail 3/3 -> debounce met
    assert m.status()["ok"] is False
    assert notified == ["Wavr: internet caiu"]


async def test_fail_streak_reset_by_an_ok_check():
    # 2 fails, then a success (resets the streak), then 2 more fails: never
    # reaches the fail_threshold=3 debounce in one unbroken streak.
    notified = []
    m = InternetMonitor(check=_flaky([True, False, False, True, False, False]),
                         fail_threshold=3, notify=notified.append)
    for _ in range(6):
        await m.check_once()
    assert m.status()["ok"] is True             # the streak was reset by the mid-sequence success
    assert notified == []


# ---- up transition fires immediately (no debounce needed for recovery) -----------

async def test_up_transition_fires_on_single_success():
    notified = []
    m = InternetMonitor(check=_flaky([True, False, False, False, True]),
                         fail_threshold=3, notify=notified.append)
    for _ in range(5):
        await m.check_once()
    assert m.status()["ok"] is True
    assert notified == ["Wavr: internet caiu", "Wavr: internet voltou"]


# ---- tolerance: a raising check counts as a failed check, never propagates -------

async def test_raising_check_counts_as_failure_and_does_not_raise():
    async def boom():
        raise RuntimeError("no route to host")
    m = InternetMonitor(check=boom, fail_threshold=1)
    ok = await m.check_once()                   # must not raise
    assert ok is False
    assert m.status()["ok"] is False


async def test_raising_notify_does_not_propagate():
    def boom(msg):
        raise RuntimeError("ntfy unreachable")
    m = InternetMonitor(check=_flaky([True, False]), fail_threshold=1, notify=boom)
    await m.check_once()
    await m.check_once()                         # must not raise despite notify blowing up
    assert m.status()["ok"] is False


# ---- start()/stop() cancel-safety (mirrors NetworkInventoryService) --------------

async def test_start_runs_checks_then_stop_is_cancel_safe():
    calls = []

    async def check():
        calls.append(1)
        return True
    m = InternetMonitor(check=check, interval=0.01, fail_threshold=1)
    await m.start()
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls                                  # background task ran >=1 check
    await m.stop()                                 # must not raise
    await m.stop()                                 # idempotent second stop


# ---- make_checker / real ping transport shape (no real network exercised) --------

def test_make_checker_returns_coroutine_function():
    fn = make_checker("192.168.1.1")
    assert inspect.iscoroutinefunction(fn)
