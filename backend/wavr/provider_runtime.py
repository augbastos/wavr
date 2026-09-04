"""Keeping one broken source from becoming a broken Wavr — and, more
importantly, from becoming a *silently* broken Wavr.

## The two failures this module exists to prevent

**A source that crashes never comes back.** `SourceManager._run` catches the
exception, logs it, tears the generator down and drops the task. Nothing retries.
A camera whose RTSP connection is cut by a router reboot stays dead until
somebody restarts Wavr — which, for a product that is supposed to sit on a shelf
for months, means the first power cut quietly ends the camera's useful life.

**Worse: the screen that exists to say what Wavr can see reports it as
watching.** `sensor_coverage` derives a camera's health from whether the
operator's switch is ON, not from whether the task behind it is alive. So a dead
camera reads `health: ok`, `observing: true`, `precision_level: position` — the
strongest claim Wavr makes, about a sensor that has not produced a frame in a
week. That is precisely the class of dishonesty the rest of this codebase is
built to refuse, and it is worth being blunt: a wrong "I can see the kitchen" is
worse than no kitchen sensor at all, because the second is visible.

Supervision fixes the first. Publishing the supervisor's own view of health —
rather than the switch position — fixes the second.

## Why not simply retry forever, fast

Because several cameras usually live on the SAME box. When that box reboots,
every camera source fails within the same second; a fixed retry interval makes
them reconnect in lockstep and hammer the thing that is already struggling. So:
exponential backoff, jitter so they spread out, a cap, and after a handful of
fast attempts a much slower cold probe.

Not *giving up*, though. A permanent "failed, click here" is the wrong end state
for a home appliance: routers reboot, ISPs drop, cameras get unplugged and
plugged back in. Wavr should still be working when somebody walks past it three
days later without having touched anything. So a failed source keeps probing —
just at a rate nobody would call a storm.

## The detail that makes the counter correct

A source that ran fine for a minute before dying has its failure counter RESET.
Without that, a camera that drops once a day would accumulate failures across
unrelated incidents and eventually land in the slow-probe state permanently,
where it would recover an hour late every time. The counter is meant to measure
*this* outage, not the camera's lifetime.

## Silence is only a fault where silence is meaningful

A hung source is worse than a crashed one: it looks alive forever. But most
sources here are legitimately quiet — a PIR fires on motion, a network sweep
runs once a minute, a BLE scan reports when something is near. Judging all of
them against one silence timeout would report "the house is broken" every quiet
evening.

So silence is only judged for sources that declare a cadence they are supposed
to keep. Everything else is never called silent, because for those, silence is
not evidence of anything.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field

# Where a source is in its life. `stopped` is a choice; everything from
# `retrying` down is a fault, and the difference between them is what a person
# should do about it: nothing yet, versus look at the camera.
STATE_STOPPED = "stopped"        # not enabled — the operator's decision
STATE_RUNNING = "running"        # the task is alive
STATE_RETRYING = "retrying"      # crashed, backing off, will try again shortly
STATE_FAILED = "failed"          # crashed repeatedly; slow probe continues
STATE_COMPLETED = "completed"    # the generator ended on its own, without error
STATE_SILENT = "silent"          # alive, but past the cadence it declared

# A fault is anything that is not "off on purpose" and not "working".
FAULT_STATES: frozenset[str] = frozenset({
    STATE_RETRYING, STATE_FAILED, STATE_COMPLETED, STATE_SILENT})

# Fast recovery, for the ordinary case: a connection dropped and will come back
# in a second or two. Doubling from one second, capped so the fast phase never
# stretches past about half a minute.
BASE_BACKOFF_S = 1.0
FAST_BACKOFF_CAP_S = 30.0
FAST_ATTEMPTS = 5

# After the fast attempts, the assumption changes: this is not a blip, something
# is actually unplugged. Five minutes is slow enough that a dozen dead sources
# generate no meaningful load, and fast enough that a person who fixes the cable
# sees Wavr recover before they have finished putting the ladder away.
COLD_PROBE_S = 300.0

# Jitter spreads a simultaneous failure of several sources — the NVR-reboot case
# — so they do not all reconnect on the same tick.
JITTER = 0.25

# A run this long counts as a genuine recovery and resets the failure counter.
HEALTHY_RUN_S = 60.0

# How much of an exception message is worth keeping. Long enough to name the
# problem, short enough that a stack-trace-shaped message cannot fill a screen.
MAX_ERROR_CHARS = 200


_CREDS_IN_URL = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")
# Anything long and random-looking: a bearer token, an API key, a session id.
# Deliberately eager — a redacted error message is a small cost, and a leaked
# camera password on an unauthenticated-looking health screen is not.
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def redact(text: str) -> str:
    """Strip credentials out of text that is about to be shown or stored.

    An exception message from a camera source routinely contains the RTSP URL it
    was dialling, and that URL routinely contains a password. Health data gets
    rendered on screens, written into diagnostics bundles and read by agents —
    every one of which is a place a camera password must never appear.

    This runs on the way IN, not on the way out, so there is no path where an
    unredacted message is stored and a later reader forgets to filter it.
    """
    s = str(text or "").strip().replace("\n", " ")
    s = _CREDS_IN_URL.sub(r"\1***@", s)
    s = _LONG_SECRET.sub("***", s)
    if len(s) > MAX_ERROR_CHARS:
        s = s[:MAX_ERROR_CHARS - 1].rstrip() + "…"
    return s


def backoff_for(attempt: int, *, rand=random.random) -> float:
    """Seconds to wait before attempt number `attempt` (1-based).

    Exponential while there is reason to think it is a blip, then flat and slow.
    Jitter is multiplicative and one-sided-ish around the nominal delay so that
    two sources failing together do not stay in lockstep through the whole
    sequence.
    """
    if attempt >= FAST_ATTEMPTS:
        nominal = COLD_PROBE_S
    else:
        nominal = min(BASE_BACKOFF_S * (2 ** max(0, attempt - 1)),
                      FAST_BACKOFF_CAP_S)
    return max(0.1, nominal * (1.0 + JITTER * (2.0 * rand() - 1.0)))


@dataclass
class SourceHealth:
    """One source's health, as the supervisor actually observed it.

    Not the switch position. The distinction is the entire point of this module:
    `enabled` is what somebody asked for, this is what is happening.
    """

    name: str
    state: str = STATE_STOPPED
    # Consecutive failures in THIS outage. Reset by a healthy run, so it reads
    # as "how bad is it right now", not "how flaky has this ever been".
    failures: int = 0
    # Lifetime count, never reset. This is the one that answers "is this camera
    # the problem?" across weeks.
    total_failures: int = 0
    last_error: str = ""
    last_error_ts: float = 0.0
    started_ts: float = 0.0
    last_event_ts: float = 0.0
    events: int = 0
    # None means this source never declared a cadence, so it is never judged
    # silent — see the module docstring.
    heartbeat_s: float | None = None
    next_attempt_ts: float = 0.0
    detail: dict = field(default_factory=dict)

    def observe_silence(self, now: float) -> str:
        """The state, taking into account how long it has been quiet.

        Kept as a read rather than a background timer: a source is only
        interesting at the moment somebody asks about it, and a timer per source
        would be a lot of wakeups to answer a question nobody is asking.
        """
        if self.state != STATE_RUNNING or not self.heartbeat_s:
            return self.state
        # Before the first event, measure from start: a source that connects and
        # then never says anything is exactly the hang worth catching, and
        # waiting for a first event to start the clock would never catch it.
        since = now - (self.last_event_ts or self.started_ts or now)
        return STATE_SILENT if since > self.heartbeat_s else STATE_RUNNING

    def to_dict(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        state = self.observe_silence(now)
        out = {
            "name": self.name,
            "state": state,
            "healthy": state == STATE_RUNNING,
            "fault": state in FAULT_STATES,
            "failures": self.failures,
            "total_failures": self.total_failures,
            "events": self.events,
        }
        if self.last_error:
            out["last_error"] = self.last_error
        if state in FAULT_STATES and self.next_attempt_ts > now:
            out["retry_in_s"] = round(self.next_attempt_ts - now, 1)
        if self.last_event_ts:
            out["quiet_for_s"] = round(now - self.last_event_ts, 1)
        elif self.started_ts and state == STATE_RUNNING:
            out["quiet_for_s"] = round(now - self.started_ts, 1)
        if self.heartbeat_s:
            out["expects_event_within_s"] = self.heartbeat_s
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


class SourceSupervisor:
    """Health for every registered source, and the restart policy over it.

    Deliberately holds no asyncio state and starts no tasks: it is a policy
    object the manager consults. That keeps the restart rules testable without
    an event loop and, more usefully, keeps the manager the single place where
    tasks are actually created — two components spawning tasks for the same
    source is how a double-start bug gets written.
    """

    def __init__(self, *, clock=time.monotonic, rand=random.random):
        self._h: dict[str, SourceHealth] = {}
        self._clock = clock
        self._rand = rand

    # -- registration --------------------------------------------------------

    def _ensure(self, name: str) -> SourceHealth:
        """The row for a source, created if absent, DECLARATION UNTOUCHED.

        Separate from `register` because the lifecycle callbacks also need a row
        and must not clear one. An earlier version had `on_start` call
        `register`, which reset `heartbeat_s` to its default — so every source
        lost its declared cadence the moment it started, and the silence check
        could never fire on the one source that has it.
        """
        h = self._h.get(name)
        if h is None:
            h = SourceHealth(name=name)
            self._h[name] = h
        return h

    def register(self, name: str, *, heartbeat_s: float | None = None,
                 detail: dict | None = None) -> SourceHealth:
        h = self._ensure(name)
        h.heartbeat_s = heartbeat_s
        if detail:
            h.detail.update(detail)
        return h

    def forget(self, name: str) -> None:
        self._h.pop(name, None)

    def get(self, name: str) -> SourceHealth | None:
        return self._h.get(name)

    # -- lifecycle callbacks, driven by SourceManager -------------------------

    def on_start(self, name: str) -> None:
        h = self._ensure(name)
        h.state = STATE_RUNNING
        h.started_ts = self._clock()
        h.last_event_ts = 0.0

    def on_event(self, name: str) -> None:
        h = self._h.get(name)
        if h is None:
            return
        now = self._clock()
        h.last_event_ts = now
        h.events += 1
        # A source producing events is working, whatever it was doing before.
        # This is also where a source that recovered on its own — reconnected
        # inside its own loop, without ever raising — gets its counter cleared.
        if h.started_ts and (now - h.started_ts) >= HEALTHY_RUN_S:
            h.failures = 0

    def on_stop(self, name: str) -> None:
        """The operator switched it off, or Wavr is shutting down.

        Not a fault, and the failure counter is cleared: whatever was wrong
        before, the next start is a fresh attempt rather than a continuation of
        an outage nobody is in any more.
        """
        h = self._h.get(name)
        if h is None:
            return
        h.state = STATE_STOPPED
        h.failures = 0
        h.next_attempt_ts = 0.0

    def on_completed(self, name: str) -> float | None:
        """The generator ended without raising.

        Treated as a fault for a source that is supposed to run continuously —
        it stopped producing, and coverage must not claim it is watching — but
        NOT as a crash: no error is recorded, because there was none, and
        inventing one would send somebody looking for a problem that has no
        message attached to it.
        """
        h = self._h.get(name)
        if h is None:
            return None
        delay = self._schedule(h, count_failure=False)
        # Set AFTER scheduling: `_schedule` picks the retrying/failed pair for a
        # crash, and a clean end deserves its own name however many times it
        # happens. Calling it "failed" on the fifth clean end would send somebody
        # hunting for an error that was never raised.
        h.state = STATE_COMPLETED
        return delay

    def on_failure(self, name: str, exc: BaseException) -> float | None:
        """A source raised. Returns how long to wait before trying again.

        The exception text is redacted here rather than at the point it is
        rendered, so no future reader of `last_error` has to remember to.
        """
        h = self._h.get(name)
        if h is None:
            return None
        now = self._clock()
        ran_for = now - h.started_ts if h.started_ts else 0.0
        if ran_for >= HEALTHY_RUN_S:
            # This outage is new. See the module docstring: the counter measures
            # the current incident, not the source's history.
            h.failures = 0
        h.last_error = redact(f"{type(exc).__name__}: {exc}")
        h.last_error_ts = now
        return self._schedule(h, count_failure=True)

    def _schedule(self, h: SourceHealth, *, count_failure: bool) -> float:
        if count_failure:
            h.failures += 1
            h.total_failures += 1
        else:
            h.failures += 1
        delay = backoff_for(h.failures, rand=self._rand)
        h.state = STATE_FAILED if h.failures >= FAST_ATTEMPTS else STATE_RETRYING
        h.next_attempt_ts = self._clock() + delay
        return delay

    # -- reads ---------------------------------------------------------------

    def all(self) -> list[SourceHealth]:
        return [self._h[k] for k in sorted(self._h)]

    def state_of(self, name: str) -> str:
        h = self._h.get(name)
        return h.observe_silence(self._clock()) if h else STATE_STOPPED

    def healthy(self, name: str) -> bool:
        return self.state_of(name) == STATE_RUNNING

    def faults(self) -> list[SourceHealth]:
        now = self._clock()
        return [h for h in self.all() if h.observe_silence(now) in FAULT_STATES]

    def to_dict(self) -> dict:
        now = self._clock()
        rows = [h.to_dict(now) for h in self.all()]
        return {
            "sources": rows,
            "faults": [r["name"] for r in rows if r["fault"]],
            "note": ("State is what the supervisor observed, not what the "
                     "switch says. A source that is enabled but not running is "
                     "a fault, and Wavr keeps trying to bring it back."),
        }


async def supervised(name: str, factory, on_event, supervisor: SourceSupervisor,
                     *, should_run=lambda: True, sleep=asyncio.sleep) -> None:
    """Run one source forever, restarting it on failure.

    Kept out of `SourceManager` so the retry policy can be tested against a fake
    clock and a fake source without an app, a network or real waiting. The
    manager still owns the task.

    Cancellation propagates untouched: a stop must actually stop, and a
    supervisor that swallowed `CancelledError` to "keep the source alive" would
    make shutdown hang — the exact failure mode where somebody force-kills the
    process and loses whatever was mid-write.
    """
    while should_run():
        supervisor.on_start(name)
        agen = None
        delay: float | None = None
        try:
            agen = factory().events()
            async for ev in agen:
                supervisor.on_event(name)
                await on_event(ev)
        except asyncio.CancelledError:
            supervisor.on_stop(name)
            raise
        except Exception as exc:                       # noqa: BLE001
            # Deliberately broad: the whole contract of this function is that an
            # arbitrary third-party source may fail in an arbitrary way without
            # taking anything else down. A narrower catch here would mean some
            # class of provider bug still stops Wavr.
            delay = supervisor.on_failure(name, exc)
        else:
            delay = supervisor.on_completed(name)
        finally:
            if agen is not None:
                try:
                    await agen.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:                      # noqa: BLE001
                    pass       # teardown of an already-failed source; the
                               # failure that matters was recorded above
        if not should_run():
            break
        await sleep(delay if delay is not None else BASE_BACKOFF_S)
    supervisor.on_stop(name)
