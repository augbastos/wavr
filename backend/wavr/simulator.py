"""Scripted scenarios, so an experience can be built without owning a radar.

## The problem

Somebody writing an application against Wavr needs a room that becomes occupied,
a count that appears and disappears, a sensor that goes offline, a precision
level that degrades. Getting those from real hardware means owning a radar, a
camera, a UWB anchor and an XR headset, and then physically walking around a
house every time they change a line of code.

That is the whole barrier to anybody building on this platform, and it is
removable: Wavr's fusion already accepts events from anywhere, so a scenario is
just a scripted sequence of them.

## The rule that makes this safe

**Simulated evidence is labelled at the source and stays labelled.**

Every event a scenario produces carries a `sim:` sensor id. That id travels
through fusion into `sources[]`, into `sensor_coverage`, into the events stream
and onto the dashboard — so there is no surface where a simulated room can be
mistaken for a real one, and no way to turn the labelling off short of editing
this file.

This matters more than it first appears. A simulator that produced
indistinguishable events would be a machine for generating convincing false
claims about a house — exactly the thing this codebase spends most of its effort
refusing to do. It would also, eventually, be used to make a demo look better
than the product.

## Deterministic

A scenario is a list of `(offset_seconds, event)` built from a fixed start time.
The same scenario produces byte-identical events every run, which is what lets a
developer write an assertion against one and what lets a bug report say "run
`occupancy_arrives` and watch step 4".

## What a scenario is NOT allowed to do

It cannot produce a state fusion would refuse to produce. Scenarios build
`SensingEvent`s and hand them to the real engine; there is no path here that
writes a RoomState directly. So a scenario cannot demonstrate a count from a PIR,
or a position from a Wi-Fi scan, because fusion would not allow it — and a
simulator able to show a capability the product does not have is a sales tool,
not a development one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from wavr.events import SensingEvent, Target

# Every simulated sensor id starts with this. Checked by `is_simulated`, which is
# what the dashboard, the coverage surface and the event stream all use — so the
# label cannot be lost by one of them forgetting the convention.
SIM_PREFIX = "sim:"

# A fixed start, so two runs of the same scenario produce identical timestamps.
# Real wall-clock time would make every scenario un-assertable.
EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def is_simulated(sensor_id: str) -> bool:
    return str(sensor_id or "").startswith(SIM_PREFIX)


def sim_id(name: str) -> str:
    return f"{SIM_PREFIX}{name}"


@dataclass(frozen=True)
class Step:
    """One moment in a scenario: an event, and how far into the story it lands."""

    at_s: float
    event: SensingEvent
    note: str = ""

    def to_dict(self) -> dict:
        return {"at_s": self.at_s, "note": self.note,
                "room": self.event.room, "modality": self.event.modality,
                "sensor_id": self.event.sensor_id,
                "presence": self.event.presence, "count": self.event.count}


@dataclass(frozen=True)
class Scenario:
    """A named story a developer can run against their application."""

    key: str
    title: str
    description: str
    steps: tuple[Step, ...] = ()
    # What this scenario is FOR — the thing a developer is trying to make their
    # application handle. Published so the list reads as a set of problems rather
    # than a set of button labels.
    teaches: str = ""
    # The end state this scenario CLAIMS to produce, per room, as a subset of the
    # RoomState fields. Checked by a test against the real engine.
    #
    # This field exists because three of these scenarios were wrong when first
    # written: they described an outcome fusion correctly does not produce on
    # that timescale. A room does not go vacant the moment a camera reports
    # empty — there is a 45-second dwell, deliberately, so one dropped frame
    # cannot fire a "nobody home" automation on somebody sitting still. A
    # scenario that ignored the dwell would have taught a developer to expect
    # something the product does not do, which is a worse lie than a missing
    # scenario.
    expects: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return max((s.at_s for s in self.steps), default=0.0)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title,
                "description": self.description, "teaches": self.teaches,
                "duration_s": self.duration_s,
                "expects": dict(self.expects),
                "steps": [s.to_dict() for s in self.steps]}


def _ev(at_s: float, *, room: str, sensor: str, modality: str, presence: bool,
        count=None, targets=(), confidence=None, note: str = "") -> Step:
    return Step(
        at_s=at_s, note=note,
        event=SensingEvent(
            sensor_id=sim_id(sensor),
            room=room, modality=modality, presence=presence,
            motion=0.0, breathing_bpm=None, heart_bpm=None,
            confidence=(0.0 if not presence
                        else (0.9 if confidence is None else confidence)),
            ts=(EPOCH + timedelta(seconds=at_s)).isoformat(),
            targets=tuple(targets), count=count))


def _camera(at_s, room, presence, count=None, targets=(), sensor="camera-1",
            note=""):
    return _ev(at_s, room=room, sensor=sensor, modality="camera",
               presence=presence, count=count, targets=targets, note=note)


def _radar(at_s, room, presence, count=None, sensor="radar-1", note=""):
    return _ev(at_s, room=room, sensor=sensor, modality="mmwave",
               presence=presence, count=count, note=note)


def _pir(at_s, room, presence, sensor="pir-1", note=""):
    return _ev(at_s, room=room, sensor=sensor, modality="pir",
               presence=presence, note=note)


# -- The scenarios -------------------------------------------------------------
#
# Each one exists because an application handles it badly by default. They are
# not a tour of the feature set; they are the situations that break naive code.

def _occupancy_arrives() -> Scenario:
    return Scenario(
        key="occupancy_arrives",
        title="A room becomes occupied, then empties",
        description=("An empty kitchen, somebody arrives, and — after the vacate "
                     "dwell has run — the room reads empty again."),
        teaches=("The simplest case, and the one that teaches the dwell. The "
                 "camera reports empty at 30s and the room STAYS occupied for "
                 "another 45, because one dropped frame must not fire a 'nobody "
                 "home' automation on somebody sitting still. Your application "
                 "should react to the change Wavr publishes, not to the reading "
                 "it can see underneath it."),
        steps=(
            _camera(0, "kitchen", False, 0, note="empty, and known to be empty"),
            _camera(5, "kitchen", True, 1, note="somebody walks in"),
            _camera(10, "kitchen", True, 1),
            _camera(30, "kitchen", False, 0, note="they leave; the dwell starts"),
            _camera(80, "kitchen", False, 0, note="dwell elapsed — now vacant"),
        ),
        expects={"kitchen": {"occupied": False, "person_count": None}})


def _count_appears_and_vanishes() -> Scenario:
    return Scenario(
        key="count_appears_and_vanishes",
        title="A headcount arrives, then stops being available",
        description=("A camera counts two people, then goes quiet. Presence "
                     "survives on a PIR; the COUNT does not."),
        teaches=("The case that catches almost everybody: your application is "
                 "showing '2 people' and the count becomes unknown. It must stop "
                 "showing the number — not keep it, and not replace it with 0. "
                 "Note the timing: Wavr HOLDS the last count while the camera "
                 "decays, and only lets go once that evidence is stale. Both "
                 "halves are deliberate, and your application will see both."),
        steps=(
            _pir(0, "living", True, note="something is in the room"),
            _camera(2, "living", True, 2, note="and now we can count"),
            _pir(20, "living", True, note="camera stops reporting; PIR carries on"),
            _pir(60, "living", True,
                 note="camera decaying — the count is STILL held here"),
            _pir(110, "living", True,
                 note="camera past stale; the count is genuinely unknown now"),
        ),
        expects={"living": {"occupied": True, "person_count": None,
                            "precision_level": "room"}})


def _precision_improves() -> Scenario:
    return Scenario(
        key="precision_improves",
        title="A room gets better at answering",
        description=("Presence only, then a radar joins and the room can count, "
                     "then a calibrated camera places people within it."),
        teaches=("Capabilities are not fixed. An application that reads them "
                 "once at startup will be wrong for the rest of the day."),
        steps=(
            _pir(0, "office", True, note="room scope: somebody is here"),
            _radar(10, "office", True, 1, note="count scope: exactly one"),
            _camera(20, "office", True, 1,
                    targets=(Target(id=1, x=1.2, y=0.8, confidence=0.9),),
                    note="position scope: and roughly where"),
        ),
        expects={"office": {"occupied": True, "precision_level": "position"}})


def _precision_degrades() -> Scenario:
    return Scenario(
        key="precision_degrades",
        title="A room gets worse at answering",
        description="The reverse: the camera drops out, then the radar.",
        teaches=("The direction applications forget. Losing a capability is a "
                 "real event, and 'no answer' is not 'no'. The last two steps "
                 "are far apart on purpose — evidence decays rather than "
                 "vanishing, so a capability is lost gradually and your "
                 "application sees the middle of that."),
        steps=(
            _camera(0, "office", True, 1,
                    targets=(Target(id=1, x=1.2, y=0.8, confidence=0.9),)),
            _radar(15, "office", True, 1, note="camera gone; count survives"),
            _pir(35, "office", True, note="radar quiet; PIR carries the room"),
            _pir(130, "office", True,
                 note="camera and radar both stale — presence only, no count"),
        ),
        expects={"office": {"occupied": True, "person_count": None,
                            "precision_level": "room"}})


def _sensors_disagree() -> Scenario:
    return Scenario(
        key="sensors_disagree",
        title="Two sensors contradict each other",
        description=("A camera says the room is empty while a radar says "
                     "somebody is there — the classic very-still-person case."),
        teaches=("Confidence is not certainty. Your application should be able "
                 "to show that Wavr is unsure, rather than picking a side and "
                 "presenting it as fact."),
        steps=(
            _camera(0, "bedroom", True, 1),
            _radar(1, "bedroom", True, 1),
            _camera(20, "bedroom", False, 0,
                    note="camera loses a person who stopped moving"),
            _radar(21, "bedroom", True, 1, note="radar still sees them"),
        ),
        # The room stays occupied, and that IS the lesson: a report of ABSENCE
        # carries no mass into the merge, because a camera failing to see a
        # still person is much weaker evidence than one seeing an empty room.
        expects={"bedroom": {"occupied": True, "person_count": 1}})


def _sensor_goes_offline() -> Scenario:
    return Scenario(
        key="sensor_goes_offline",
        title="The only sensor in a room stops reporting",
        description=("A radar reports, then goes silent. Nothing replaces it, "
                     "so the room fades to unknown rather than to empty."),
        teaches=("An unobserved room is not an empty room. If your application "
                 "turns lights off when a room reads unoccupied, this is the "
                 "scenario that turns them off on somebody."),
        steps=(
            _radar(0, "hall", True, 1),
            _radar(5, "hall", True, 1),
            # Nothing after this: the silence IS the scenario.
        ),
        # Right after the last step the room is still occupied, because nothing
        # has advanced the clock. The FADE arrives afterwards, from the Core's
        # periodic re-fuse tick ageing the radar out -- so run this one in
        # realtime mode and watch, rather than expecting the instant replay to
        # show it. Declaring the instant outcome honestly is better than
        # pretending a compressed timeline can demonstrate a timeout.
        expects={"hall": {"occupied": True, "person_count": 1}})


def _two_rooms_change_together() -> Scenario:
    return Scenario(
        key="two_rooms_change_together",
        title="One room empties as another fills",
        description=("The kitchen goes empty and the hall goes occupied a few "
                     "seconds later."),
        teaches=("It LOOKS like somebody walked from one to the other, and Wavr "
                 "will not say that — there is no `person.entered_room` event. "
                 "If your application needs to claim it, that is your inference "
                 "to make and your inference to be wrong about."),
        steps=(
            _camera(0, "kitchen", True, 1),
            _camera(10, "kitchen", False, 0,
                    note="kitchen reads empty; its vacate dwell starts"),
            _pir(13, "hall", True, note="hall fills, three seconds later"),
            _camera(60, "kitchen", False, 0,
                    note="dwell elapsed — the kitchen is now genuinely vacant"),
        ),
        expects={"kitchen": {"occupied": False},
                 "hall": {"occupied": True, "person_count": None}})


def _crowded_room() -> Scenario:
    return Scenario(
        key="crowded_room",
        title="A count that keeps changing",
        description="Two, then four, then one.",
        teaches=("Rendering. An application that animates every count change "
                 "will thrash; one that debounces will lag. This is where you "
                 "decide which."),
        steps=(
            _camera(0, "living", True, 2),
            _camera(5, "living", True, 4),
            _camera(9, "living", True, 3),
            _camera(12, "living", True, 1),
        ),
        expects={"living": {"occupied": True, "person_count": 1}})


SCENARIOS: dict[str, Scenario] = {
    s.key: s for s in (
        _occupancy_arrives(),
        _count_appears_and_vanishes(),
        _precision_improves(),
        _precision_degrades(),
        _sensors_disagree(),
        _sensor_goes_offline(),
        _two_rooms_change_together(),
        _crowded_room(),
    )
}


def list_scenarios() -> dict:
    return {
        "scenarios": [s.to_dict() for s in SCENARIOS.values()],
        "note": ("Every event a scenario produces carries a `sim:` sensor id, "
                 "all the way through fusion to the dashboard. Simulated "
                 "evidence is never indistinguishable from real evidence."),
    }


def get(key: str) -> Scenario | None:
    return SCENARIOS.get(key)


class SimulationRun:
    """One scenario being played, and the record of what it produced.

    Holds the scenario and a cursor. Deliberately NOT a task: whether the steps
    are fed in real time or all at once is the caller's decision, and a component
    that owned a timer here would make the deterministic path (feed everything,
    assert the result) the awkward one.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.index = 0
        self.started = False

    @property
    def finished(self) -> bool:
        return self.index >= len(self.scenario.steps)

    def next_step(self) -> Step | None:
        if self.finished:
            return None
        step = self.scenario.steps[self.index]
        self.index += 1
        self.started = True
        return step

    def remaining(self) -> tuple[Step, ...]:
        return self.scenario.steps[self.index:]

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario.key,
            "title": self.scenario.title,
            "step": self.index,
            "steps_total": len(self.scenario.steps),
            "finished": self.finished,
            "next": (self.scenario.steps[self.index].to_dict()
                     if not self.finished else None),
        }


