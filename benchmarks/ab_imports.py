"""Interleaved A/B of `import wavr.app` between two installs.

    python benchmarks/ab_imports.py <python-A> <python-B> [--rounds 12]

A machine that is busier during one benchmark run than the other makes a
before/after pair of separate runs meaningless for wall-clock numbers (the
interpreter's own start-up is the tell: it cannot have changed, and when it
moves, everything moves). This alternates A and B, round by round, so drift
lands on both, and reports medians of: interpreter-only start, import of
wavr.app (which also builds the default app), and the per-package import times.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update(WAVR_DB=os.path.join(tmp, "wavr.db"),
               WAVR_HOUSE_MAP=os.path.join(tmp, "house.json"),
               WAVR_FRONTEND=str(REPO / "frontend"), PYTHON_DOTENV_DISABLED="1")
    return env


def one(python: str, tmp: str) -> dict:
    env = _env(tmp)
    t = time.perf_counter()
    subprocess.run([python, "-c", "pass"], env=env, check=True)
    bare = time.perf_counter() - t
    t = time.perf_counter()
    r = subprocess.run([python, "-X", "importtime", "-c", "import wavr.app"],
                       env=env, cwd=tmp, capture_output=True, text=True, check=True)
    wall = time.perf_counter() - t
    pkgs: dict[str, int] = {}
    for line in r.stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            parts = [p.strip() for p in line[12:].split("|")]
            try:
                pkgs[parts[2].split(".")[0]] = pkgs.get(parts[2].split(".")[0], 0) + int(parts[0])
            except ValueError:
                pass
    return {"bare_s": bare, "import_wall_s": wall, "pkgs_us": pkgs}


def main() -> None:
    a, b = sys.argv[1], sys.argv[2]
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 12
    # One database directory per install: the two schemas must not meet.
    tmps = {"A": tempfile.mkdtemp(prefix="wavr-ab-A-"), "B": tempfile.mkdtemp(prefix="wavr-ab-B-")}
    for label, py in (("A", a), ("B", b)):   # warm .pyc and the database
        one(py, tmps[label])
    res = {"A": [], "B": []}
    for i in range(rounds):
        for label, py in (("A", a), ("B", b)) if i % 2 == 0 else (("B", b), ("A", a)):
            res[label].append(one(py, tmps[label]))
    out = {}
    for label in ("A", "B"):
        runs = res[label]
        pk: dict[str, list[int]] = {}
        for r in runs:
            for k, v in r["pkgs_us"].items():
                pk.setdefault(k, []).append(v)
        top = sorted(((k, statistics.median(v) / 1000) for k, v in pk.items()),
                     key=lambda kv: -kv[1])[:8]
        out[label] = {
            "bare_interpreter_s": round(statistics.median(r["bare_s"] for r in runs), 3),
            "import_wavr_app_s": round(statistics.median(r["import_wall_s"] for r in runs), 3),
            "import_minus_interpreter_s": round(statistics.median(
                r["import_wall_s"] - r["bare_s"] for r in runs), 3),
            "top_packages_self_ms": {k: round(v, 1) for k, v in top},
        }
    print(json.dumps({"rounds": rounds, "A": a, "B": b, **out}, indent=1))


if __name__ == "__main__":
    main()
