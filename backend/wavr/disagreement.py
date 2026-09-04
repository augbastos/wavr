"""Where sensors in one room contradict each other, said out loud.

## Why this is its own module

A confidence number is a summary, and a summary of a contradiction is the one
place a summary lies. "68% occupied" over a radar saying somebody is there and a
camera saying the room is empty tells a person nothing about the interesting
part — which is that Wavr's own evidence does not agree, and that the answer
rests on a rule rather than on consensus.

This lived inside `mcp.py`, which meant an AI agent could be told about a
disagreement and the household could not. The people who own the house had less
information than a program talking to it.

So it is here, and every surface reads it: the dashboard, the MCP tools, and
anything added later. One producer, for the same reason the runtime state has
one — two implementations of "do these sensors agree" eventually differ, in
front of somebody with no way to tell which is right.

## Two things it refuses to do

**It does not compare stale sensors.** A sensor that stopped reporting is not
disagreeing, it is absent, and counting it as dissent manufactures a
contradiction out of something being unplugged.

**It does not present the sides as equal.** Wavr's fusion gives a report of
absence no mass — a camera failing to see a still person is weaker evidence than
one that sees somebody — so the report says both what the disagreement IS and
how Wavr weighed it. Showing "two sensors say empty, one says occupied" without
that would imply the majority should have won.
"""
from __future__ import annotations


def disagreement(sources: list) -> dict:
    """Where fresh sensors in one room contradict each other.

    Only FRESH ones are compared: a stale sensor is not disagreeing, it is
    absent, and reporting it as dissent would manufacture a contradiction out of
    something being unplugged.

    A source that reports absence is not treated as an equal vote — Wavr's own
    fusion gives absence no mass, because a camera failing to see a still person
    is weaker evidence than one seeing somebody. The report says both what the
    disagreement is and how Wavr weighed it, so an agent does not have to guess
    which side won.
    """
    fresh = [s for s in (sources or []) if s.get("health") == "fresh"]
    saying_present = [s for s in fresh if s.get("presence")]
    saying_empty = [s for s in fresh if not s.get("presence")]
    if not (saying_present and saying_empty):
        return {"disagree": False, "sensors": []}

    def label(s):
        return {"sensor_id": s.get("sensor_id", ""),
                "modality": s.get("modality", ""),
                "says": "occupied" if s.get("presence") else "empty"}

    counts = {s.get("count") for s in fresh if s.get("count") is not None}
    return {
        "disagree": True,
        "sensors": [label(s) for s in fresh],
        "counts_disagree": len(counts) > 1,
        "note": ("These sensors contradict each other. Wavr gives a report of "
                 "absence no weight in the merge — a sensor that fails to see a "
                 "still person is weaker evidence than one that sees somebody — "
                 "so the room reads as occupied while the disagreement stands."),
    }
