"""A broken source must not become a broken Wavr — and must not become a
SILENTLY broken Wavr, which is the harder half.

The tests that matter most here are the honesty ones: a crashed source is a
fault, a switched-off source is not, and neither one is allowed to read as
"working" on a screen that exists to say what Wavr can see.
"""
import asyncio

import pytest

from wavr.provider_runtime import (
    BASE_BACKOFF_S, COLD_PROBE_S, FAST_ATTEMPTS, FAST_BACKOFF_CAP_S,
    HEALTHY_RUN_S, STATE_COMPLETED, STATE_FAILED, STATE_RETRYING,
    STATE_RUNNING, STATE_SILENT, STATE_STOPPED, SourceSupervisor,
    backoff_for, redact, supervised,
)
from wavr.sensor_coverage import (
    HEALTH_DISABLED, HEALTH_OFFLINE, HEALTH_OK, HEALTH_UNKNOWN,
    collect_coverage, health_from_source_state,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def sup(rand=lambda: 0.5):
    """A supervisor on a clock the test drives, with jitter pinned to the middle
    so a delay assertion is about the policy and not about luck."""
    clock = FakeClock()
    return SourceSupervisor(clock=clock, rand=rand), clock


# -- Redaction: this runs on data that ends up on screens ----------------------

def test_a_camera_password_never_survives_into_health():
    """An RTSP error message routinely carries the URL it was dialling, and that
    URL routinely carries the password. Health is rendered, bundled into
    diagnostics and read by agents."""
    out = redact("OSError: failed to open rtsp://admin:hunter2@192.168.1.9/s1")
    assert "hunter2" not in out
    assert "192.168.1.9" in out, "the host is what makes the error actionable"


def test_a_bare_token_is_redacted_too():
    out = redact("401 from provider, key=sk_live_abcdefghijklmnopqrstuvwxyz012")
    assert "sk_live_abcdefghijklmnopqrstuvwxyz012" not in out


def test_redaction_happens_on_the_way_in():
    """So no future reader of `last_error` has to remember to filter it."""
    s, _ = sup()
    s.register("cam")
    s.on_start("cam")
    s.on_failure("cam", OSError("rtsp://admin:hunter2@cam.local/s"))
    assert "hunter2" not in s.get("cam").last_error


def test_a_long_error_is_bounded():
    assert len(redact("x" * 5000)) <= 200


# -- Backoff: the retry-storm rules -------------------------------------------

def test_backoff_doubles_then_caps():
    mid = lambda: 0.5     # noqa: E731 -- no jitter, so the shape is visible
    assert backoff_for(1, rand=mid) == pytest.approx(BASE_BACKOFF_S)
    assert backoff_for(2, rand=mid) == pytest.approx(BASE_BACKOFF_S * 2)
    assert backoff_for(4, rand=mid) <= FAST_BACKOFF_CAP_S


def test_after_the_fast_attempts_it_goes_cold_not_faster():
    """Several cameras usually live on the same box. When that box reboots they
    all fail in the same second, and a short fixed retry would have them hammer
    the thing that is already struggling."""
    assert backoff_for(FAST_ATTEMPTS, rand=lambda: 0.5) == pytest.approx(COLD_PROBE_S)
    assert backoff_for(FAST_ATTEMPTS + 9, rand=lambda: 0.5) == pytest.approx(COLD_PROBE_S)


def test_jitter_spreads_simultaneous_failures():
    early, late = backoff_for(3, rand=lambda: 0.0), backoff_for(3, rand=lambda: 1.0)
    assert early < late, "identical delays would keep failed sources in lockstep"


def test_a_failed_source_keeps_probing_forever():
    """A permanent "failed, click here" is the wrong end state for something that
    sits on a shelf. Routers reboot; Wavr should still be working when somebody
    walks past three days later."""
    assert backoff_for(500, rand=lambda: 0.5) == pytest.approx(COLD_PROBE_S)


# -- The state machine ---------------------------------------------------------

def test_a_crash_is_retrying_then_failed():
    s, _ = sup()
    s.register("radar")
    for i in range(1, FAST_ATTEMPTS):
        s.on_start("radar")
        s.on_failure("radar", RuntimeError("boom"))
        assert s.state_of("radar") == STATE_RETRYING, i
    s.on_start("radar")
    s.on_failure("radar", RuntimeError("boom"))
    assert s.state_of("radar") == STATE_FAILED


def test_a_healthy_run_resets_the_counter():
    """Otherwise a camera that drops once a day accumulates failures across
    unrelated incidents and eventually lands in slow-probe permanently, where it
    recovers an hour late every time. The counter measures THIS outage."""
    s, clock = sup()
    s.register("cam")
    for _ in range(FAST_ATTEMPTS + 2):
        s.on_start("cam")
        clock.advance(HEALTHY_RUN_S + 1)
        s.on_failure("cam", OSError("stream ended"))
    assert s.get("cam").failures == 1
    assert s.state_of("cam") == STATE_RETRYING


def test_repeated_fast_crashes_do_not_reset():
    s, clock = sup()
    s.register("cam")
    for _ in range(3):
        s.on_start("cam")
        clock.advance(0.2)
        s.on_failure("cam", OSError("nope"))
    assert s.get("cam").failures == 3


def test_lifetime_failures_are_kept_even_when_the_outage_counter_resets():
    """"How bad is it right now" and "is this camera the problem" are different
    questions and need different numbers."""
    s, clock = sup()
    s.register("cam")
    for _ in range(4):
        s.on_start("cam")
        clock.advance(HEALTHY_RUN_S + 1)
        s.on_failure("cam", OSError("x"))
    assert s.get("cam").failures == 1
    assert s.get("cam").total_failures == 4


def test_switching_a_source_off_is_not_a_fault():
    s, _ = sup()
    s.register("cam")
    s.on_start("cam")
    s.on_failure("cam", OSError("x"))
    s.on_stop("cam")
    assert s.state_of("cam") == STATE_STOPPED
    assert s.get("cam").failures == 0, "the next start is a fresh attempt"
    assert not s.faults()


def test_a_generator_that_ends_is_a_fault_but_not_an_error():
    """It stopped producing, so coverage must not claim it is watching. But no
    error is invented — sending somebody to look for a message that does not
    exist wastes their evening."""
    s, _ = sup()
    s.register("net")
    s.on_start("net")
    s.on_completed("net")
    assert s.state_of("net") == STATE_COMPLETED
    assert s.get("net").last_error == ""
    assert s.get("net").total_failures == 0


# -- Silence, judged only where it means something ----------------------------

def test_a_source_with_no_declared_cadence_is_never_called_silent():
    """A PIR is quiet in an empty room and a camera is quiet while covered.
    Judging every source against one timeout would report a broken house every
    quiet evening."""
    s, clock = sup()
    s.register("pir")            # no heartbeat_s
    s.on_start("pir")
    clock.advance(86_400)
    assert s.state_of("pir") == STATE_RUNNING


def test_a_stalled_stream_is_caught_by_its_declared_cadence():
    """The hang case: a USB serial read that stops delivering without raising.
    No exception, no reconnect, and the source looks alive forever."""
    s, clock = sup()
    s.register("mmwave", heartbeat_s=30.0)
    s.on_start("mmwave")
    s.on_event("mmwave")
    clock.advance(31)
    assert s.state_of("mmwave") == STATE_SILENT
    assert s.faults()


def test_silence_is_measured_from_start_before_any_event():
    """A source that connects and then never says anything is exactly the hang
    worth catching; waiting for a first event to start the clock never would."""
    s, clock = sup()
    s.register("mmwave", heartbeat_s=30.0)
    s.on_start("mmwave")
    clock.advance(31)
    assert s.state_of("mmwave") == STATE_SILENT


def test_an_event_clears_silence():
    s, clock = sup()
    s.register("mmwave", heartbeat_s=30.0)
    s.on_start("mmwave")
    clock.advance(31)
    s.on_event("mmwave")
    assert s.state_of("mmwave") == STATE_RUNNING


# -- The supervised loop -------------------------------------------------------

class Boom:
    """A source that fails on its first `n` lives, then works."""

    def __init__(self, fail_times, seen):
        self.fail_times = fail_times
        self.seen = seen
        self.lives = 0

    def __call__(self):
        self.lives += 1
        return self

    async def events(self):
        if self.lives <= self.fail_times:
            raise OSError(f"life {self.lives} failed")
        for i in range(3):
            yield f"ev{i}"
        self.seen.append("done")


@pytest.mark.asyncio
async def test_a_crashed_source_is_restarted():
    """This is the whole point. Before supervision a source got exactly one
    life, so a camera cut off by a router reboot stayed dead until somebody
    restarted Wavr."""
    seen, got = [], []
    s, _ = sup()
    s.register("cam")
    src = Boom(2, seen)

    # Bound the test, not the product: `supervised` loops forever by design.
    # Counted on the SOURCE's lives rather than on calls, because the loop asks
    # `should_run` twice per iteration.
    await supervised("cam", src, lambda ev: got.append(ev) or _noop(), s,
                     should_run=lambda: src.lives < 3, sleep=_instant)
    assert src.lives == 3, "it kept trying after the two failures"
    assert got, "and the third life produced events"
    assert s.get("cam").total_failures == 2


async def _noop():
    return None


async def _instant(_delay):
    return None


def _capture(into, supervisor, name):
    """A `sleep` that records what the supervisor thought of `name` at the moment
    it was about to wait — the only point where a retrying state is observable
    from outside the loop."""
    async def sleep(_delay):
        into.append(supervisor.state_of(name))
    return sleep


@pytest.mark.asyncio
async def test_cancellation_actually_stops_it():
    """A supervisor that swallowed CancelledError to "keep the source alive"
    would hang shutdown — the exact failure mode where somebody force-kills the
    process and loses whatever was mid-write."""
    s, _ = sup()
    s.register("slow")

    class Forever:
        def __call__(self):
            return self

        async def events(self):
            while True:
                await asyncio.sleep(0.01)
                yield "tick"

    task = asyncio.create_task(
        supervised("slow", Forever(), lambda ev: _noop(), s, sleep=_instant))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.state_of("slow") == STATE_STOPPED


@pytest.mark.asyncio
async def test_one_broken_source_does_not_take_down_another():
    """`provider unavailable`, not `Wavr Core broken`."""
    s, _ = sup()
    s.register("bad")
    s.register("good")
    got = []

    class Good:
        calls = 0

        def __call__(self):
            self.calls += 1
            return self

        async def events(self):
            for i in range(2):
                yield i

    bad, good = Boom(99, []), Good()
    states = []

    async def run(name, src, done, sleep):
        await supervised(name, src, lambda ev: got.append((name, ev)) or _noop(),
                         s, should_run=done, sleep=sleep)

    await asyncio.gather(
        run("bad", bad, lambda: bad.lives < 3, _capture(states, s, "bad")),
        run("good", good, lambda: good.calls < 2, _instant))
    assert any(n == "good" for n, _ in got), "the healthy source still produced"
    assert bad.lives == 3, "and the broken one kept being retried"
    # Sampled mid-flight: after the loop exits, `should_run` going false reads as
    # a deliberate stop, which is exactly what it means in production.
    assert set(states) <= {STATE_RETRYING, STATE_FAILED} and states


# -- Coverage: the honesty rule this exists to enforce ------------------------

def test_a_crashed_camera_is_offline_not_ok():
    """The defect this whole unit was written for. Coverage derived health from
    the operator's switch, so a dead camera read `observing: true` and
    `precision_level: position` — the strongest claim Wavr makes, about a sensor
    that had not produced a frame in a week."""
    rows = collect_coverage(
        cameras=[{"name": "hall", "room": "hall"}],
        cameras_enabled={"hall"},
        source_health={"hall": STATE_FAILED})
    assert rows[0].health == HEALTH_OFFLINE
    assert rows[0].observing is False


def test_a_camera_that_is_merely_switched_off_is_disabled_not_broken():
    """One is a settings click and the other is a ladder."""
    rows = collect_coverage(
        cameras=[{"name": "hall", "room": "hall"}],
        cameras_enabled=set(),
        source_health={"hall": STATE_STOPPED})
    assert rows[0].health == HEALTH_DISABLED


def test_a_running_camera_is_ok():
    rows = collect_coverage(
        cameras=[{"name": "hall", "room": "hall"}],
        cameras_enabled={"hall"},
        source_health={"hall": STATE_RUNNING})
    assert rows[0].health == HEALTH_OK


def test_a_camera_mid_restart_is_not_reported_as_working():
    rows = collect_coverage(
        cameras=[{"name": "hall", "room": "hall"}],
        cameras_enabled={"hall"},
        source_health={"hall": STATE_RETRYING})
    assert rows[0].health == HEALTH_OFFLINE


def test_an_unplugged_wired_radar_stops_claiming_to_watch_its_room():
    """It used to be hard-coded `ok`: the claim came from the CONFIG naming a
    serial port, not from anything observing the radar."""
    rows = collect_coverage(serial_rooms=("kitchen",),
                            source_health={"mmwave": STATE_SILENT})
    assert rows[0].health == HEALTH_OFFLINE
    assert rows[0].observing is False


def test_a_host_row_with_no_source_behind_it_is_unknown_not_green():
    """A renamed source should show up as a gap, never as false reassurance."""
    rows = collect_coverage(network=True, source_health={})
    assert rows[0].health == HEALTH_UNKNOWN
    assert rows[0].observing is False


def test_without_observed_health_the_legacy_reading_is_preserved():
    """Callers that cannot see a SourceManager keep the old behaviour rather
    than every sensor suddenly reading as broken."""
    rows = collect_coverage(cameras=[{"name": "hall", "room": "hall"}],
                            cameras_enabled={"hall"})
    assert rows[0].health == HEALTH_OK


def test_health_mapping_is_explicit_about_the_three_cases():
    assert health_from_source_state(STATE_RUNNING, enabled=True) == HEALTH_OK
    assert health_from_source_state(STATE_FAILED, enabled=True) == HEALTH_OFFLINE
    assert health_from_source_state(STATE_RUNNING, enabled=False) == HEALTH_DISABLED
