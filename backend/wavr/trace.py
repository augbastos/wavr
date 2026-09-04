"""Record what the sensors said, replay it, get the same answer every time.

## Why this is engineering infrastructure and not a debug toy

A fusion bug in a real house is nearly impossible to investigate. It depends on
which sensors were fresh, in what order they spoke, how stale each one was, and
what the latch was holding — none of which survives the moment. Today the only
way to study one is to reproduce the physical situation, which for "the radar
drops a still person while the PIR false-trips" means standing very still in a
kitchen for four minutes.

A trace turns that into a file. The same file, replayed twice, must produce the
same room states — otherwise it is a recording, not a reproduction.

## What makes replay deterministic

The clock. `FusionEngine` ages every source against a reference time, and a
replay that used the wall clock would decay the whole trace to nothing on the
second run. `replay()` therefore drives the engine from the TRACE's own
timestamps, so a recording made last Tuesday behaves on replay exactly as it did
last Tuesday.

## Sanitisation is not optional decoration

A trace is the most sensitive artefact Wavr can produce: it is a minute-by-minute
record of where people were. The whole point of being able to share one — to file
a bug, to build a test fixture, to let somebody develop against realistic data —
requires that it carry no person in it.

`sanitize()` removes identity labels and vital signs outright. It does NOT remove
coordinates by default, because a positional bug cannot be reproduced without
positions; `sanitize(drop_positions=True)` is there for when the trace is going
somewhere the author does not control. Both modes stamp what was removed, so a
recipient can tell which they were given rather than assuming.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from wavr.contracts import version
from wavr.events import Identity, SensingEvent, Target

# Bumped when the recorded shape changes in a way an older reader would
# misinterpret. A trace that cannot say what it is becomes unreadable the first
# time the event shape moves. Owned by `contracts` with every other published
# shape, so no two of them can disagree about what "version 1" means.
TRACE_VERSION = version("trace")

# Everything a sanitised trace guarantees is gone. Named so a recipient can
# check the promise rather than trust it.
SANITIZED_ALWAYS = ("identities", "breathing_bpm", "heart_bpm")


class TraceError(ValueError):
    """A trace that cannot be read or trusted."""


def _event_to_row(event: SensingEvent) -> dict:
    return {"t": event.ts, "event": event.to_dict()}


def _rebuild(cls, rows):
    """Rows back into the dataclass the engine expects.

    Both targets and identities go through this. They used to be handled
    differently — targets rebuilt, identities left as raw dicts — and replaying a
    trace with any identity in it crashed fusion on `ident.to_dict()`. One
    function for both removes the asymmetry that allowed that.

    Unknown keys are dropped rather than raising: a trace written by a newer
    Wavr may carry fields this one does not have, and refusing the whole event
    over an extra key would make traces unshareable across versions for no
    safety benefit.
    """
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append(cls(**{k: v for k, v in row.items()
                          if k in cls.__annotations__}))
    return tuple(out)


def _row_to_event(row: dict) -> SensingEvent:
    raw = dict(row.get("event") or {})
    targets = _rebuild(Target, raw.pop("targets", None))
    identities = _rebuild(Identity, raw.pop("identities", None))
    known = {k: v for k, v in raw.items() if k in SensingEvent.__annotations__}
    return SensingEvent(**known, targets=targets, identities=identities)


class TraceRecorder:
    """Captures events as they are ingested.

    Bounded on purpose. An unbounded recorder left on by accident fills a
    Raspberry Pi's card with a movement log of somebody's home, which is the
    worst possible way to run out of disk.
    """

    def __init__(self, label: str = "", max_events: int = 20_000):
        self.label = label
        self._max = max(1, int(max_events))
        self._rows: list[dict] = []
        self._dropped = 0
        self.started = datetime.now(timezone.utc).isoformat()

    def record(self, event: SensingEvent) -> None:
        if len(self._rows) >= self._max:
            # Drop the NEWEST rather than the oldest: a trace is opened to study
            # how a situation developed, and losing its beginning loses the
            # explanation while keeping the symptom.
            self._dropped += 1
            return
        self._rows.append(_event_to_row(event))

    def __len__(self) -> int:
        return len(self._rows)

    def to_dict(self) -> dict:
        return {
            "version": TRACE_VERSION,
            "label": self.label,
            "started": self.started,
            "events": list(self._rows),
            "truncated": self._dropped > 0,
            "dropped": self._dropped,
            "sanitized": False,
        }


def sanitize(trace: dict, drop_positions: bool = False) -> dict:
    """Strip everything that identifies a person.

    Identity labels and vitals go unconditionally — there is no debugging reason
    to know it was Ana, or what her breathing rate was. Coordinates stay by
    default because a positional bug cannot be reproduced without them, and go
    when the trace is leaving the author's control.

    The result records WHAT was removed. A recipient must be able to check the
    promise instead of trusting a filename.
    """
    if not isinstance(trace, dict):
        raise TraceError("a trace must be an object")
    removed = list(SANITIZED_ALWAYS)
    out_events = []
    for row in trace.get("events") or []:
        ev = dict(row.get("event") or {})
        ev["identities"] = []
        ev["breathing_bpm"] = None
        ev["heart_bpm"] = None
        if drop_positions:
            ev["targets"] = [
                {**t, "x": None, "y": None, "z": None}
                for t in (ev.get("targets") or [])]
        out_events.append({**row, "event": ev})
    if drop_positions:
        removed.append("target positions")
    return {**trace, "events": out_events, "sanitized": True,
            "sanitized_removed": removed}


def save(trace: dict, path: str | Path) -> Path:
    """Write a trace, refusing to write an unsanitised one to disk unmarked.

    Not a hard refusal — an operator may legitimately keep a raw trace on their
    own machine — but the file says which it is, in the file, so a copy that
    travels cannot pretend to be safe.
    """
    p = Path(path)
    p.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return p


def load(path: str | Path) -> dict:
    try:
        trace = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceError(f"could not read the trace: {exc}") from None
    if not isinstance(trace, dict) or "events" not in trace:
        raise TraceError("this file is not a Wavr trace")
    version = trace.get("version")
    if version != TRACE_VERSION:
        # Refuse rather than guess. A trace read under the wrong shape produces
        # room states that look plausible and are wrong, which is worse than an
        # error message.
        raise TraceError(
            f"trace version {version} cannot be read by this Wavr "
            f"(expected {TRACE_VERSION})")
    return trace


def replay(trace: dict, engine_factory, on_state=None) -> list:
    """Feed a trace through a fresh engine and return every room state produced.

    `engine_factory(now_fn) -> FusionEngine`. The factory receives the clock,
    which is the whole trick: the engine ages sources against the TRACE's
    timestamps, not the wall clock, so a recording from last Tuesday behaves on
    replay exactly as it did then. Without that, every replay decays its own
    input to nothing.
    """
    rows = trace.get("events") or []
    clock = {"now": None}
    engine = engine_factory(lambda: clock["now"])
    states = []
    for row in rows:
        event = _row_to_event(row)
        try:
            clock["now"] = datetime.fromisoformat(event.ts)
        except (TypeError, ValueError):
            # A malformed timestamp is part of what the trace recorded; the
            # engine has its own handling for it. Leave the clock where it was
            # rather than skipping the event, so replay reproduces the original
            # behaviour instead of an idealised version of it.
            pass
        state = engine.update(event)
        states.append(state)
        if on_state is not None:
            on_state(state)
    return states


def summarize(trace: dict) -> dict:
    """What is in this trace, without replaying it.

    Enough to decide whether a trace is the one you want before spending the
    time: which rooms, which sensors, how long, and whether it is safe to share.
    """
    rows = trace.get("events") or []
    rooms, sensors, modalities = set(), set(), set()
    first = last = None
    for row in rows:
        ev = row.get("event") or {}
        if ev.get("room"):
            rooms.add(ev["room"])
        if ev.get("sensor_id"):
            sensors.add(ev["sensor_id"])
        if ev.get("modality"):
            modalities.add(ev["modality"])
        ts = ev.get("ts")
        if ts:
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
    span = None
    if first and last:
        try:
            span = round((datetime.fromisoformat(last)
                          - datetime.fromisoformat(first)).total_seconds(), 1)
        except ValueError:
            span = None
    return {
        "label": trace.get("label", ""),
        "events": len(rows),
        "rooms": sorted(rooms),
        "sensors": sorted(sensors),
        "modalities": sorted(modalities),
        "first_ts": first,
        "last_ts": last,
        "span_s": span,
        "truncated": bool(trace.get("truncated")),
        "sanitized": bool(trace.get("sanitized")),
        "sanitized_removed": trace.get("sanitized_removed") or [],
    }
