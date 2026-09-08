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


# -- What a finding can say, as templates --------------------------------------
#
# Named constants rather than literals at the call sites, so one tuple at the
# bottom of this file is the whole of what this surface says and a test can
# require a translation for every line of it. `{age}` is filled by the renderer
# from an `age_s` argument; every other slot is a plain value.
STATE_STARTING_TEXT = "Wavr has just started and has not worked out any room yet."
STATE_NO_READING = ("Wavr has not worked out the state of any room. Nothing it "
                    "can see is producing readings.")
STATE_DEAD = ("No room reading for {age}. Treat Wavr as not running until this "
              "clears.")
STATE_STALE = ("The last room reading was {age} ago. Wavr is up but it is not "
               "learning anything new.")
STATE_FRESH = "Rooms updated {age} ago."
SENSORS_NONE = "No sensor is reporting. Wavr cannot see anything right now."
SENSORS_OFFLINE = "{n} of {total} sensors are not reporting."
SENSORS_UNKNOWN = "Wavr cannot tell whether {n} of {total} sensors are working."
SENSORS_ALL_OFF = ("Every sensor is switched off. Wavr is running and watching "
                   "nothing.")
SENSORS_OK = "{n} sensor reporting.|{n} sensors reporting."
SOURCES_STOPPED = ("{n} input stopped and Wavr is retrying."
                   "|{n} inputs stopped and Wavr is retrying.")
STORAGE_BAD = "Wavr cannot write to its database. Nothing is being recorded."
SENSING_PAUSED_TEXT = ("Sensing is paused. Wavr is running and deliberately not "
                       "watching.")
UPDATE_RUNNING = "An update is in progress."
# "are", not "is". The singular verb sat under a plural noun for as long as this
# finding existed, and nobody saw it because the finding itself was unreachable:
# it filtered connectors on a key the connector store does not have, so the list
# was always empty. Fixing the filter is what put this sentence on a screen.
EGRESS_ON = ("{n} connection to the outside is switched on."
             "|{n} connections to the outside are switched on.")
NODES_PAIRED = "{n} sensor node paired.|{n} sensor nodes paired."
CORE_SILENT = "Wavr is not answering on this machine. It may have stopped."
# The headline a CLIENT renders when it got no answer at all. Two whole
# sentences rather than one with an optional tail, so each is a key.
HEADLINE_SILENT = "Wavr — not responding"
HEADLINE_SILENT_IN_SPACE = "Wavr — not responding · {space}"


def _fill(template: str, args: dict) -> str:
    """The English a non-translating consumer reads.

    Picks the plural arm the way the frontend catalogue does — `one|many` split
    on `n` — so `wavr status`, the tray and the dashboard cannot disagree about
    which arm a given count takes.
    """
    if not template:
        return ""
    text = template
    if "|" in text:
        one, many = text.split("|", 1)
        text = one if args.get("n") == 1 else many
    if "{age}" in text and "age_s" in args:
        text = text.replace("{age}", _human(args["age_s"]))
    for key, value in args.items():
        text = text.replace("{" + key + "}", str(value))
    return text


@dataclass(frozen=True)
class Finding:
    """One thing a person is told, with the reason attached.

    `detail` is what an advanced user drills into. `text` is what everybody
    reads, and it is written as a sentence rather than a metric because "3
    sensors offline" needs a person to know whether three is all of them.

    ## Why the sentence is a template

    The Core Panel renders the worst finding with `WavrT(worst.text)`. On a
    composed sentence that makes the lookup key "3 of 5 sensors are not
    reporting" — a key with a count in it, which no catalogue can hold — so the
    wall panel's one line of prose stayed English in every language. The
    headline below has carried a `{n}` slot for exactly this reason since it was
    fixed; the findings it summarises did not.

    `text` still composes the English, because `wavr status` and the tray read
    it and translate nothing.

    A duration arrives as SECONDS under a key ending in `_s`, never as a
    humanised phrase: "3 minutes" composed here is an English fragment that
    would land in the middle of a Portuguese sentence. The renderer turns
    `age_s` into a localised `{age}` before it looks the sentence up.
    """
    key: str
    state: str
    text_template: str = ""
    # Keyword-only from here. `Finding(key, state, text, detail)` was the
    # signature for as long as this module existed, so an un-updated caller put
    # a STRING in what is now `text_args` and `_fill` raised AttributeError deep
    # inside a render. Keyword-only makes that a TypeError at the call site
    # instead: the same mistake, caught a second earlier and named.
    text_args: dict = field(default_factory=dict, kw_only=True)
    detail: str = field(default="", kw_only=True)

    @property
    def text(self) -> str:
        return _fill(self.text_template, self.text_args)


