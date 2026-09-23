"""Interleaved A/B of benchmarks/pipeline.py between two source trees.

    python benchmarks/ab_pipeline.py <backend-dir-A> <backend-dir-B> [--rounds 5]

Each round runs pipeline.py once against A and once against B (alternating the
order), with PYTHONPATH pointing at that tree's backend/, in the interpreter
running this script. Separate runs taken minutes apart were not comparable on a
shared machine -- fusion, which had not changed, came out 60% slower in the
second -- so only interleaved medians are reported.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = [("ingest_to_hub_changing", "p50_us"), ("ingest_to_hub_steady", "p50_us"),
        ("recent_200", "p50_us"), ("fusion_update_us", None),
        ("insert_state_ms", None), ("create_app_existing_db_s", None)]
PAYLOADS = ["/api/state", "/api/runtime", "/api/history"]


def run(backend: str, events: int) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env["PYTHONPATH"] = backend
    r = subprocess.run([sys.executable, str(HERE / "pipeline.py"), "--events", str(events)],
                       env=env, capture_output=True, text=True, check=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> None:
    a, b = sys.argv[1], sys.argv[2]
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 5
    res = {"A": [], "B": []}
    for i in range(rounds):
        order = (("A", a), ("B", b)) if i % 2 == 0 else (("B", b), ("A", a))
        for label, tree in order:
            res[label].append(run(tree, 600))
    out = {"rounds": rounds}
    for label in ("A", "B"):
        m = {}
        for key, sub in KEYS:
            vals = [r[key][sub] if sub else r[key] for r in res[label]]
            m[key + (f".{sub}" if sub else "")] = round(statistics.median(vals), 3)
        for p in PAYLOADS:
            m[f"{p}.p50_us"] = round(statistics.median(r["payloads"][p]["p50_us"]
                                                       for r in res[label]), 1)
        out[label] = m
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
