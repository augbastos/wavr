"""Is Wavr running, and is it actually doing anything? — answered for a person.

## NO INVISIBLE SUCCESS

If Wavr is performing an important persistent function, somebody must be able to
perceive its status without Task Manager, a terminal, a log file, developer
tools, a port or an internal endpoint.

The failure this exists to prevent is specific, and it is the worst one this
product can have: a Core that died three days ago, a dashboard nobody opened,
and a household that believed their home was being watched over. Every hour of
that is a silent lie told by a system whose entire pitch is explainability.

## The rule that shapes every line below

**A healthy-looking indicator over a dead Core is worse than no indicator.**

So every conclusion here is derived from something OBSERVED, never from
something assumed. "The process is running" is not health — a process can be up
with every source dead, and that is exactly the state `sensor_coverage` had to
stop reporting as watching. What counts is whether state has actually been
produced recently.

That makes freshness the spine of this module rather than a field on the side:

  * `last_state_at` is when fusion last produced a room reading.
  * Past `STALE_AFTER_S`, the answer is DEGRADED no matter how many components
    call themselves fine.
  * Past `DEAD_AFTER_S`, it is CORE_UNAVAILABLE, and a caller that cannot reach
    this endpoint at all must render the same thing.

The last part is why the states are ordered and why the worst one wins. A tray
icon that stays green because four checks passed and one silently stopped
reporting is the bug, not the feature.

## What this module does not do

It measures nothing. Every input is a fact some other module already
established — coverage from `sensor_coverage`, source states from the
supervisor, role from `core_registry`, connectors from the connector store. A
second measurement path would be a second thing to keep correct, and the two
would eventually disagree in front of a person who has no way to tell which is
right.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# The states a person is shown. Ordered worst-last so a caller can take the
# maximum and never accidentally report the better of two answers.
STARTING = "starting"
HEALTHY = "healthy"
UPDATING = "updating"
PAUSED = "paused"
DEGRADED = "degraded"
ATTENTION = "attention"
UNAVAILABLE = "unavailable"

# STARTING is deliberately NOT in this ladder. It is a PHASE, not a severity —
# a Core thirty seconds old with one healthy sensor is starting, not healthy,
# and putting it in the ladder made the sensor's HEALTHY outrank it. Phases and
# severities do not compare, and pretending they do produces the wrong answer in
# the one window where a person is most likely to be watching.
SEVERITY = (HEALTHY, UPDATING, PAUSED, DEGRADED, ATTENTION, UNAVAILABLE)

# A Core that has produced no room state for this long is not healthy, whatever
# else it says. Deliberately generous: fusion emits on change, and a still house
# at 4am legitimately produces nothing for a while. What it is NOT is a
# heartbeat — a heartbeat proves the loop runs, and this asks whether the loop
# is producing anything, which is the question a household cares about.
STALE_AFTER_S = 900.0        # 15 minutes: something is wrong
DEAD_AFTER_S = 3600.0        # an hour: treat as not running

# Below this, "it is still starting" is the honest answer rather than "degraded".
# A Core that has been up for ten seconds with no reading yet is not broken.
STARTING_UNTIL_S = 90.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_s(ts, *, now=None) -> float | None:
    """Seconds since `ts`, or None when there is no honest answer.

    None rather than a large number: "never happened" and "happened a long time
    ago" are different facts, and rendering both as a big age would let a Core
    that has NEVER produced a reading look like one that produced one last week.
    """
    if not ts:
        return None
    try:
        when = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or _now()) - when).total_seconds())


@dataclass(frozen=True)
class Finding:
    """One thing a person is told, with the reason attached.

    `detail` is what an advanced user drills into. `text` is what everybody
    reads, and it is written as a sentence rather than a metric because "3
    sensors offline" needs a person to know whether three is all of them.
    """
    key: str
    state: str
    text: str
    detail: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    state: str
    headline: str
    space: str = ""
    findings: tuple[Finding, ...] = ()
    uptime_s: float | None = None
    last_state_age_s: float | None = None
    role: str = ""
    protocol: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "headline": self.headline,
            "space": self.space,
            "role": self.role,
            "uptime_s": self.uptime_s,
            # Named `age` rather than a timestamp: a consumer comparing a
            # timestamp needs a correct clock of its own, and a tray on a
            # machine with a skewed clock would then render a fresh reading as
            # ancient. An age is the same number everywhere.
            "last_state_age_s": self.last_state_age_s,
            "findings": [{"key": f.key, "state": f.state, "text": f.text,
                          "detail": f.detail} for f in self.findings],
            **self.extra,
        }


def _worst(states) -> str:
    worst = HEALTHY
    for s in states:
        if SEVERITY.index(s) > SEVERITY.index(worst):
            worst = s
    return worst


def assess(*, uptime_s=None, last_state_at=None, space_name="", role="",
           coverage_rows=(), source_states=None, nodes=(), egress_connectors=(),
           sensing_paused=False, updating=False, db_ok=True, now=None) -> RuntimeStatus:
    """One answer, in a person's words, from facts other modules established.

    Every argument is optional and every absence is treated as "cannot tell"
    rather than "fine" — a caller that forgets to pass coverage should not
    receive a clean bill of health for the sensors.
    """
    now = now or _now()
    findings: list[Finding] = []
    age = _age_s(last_state_at, now=now)
    starting = uptime_s is not None and uptime_s < STARTING_UNTIL_S

    # -- The spine: has this Core produced anything? --------------------------
    if age is None:
        if starting:
            findings.append(Finding(
                "state", STARTING,
                "Wavr has just started and has not worked out any room yet."))
        else:
            findings.append(Finding(
                "state", ATTENTION,
                "Wavr has not worked out the state of any room. Nothing it can "
                "see is producing readings.",
                "no fusion output since this Core started"))
    elif age >= DEAD_AFTER_S:
        findings.append(Finding(
            "state", UNAVAILABLE,
            f"No room reading for {_human(age)}. Treat Wavr as not running "
            f"until this clears.",
            f"last fusion output {age:.0f}s ago"))
    elif age >= STALE_AFTER_S:
        findings.append(Finding(
            "state", DEGRADED,
            f"The last room reading was {_human(age)} ago. Wavr is up but it is "
            f"not learning anything new.",
            f"last fusion output {age:.0f}s ago"))
    else:
        findings.append(Finding(
            "state", HEALTHY,
            f"Rooms updated {_human(age)} ago."))

    # -- Sensors, counted from what OBSERVES them, never from a switch --------
    rows = list(coverage_rows or ())
    if rows:
        # `sensor_coverage` emits ok / offline / disabled / UNKNOWN, and its own
        # docstring says a row whose source is missing from the manager "reads
        # `unknown`, never green, so a renamed source shows up as a gap instead
        # of false reassurance". Subtracting only offline and disabled swept
        # `unknown` into the live count, so the coverage screen said "NOT being
        # watched" while the tray said "everything reporting" about the same
        # sensor.
        offline = [r for r in rows if str(r.get("health")) in ("offline", "silent",
                                                               "failed")]
        disabled = [r for r in rows if str(r.get("health")) == "disabled"]
        unknown = [r for r in rows if str(r.get("health")) == "unknown"]
        live = len(rows) - len(offline) - len(disabled) - len(unknown)
        if offline and live == 0:
            findings.append(Finding(
                "sensors", ATTENTION,
                "Every sensor is offline. Wavr cannot see anything right now.",
                f"{len(offline)} of {len(rows)} not reporting"))
        elif offline:
            findings.append(Finding(
                "sensors", DEGRADED,
                f"{len(offline)} of {len(rows)} sensors are not reporting.",
                ", ".join(str(r.get("sensor_id") or "?") for r in offline)))
        elif unknown:
            # Not a fault and not health. Wavr cannot tell, and saying so is the
            # whole rule this module is built on.
            findings.append(Finding(
                "sensors", DEGRADED,
                f"Wavr cannot tell whether {len(unknown)} of {len(rows)} "
                f"sensors are working.",
                ", ".join(str(r.get("sensor_id") or "?") for r in unknown)))
        else:
            findings.append(Finding(
                "sensors", HEALTHY,
                f"{live} sensor{'' if live == 1 else 's'} reporting."))

    # -- Sources: the supervisor's own view, which is about processes ---------
    if source_states:
        broken = sorted(k for k, v in source_states.items()
                        if str(v) in ("failed", "silent"))
        if broken:
            findings.append(Finding(
                "sources", DEGRADED,
                f"{len(broken)} input{'' if len(broken) == 1 else 's'} stopped "
                f"and Wavr is retrying.",
                ", ".join(broken)))

    if not db_ok:
        findings.append(Finding(
            "storage", ATTENTION,
            "Wavr cannot write to its database. Nothing is being recorded.",
            "storage check failed"))

    if sensing_paused:
        findings.append(Finding(
            "sensing", PAUSED,
            "Sensing is paused. Wavr is running and deliberately not watching."))

    if updating:
        findings.append(Finding("update", UPDATING, "An update is in progress."))

    # -- Things that are not health, but that a person must be able to see ----
    out = [c for c in (egress_connectors or ())]
    if out:
        findings.append(Finding(
            "egress", HEALTHY,
            f"{len(out)} connection{'' if len(out) == 1 else 's'} to the "
            f"outside is switched on.",
            ", ".join(str(c) for c in out)))

    n = len(list(nodes or ()))
    if n:
        findings.append(Finding("nodes", HEALTHY,
                                f"{n} sensor node{'' if n == 1 else 's'} paired."))

    state = _worst(f.state for f in findings if f.state != STARTING)         if findings else STARTING
    # A Core that came up ten seconds ago has not failed; it has not finished.
    # Saying "attention required" there trains people to ignore the indicator.
    # UNAVAILABLE still wins: a broken database during startup is worth showing
    # immediately, and it will not clear by waiting.
    if starting and age is None and state != UNAVAILABLE:
        state = STARTING

    return RuntimeStatus(
        state=state,
        headline=_headline(state, space_name, findings),
        space=space_name or "",
        role=role or "",
        uptime_s=uptime_s,
        last_state_age_s=age,
        findings=tuple(findings),
    )


def _human(seconds: float) -> str:
    """A duration the way somebody says it out loud."""
    s = int(seconds)
    if s < 60:
        return f"{s} second{'' if s == 1 else 's'}"
    if s < 3600:
        m = s // 60
        return f"{m} minute{'' if m == 1 else 's'}"
    if s < 86400:
        h = s // 3600
        return f"{h} hour{'' if h == 1 else 's'}"
    d = s // 86400
    return f"{d} day{'' if d == 1 else 's'}"


def _headline(state: str, space: str, findings) -> str:
    """One line, which is all a tray tooltip gets.

    It names the WORST thing rather than summarising, because a tooltip that
    says "3 of 4 fine" is read as fine.
    """
    where = f" · {space}" if space else ""
    if state == HEALTHY:
        return f"Wavr — running{where} · everything reporting"
    if state == STARTING:
        return f"Wavr — starting{where}"
    if state == PAUSED:
        return f"Wavr — running{where} · sensing paused"
    if state == UPDATING:
        return f"Wavr — updating{where}"
    worst = next((f for f in findings if f.state == state), None)
    label = {DEGRADED: "degraded", ATTENTION: "needs attention",
             UNAVAILABLE: "not running"}.get(state, state)
    return f"Wavr — {label}{where}" + (f" · {worst.text}" if worst else "")


def unreachable(space_name: str = "") -> RuntimeStatus:
    """What a CLIENT renders when it cannot reach the Core at all.

    Here rather than in each client, so a tray, a menu bar and a browser tab
    cannot disagree about what "I got no answer" means — and so none of them can
    quietly fall back to the last good state, which is the exact way a dead Core
    goes on looking alive.
    """
    return RuntimeStatus(
        state=UNAVAILABLE,
        headline=(f"Wavr — not responding"
                  + (f" · {space_name}" if space_name else "")),
        space=space_name or "",
        findings=(Finding(
            "core", UNAVAILABLE,
            "Wavr is not answering on this machine. It may have stopped.",
            "no response from the local Core"),),
    )