@dataclass(frozen=True)
class RuntimeStatus:
    state: str
    headline: str
    # The same sentence with `{space}` where the name goes. A translating
    # client renders THIS and substitutes locally, so the household's name for
    # their home never becomes a catalogue key. See `_headline`.
    headline_template: str = ""
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
            "headline_template": self.headline_template or self.headline,
            "space": self.space,
            "role": self.role,
            "uptime_s": self.uptime_s,
            # Named `age` rather than a timestamp: a consumer comparing a
            # timestamp needs a correct clock of its own, and a tray on a
            # machine with a skewed clock would then render a fresh reading as
            # ancient. An age is the same number everywhere.
            "last_state_age_s": self.last_state_age_s,
            "findings": [{"key": f.key, "state": f.state, "text": f.text,
                          "text_template": f.text_template,
                          "text_args": dict(f.text_args),
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
                "state", STARTING, STATE_STARTING_TEXT))
        else:
            findings.append(Finding(
                "state", ATTENTION, STATE_NO_READING,
                detail="no fusion output since this Core started"))
    elif age >= DEAD_AFTER_S:
        findings.append(Finding(
            "state", UNAVAILABLE, STATE_DEAD, text_args={"age_s": age},
            detail=f"last fusion output {age:.0f}s ago"))
    elif age >= STALE_AFTER_S:
        findings.append(Finding(
            "state", DEGRADED, STATE_STALE, text_args={"age_s": age},
            detail=f"last fusion output {age:.0f}s ago"))
    else:
        findings.append(Finding(
            "state", HEALTHY, STATE_FRESH, text_args={"age_s": age}))

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
                # "Not reporting", never "offline": a laptop is offline when
                # it is asleep, so a household reads the wire value as
                # "switched off" rather than "broken" — the opposite of what
                # it means here. The sentence eight lines down already said it
                # correctly; this one did not.
                SENSORS_NONE,
                detail=f"{len(offline)} of {len(rows)} not reporting"))
        elif offline:
            findings.append(Finding(
                "sensors", DEGRADED, SENSORS_OFFLINE,
                text_args={"n": len(offline), "total": len(rows)},
                detail=", ".join(str(r.get("sensor_id") or "?") for r in offline)))
        elif unknown:
            # Not a fault and not health. Wavr cannot tell, and saying so is the
            # whole rule this module is built on.
            findings.append(Finding(
                "sensors", DEGRADED, SENSORS_UNKNOWN,
                text_args={"n": len(unknown), "total": len(rows)},
                detail=", ".join(str(r.get("sensor_id") or "?") for r in unknown)))
        elif live == 0:
            # Reached when every row is `disabled` — nothing offline, nothing
            # unknown, and nothing watching either.
            #
            # This used to fall through to the branch below and produce
            # `HEALTHY, "0 sensors reporting."`. Read on its own that sentence
            # is even true. Rendered, it becomes a green tray icon, a green
            # chip, a green Android notification and a `wavr status` that all
            # say everything is reporting, over a house where nothing is —
            # "a healthy-looking indicator over a dead Core", reached by a
            # household using a switch the product offers them.
            #
            # PAUSED, not ATTENTION: this is not a fault, somebody turned them
            # off, and alarming about a state a person chose is how an alarm
            # becomes background noise. What it must never do is call it fine.
            findings.append(Finding(
                "sensors", PAUSED, SENSORS_ALL_OFF,
                detail=f"{len(disabled)} of {len(rows)} switched off"))
        else:
            findings.append(Finding(
                "sensors", HEALTHY, SENSORS_OK, text_args={"n": live}))

    # -- Sources: the supervisor's own view, which is about processes ---------
    if source_states:
        broken = sorted(k for k, v in source_states.items()
                        if str(v) in ("failed", "silent"))
        if broken:
            findings.append(Finding(
                "sources", DEGRADED, SOURCES_STOPPED, text_args={"n": len(broken)},
                detail=", ".join(broken)))

    if not db_ok:
        findings.append(Finding(
            "storage", ATTENTION, STORAGE_BAD,
            detail="storage check failed"))

    if sensing_paused:
        findings.append(Finding("sensing", PAUSED, SENSING_PAUSED_TEXT))

    if updating:
        findings.append(Finding("update", UPDATING, UPDATE_RUNNING))

    # -- Things that are not health, but that a person must be able to see ----
    out = [c for c in (egress_connectors or ())]
    if out:
        findings.append(Finding(
            "egress", HEALTHY, EGRESS_ON, text_args={"n": len(out)},
            detail=", ".join(str(c) for c in out)))

    n = len(list(nodes or ()))
    if n:
        findings.append(Finding("nodes", HEALTHY, NODES_PAIRED, text_args={"n": n}))

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
        headline_template=_headline(state, space_name, findings, template=True),
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


