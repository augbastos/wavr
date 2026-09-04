"""Semantic events an application can act on, derived from room state.

## Why a second stream at all

`/ws/live` already broadcasts every fused room state, several times a second per
room. That is the right shape for a dashboard redrawing itself and the wrong
shape for anything else: an application that wants to know "did the kitchen just
become occupied" has to diff consecutive frames, and every application would
implement that diff slightly differently and get the edge cases slightly wrong.

This computes the diff once, in the place that knows what the fields mean.

## The naming rule, which is not stylistic

**An event may only claim what Wavr actually knows.** Wavr detects presence, not
people. It does not know who walked in, and usually does not know that anyone
"walked in" at all — only that a room's occupancy changed. So:

    room.occupancy_changed        ✓ what Wavr observed
    person.entered_room           ✗ two claims Wavr cannot make: that it was a
                                    PERSON rather than presence, and that they
                                    ENTERED rather than became detectable

An application built against a dishonest event name will make promises to its
own users that Wavr cannot keep, and the failure will surface as Wavr's bug.

## What is deliberately absent

No `person.*` events at all. No `transition.detected` — Wavr can say a room's
occupancy changed and another room's changed too, but calling that one person
moving is an inference topology can *assess* and nothing here may *assert*.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.contracts import version

# The shape of an event. Stamped on every one rather than published only at a
# discovery endpoint: an event arrives on a socket a consumer may have opened
# before the endpoint existed, and a version it has to fetch separately is a
# version half of them will not fetch.
EVENTS_VERSION = version("spatial_events")

# Occupancy flipped. The workhorse: it is what most applications actually want,
# and it is a claim about a ROOM, which is a claim Wavr can support.
EV_OCCUPANCY = "room.occupancy_changed"
# The headcount changed, including to or from unknown. `None -> 2` and `2 -> None`
# are both real events: losing the ability to count is information.
EV_COUNT = "room.count_changed"
# How detailed an answer the room can support changed — a camera was calibrated,
# or a radar went offline and took the count rung with it.
EV_PRECISION = "room.precision_changed"
# A sensor started or stopped contributing. Named per SENSOR, which only became
# possible once the merge stopped collapsing them by modality.
EV_SENSOR_OFFLINE = "sensor.offline"
EV_SENSOR_ONLINE = "sensor.online"
# Fresh sensors in one room now contradict each other, or have stopped.
EV_DISAGREEMENT = "room.sensors_disagree"
EV_AGREEMENT = "room.sensors_agree"

EVENT_TYPES: frozenset[str] = frozenset({
    EV_OCCUPANCY, EV_COUNT, EV_PRECISION,
    EV_SENSOR_OFFLINE, EV_SENSOR_ONLINE,
    EV_DISAGREEMENT, EV_AGREEMENT})


def _fresh_sensors(state: dict) -> dict[str, dict]:
    """Sensors currently contributing, by id.

    Anonymous sources are excluded: an event that cannot name which sensor went
    offline is not actionable, and emitting one per anonymous source would make
    the stream noisy in exactly the way that trains people to ignore it.
    """
    out = {}
    for s in state.get("sources") or []:
        sid = s.get("sensor_id")
        if sid and s.get("health") == "fresh":
            out[sid] = s
    return out


def _disagreeing(state: dict) -> bool:
    fresh = list(_fresh_sensors(state).values())
    return any(s.get("presence") for s in fresh) and \
        any(not s.get("presence") for s in fresh)


class SpatialEvents:
    """Turns a stream of room states into events, remembering just enough.

    Holds one previous state per room — the minimum needed to diff — and nothing
    historical. A component that accumulated history here would be a second,
    unbounded copy of the occupancy log.
    """

    def __init__(self, on_event=None):
        self._prev: dict[str, dict] = {}
        self._on_event = on_event

    def observe(self, state: dict) -> list[dict]:
        """Diff one room state against the last, returning what changed.

        Returns a list rather than one event because a single frame genuinely
        can carry several: a radar coming back online can flip occupancy, restore
        a count and raise precision at once, and collapsing those into one would
        force every consumer to guess which they were being told about.
        """
        room = state.get("room")
        if not room:
            return []
        prev = self._prev.get(room)
        self._prev[room] = state
        if prev is None:
            # The first sighting of a room is not a change. Emitting one would
            # make every application see a burst of "everything just happened"
            # at startup and react to a house that was already like that.
            return []

        ts = state.get("ts") or datetime.now(timezone.utc).isoformat()
        events: list[dict] = []

        def emit(kind: str, **payload):
            events.append({"event": kind, "v": EVENTS_VERSION,
                           "room": room, "at": ts, **payload})

        if bool(prev.get("occupied")) != bool(state.get("occupied")):
            emit(EV_OCCUPANCY,
                 occupied=bool(state.get("occupied")),
                 confidence=state.get("confidence"),
                 precision=state.get("precision_level"))

        if prev.get("person_count") != state.get("person_count"):
            # `None` is a value here, not a gap: losing the ability to count is
            # a real event, and an application showing "2 people" needs to know
            # to stop showing it.
            emit(EV_COUNT,
                 count=state.get("person_count"),
                 previous=prev.get("person_count"),
                 count_known=state.get("person_count") is not None)

        if prev.get("precision_level") != state.get("precision_level"):
            emit(EV_PRECISION,
                 precision=state.get("precision_level"),
                 previous=prev.get("precision_level"),
                 how_to_improve=state.get("precision_next"))

        before, now = _fresh_sensors(prev), _fresh_sensors(state)
        for sid in sorted(set(before) - set(now)):
            emit(EV_SENSOR_OFFLINE, sensor_id=sid,
                 modality=before[sid].get("modality", ""))
        for sid in sorted(set(now) - set(before)):
            emit(EV_SENSOR_ONLINE, sensor_id=sid,
                 modality=now[sid].get("modality", ""))

        was, is_now = _disagreeing(prev), _disagreeing(state)
        if was != is_now:
            emit(EV_DISAGREEMENT if is_now else EV_AGREEMENT,
                 sensors=[{"sensor_id": sid,
                           "says": "occupied" if s.get("presence") else "empty"}
                          for sid, s in sorted(now.items())])

        if self._on_event is not None:
            for ev in events:
                self._on_event(ev)
        return events

    def forget(self, room: str) -> None:
        """Drop a room's baseline — for when it is deleted or renamed.

        Without this, a renamed room's old entry would sit there forever and the
        new name would look like a brand-new room on its next reading.
        """
        self._prev.pop(room, None)