def remap(scenario: Scenario, rooms) -> tuple[Scenario, dict]:
    """Rewrite a scenario to use the rooms this Space actually has.

    A scenario names `kitchen`, `living`, `office`. A real Space might call them
    `sala`, `quarto`, `quintal` — and running the scenario unchanged conjures
    rooms that are not on anybody's floor plan, so a developer watches their
    dashboard grow a "kitchen" they have never had. Watching YOUR kitchen become
    occupied is the demonstration; watching an invented one is a puzzle.

    Deterministic: the same scenario against the same room list always produces
    the same mapping, because a scenario whose rooms moved between runs would be
    useless to assert against.

    Returns the rewritten scenario and the mapping, which the caller publishes —
    a silent rename would leave somebody looking at the wrong card.
    """
    wanted = rooms_touched(scenario)
    available = [str(r) for r in (rooms or []) if str(r).strip()]
    if not available:
        # No floor plan yet. Keep the scenario's own names rather than refusing:
        # a developer with a fresh install is exactly who most needs this.
        return scenario, {}

    mapping = {name: available[i % len(available)]
               for i, name in enumerate(wanted)}
    if all(k == v for k, v in mapping.items()):
        return scenario, {}

    steps = tuple(
        Step(at_s=s.at_s, note=s.note,
             event=_replace_room(s.event, mapping[s.event.room]))
        for s in scenario.steps)
    moved = Scenario(
        key=scenario.key, title=scenario.title, description=scenario.description,
        steps=steps, teaches=scenario.teaches,
        expects={mapping.get(r, r): v for r, v in scenario.expects.items()})
    return moved, mapping


def _replace_room(event: SensingEvent, room: str) -> SensingEvent:
    """The same event, in a different room.

    `SensingEvent` is a frozen dataclass; building a new one keeps every other
    field — including the `sim:` sensor id — exactly as it was, which is the
    property that matters here.
    """
    from dataclasses import replace
    return replace(event, room=room)


def rooms_touched(scenario: Scenario) -> list[str]:
    """Which rooms a scenario will write into.

    Published before a run so an operator can see it is about to put simulated
    people in their actual kitchen. That is a reasonable thing to do on a
    developer machine and an alarming one on a real install, and the difference
    is whether anybody was told.
    """
    return sorted({s.event.room for s in scenario.steps})


def sensors_used(scenario: Scenario) -> list[str]:
    return sorted({s.event.sensor_id for s in scenario.steps})