def _headline(state: str, space: str, findings, *, template: bool = False) -> str:
    """One line, which is all a tray tooltip gets.

    It names the WORST thing rather than summarising, because a tooltip that
    says "3 of 4 fine" is read as fine.

    `template=True` returns the same sentence with `{space}` where the Space's
    name goes. The browser renders THAT and substitutes locally, so a
    household's private name for their home never becomes a translation-lookup
    key — the frontend was calling `WavrT(body.headline)`, which sent
    "Wavr — running · Casa de Teste · everything reporting" through the
    catalogue and recorded the name as a missing translation, which is how a
    private name ends up in an audit. Same leak `#brandSpace` and `<title>`
    already had; this was its third door.

    The literal `headline` stays, unchanged, for the tray, the Android
    notification and `wavr status` — none of which translate anything, and all
    of which would otherwise render `{space}`.
    """
    # The TEMPLATE forms are written out as literals rather than assembled.
    #
    # A browser looks the template up in its catalogue, so the exact string has
    # to exist somewhere a key extractor can find it. Composed with an
    # f-string, it existed in no source file — the lookup could never match,
    # and the most visible sentence in the product was the one string that
    # could not be translated.
    if template and space:
        if state == HEALTHY:
            return "Wavr — running · {space} · everything reporting"
        if state == STARTING:
            return "Wavr — starting · {space}"
        if state == PAUSED:
            return "Wavr — running · {space} · sensing paused"
        if state == UPDATING:
            return "Wavr — updating · {space}"
        # The three bad states, written out whole and WITHOUT the worst
        # finding's sentence.
        #
        # This appended `worst.text` — a sentence already composed, with a
        # count inside it — so the key became "Wavr — degraded · {space} · 3 of
        # 5 sensors are not reporting." No catalogue can hold that, which left
        # the chip's tooltip English in every state except the four above: the
        # change that took a count OUT of one key had put one back into another.
        #
        # The finding is translated separately, by the same catalogue, at the
        # site that renders it — the browser appends `findingWords(worst)` to
        # this head. `headline` below still carries the whole sentence, for the
        # tray and the CLI, which translate nothing and need it composed.
        if state == DEGRADED:
            return "Wavr — degraded · {space}"
        if state == ATTENTION:
            return "Wavr — needs attention · {space}"
        if state == UNAVAILABLE:
            return "Wavr — not running · {space}"
        return "Wavr — " + str(state) + " · {space}"

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
        # With a `{space}` slot, like every other headline in this module.
        # This one had none, and `to_dict` falls back to the composed
        # `headline` when the template is empty — so the household's own name
        # for their home went into the catalogue lookup as part of the key, on
        # the one screen a person reads when nothing is answering. Same leak
        # `#brandSpace`, `<title>` and `WavrT(body.headline)` each had; this
        # was its fourth door, and it opened in the state where somebody is
        # most likely to be reading.
        headline_template=(HEADLINE_SILENT_IN_SPACE if space_name
                           else HEADLINE_SILENT),
        space=space_name or "",
        findings=(Finding(
            "core", UNAVAILABLE, CORE_SILENT,
            detail="no response from the local Core"),),
    )


# Every sentence a finding can put in front of a person, in one tuple, built
# from the constants above. One test requires a translation for each; another
# drives `assess()` over every branch and requires what comes out to be in here,
# so a finding written with an inline literal fails instead of quietly shipping
# an English sentence to a wall panel in a Portuguese house.
TEMPLATES: tuple[str, ...] = tuple(sorted({
    STATE_STARTING_TEXT, STATE_NO_READING, STATE_DEAD, STATE_STALE, STATE_FRESH,
    SENSORS_NONE, SENSORS_OFFLINE, SENSORS_UNKNOWN, SENSORS_ALL_OFF, SENSORS_OK,
    SOURCES_STOPPED, STORAGE_BAD, SENSING_PAUSED_TEXT, UPDATE_RUNNING,
    EGRESS_ON, NODES_PAIRED, CORE_SILENT,
}))
