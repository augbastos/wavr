"""Reference experience #4: an agent asking Wavr about physical context.

    python experiences/mcp-agent/ask_wavr.py --scenario occupancy_arrives
    python experiences/mcp-agent/ask_wavr.py --room kitchen --json

Uses the REAL MCP tools — `wavr.mcp.get_room_context`, `get_sensor_coverage` and
`explain_room_state`, the same functions served over stdio and streamable HTTP.
Called in-process here so the script needs no transport and no running server;
point an MCP client at `python -m wavr.mcp_serve` and it calls these exact
functions.

By default it plays a simulator scenario into a fresh fusion engine, so this runs
on a laptop with no sensors attached. Every reading it produces therefore comes
from a `sim:` sensor, and that label is visible in the output — a demo that
looked like a real house would be a demo that lies.

## What this exists to show

An agent's question is never "what does sensor 3 say". It is "can I answer
without guessing". So the interesting output is where Wavr REFUSES:

  * a room with no coverage says so, rather than returning empty;
  * a headcount nothing can produce is `None`, never 0;
  * two sensors that disagree are reported as disagreeing, rather than merged
    into a number that hides it.

An agent that reads those and says "I don't know" is behaving correctly.

SPDX-License-Identifier: AGPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Wavr's explanations contain typographic characters (an arrow between precision
# rungs, for one), and a Windows console defaults to cp1252, where printing them
# raises. A reference script that crashes on the platform half its readers are
# using is not a reference. `errors="replace"` rather than a silent swap, so a
# character that cannot be shown is visibly a substitution.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Run from a checkout without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from wavr import simulator                                     # noqa: E402
from wavr.fusion import FusionEngine                           # noqa: E402
from wavr.mcp import (                                         # noqa: E402
    FusionStateProvider, explain_room_state, get_room_context, list_rooms,
)


def play(scenario_key: str) -> FusionStateProvider:
    """Feed a scenario into a real fusion engine and hand back an MCP provider.

    The REAL engine, not a stand-in. A demo running against different fusion than
    production would eventually disagree with it, and the disagreement would be
    found by whoever trusted the demo.
    """
    scenario = simulator.get(scenario_key)
    if scenario is None:
        known = ", ".join(sorted(simulator.SCENARIOS))
        raise SystemExit(f"no scenario {scenario_key!r}. Try one of: {known}")
    engine = FusionEngine()
    for step in scenario.steps:
        engine.update(step.event)
    return FusionStateProvider(engine)


def narrate(provider: FusionStateProvider, room_filter: str = "") -> list[str]:
    """The sentences an agent would say, and the ones it must not.

    Every branch that could invent a fact says "I do not know" instead, and the
    comments name the tempting wrong answer.
    """
    lines: list[str] = []
    rooms = [r["room"] for r in list_rooms(provider)]
    if room_filter:
        rooms = [r for r in rooms if r == room_filter]
    if not rooms:
        # The tempting wrong answer: "the house is empty". Wavr has no reading,
        # which is a different thing entirely and the one an agent must say.
        return ["Wavr has no reading for any room. I do not know whether "
                "anybody is home."]

    for name in rooms:
        ctx = get_room_context(provider, name)
        if ctx is None:
            lines.append(f"{name}: nothing is watching it. I do not know.")
            continue

        simulated = any(simulator.is_simulated(str(s.get("sensor_id") or ""))
                        for s in ctx.get("sources") or [])
        tag = "  [simulated]" if simulated else ""

        if ctx.get("occupied"):
            count = ctx.get("person_count")
            if count is None:
                # The tempting wrong answer: "1 person". Presence is not a count,
                # and an agent that rounds it to one will eventually tell somebody
                # their house is empty when three people are in it.
                lines.append(f"{name}: somebody is there. I cannot say how "
                             f"many.{tag}")
            else:
                lines.append(f"{name}: {count} "
                             f"{'person' if count == 1 else 'people'} "
                             f"({int(ctx.get('confidence', 0) * 100)}% "
                             f"confident).{tag}")
        else:
            lines.append(f"{name}: nobody detected "
                         f"({int(ctx.get('confidence', 0) * 100)}% "
                         f"confident).{tag}")

        why = explain_room_state(provider, name) or {}
        # The field that makes an agent useful rather than merely fluent: when
        # two sensors contradict each other, say so instead of picking one.
        dis = why.get("disagreement") or {}
        if dis.get("disagree"):
            said = ", ".join(f"{s['sensor_id']} says {s['says']}"
                             for s in dis.get("sensors", []))
            lines.append(f"    its sensors disagree — {said}.")
            if dis.get("note"):
                lines.append(f"    {dis['note']}")
        if why.get("explanation"):
            lines.append(f"    because: {why['explanation']}")
        nxt = ctx.get("precision_next")
        if nxt:
            lines.append(f"    to answer better here: {nxt}")

    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", default="count_appears_and_vanishes",
                    help="which simulated house to ask about")
    ap.add_argument("--room", default="", help="ask about one room only")
    ap.add_argument("--json", action="store_true",
                    help="raw MCP tool output instead of prose")
    ap.add_argument("--list", action="store_true", help="list the scenarios")
    args = ap.parse_args()

    if args.list:
        for key, scenario in sorted(simulator.SCENARIOS.items()):
            print(f"{key:30s} {scenario.title}")
        return 0

    provider = play(args.scenario)

    if args.json:
        rooms = [r["room"] for r in list_rooms(provider)]
        print(json.dumps(
            {r: {"context": get_room_context(provider, r),
                 "explanation": explain_room_state(provider, r)}
             for r in rooms if not args.room or r == args.room}, indent=2))
        return 0

    scenario = simulator.get(args.scenario)
    print(f"Scenario: {scenario.title}")
    print(f"  {scenario.teaches}\n")
    for line in narrate(provider, args.room):
        print(line)
    print("\nThese are the MCP tools verbatim. Any MCP client gets the same "
          "answers: `python -m wavr.mcp_serve`, then get_room_context / "
          "explain_room_state / get_sensor_coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
