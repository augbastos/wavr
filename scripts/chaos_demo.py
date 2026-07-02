"""Drive one Wavr chaos scenario through the FusionEngine and print the evolving
RoomState. Demo / debug helper — no hardware, no heavy deps, fully deterministic.

    python scripts/chaos_demo.py wifi-drop
    python scripts/chaos_demo.py            # defaults to wifi-drop

Scenarios: wifi-drop, camera-flicker, multi-target, fall.
"""
from __future__ import annotations

import argparse
import os
import sys

# Make `wavr` importable whether or not the backend package is pip-installed.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# The fused explanation carries non-ASCII glyphs (→, Portuguese accents); keep the
# demo printable on a legacy Windows console (cp1252) instead of crashing.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wavr.fusion import FusionEngine
from wavr.sources.chaos import SCENARIOS, scenario_events


def run(scenario: str) -> None:
    fusion = FusionEngine()
    print(f"=== chaos scenario: {scenario} ===")
    for ev in scenario_events(scenario):
        rs = fusion.update(ev)
        tgt = f"  targets={len(rs.targets)}" if rs.targets else ""
        print(f"[{ev.ts[11:19]}] {ev.room:<7} {ev.modality:<8} "
              f"{'PRES' if ev.presence else 'gone'} -> "
              f"{'OCCUPIED' if rs.occupied else 'vacant  '} "
              f"conf={rs.confidence:.2f}{tgt}")
    print(f"final: {rs.explanation}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Wavr chaos scenario demo")
    ap.add_argument("scenario", nargs="?", default="wifi-drop",
                    choices=sorted(SCENARIOS))
    run(ap.parse_args().scenario)


if __name__ == "__main__":
    main()
