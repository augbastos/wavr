"""Does the suite actually hold the guarantees this codebase states?

## The question this answers

Wavr's docstrings make absolute promises: identities never appear in an
experience context, a timestamp is never in the future, an alignment is never
solved from one correspondence, Wavr's own conclusions can never be fed back in
as evidence. Those promises are the product.

A passing test suite does not prove any of them. It proves that the tests
somebody wrote pass. The gap between the two is where several defects hid this
week, and one of them was a promise the code had never implemented at all.

So: break each guard on purpose, run the tests, and see whether anything
notices. If the suite still passes with the guard removed, the guarantee is not
being held by the suite — it is being held by whoever last read the file.

That has already happened here. "A timestamp is never in the future" is enforced
on three code paths and was tested on one; deleting it from either of the other
two passed the entire suite.

## Why this is a script and not a test

It edits source files, which belongs nowhere near a test run.

Every file is copied to `<name>.guarantee-backup` BEFORE it is touched, restored
from that copy afterwards, and compared byte-for-byte. A `git stash` would not
do: this has to be safe on a tree full of uncommitted work, which is exactly the
tree it is most useful on. If a restore ever fails, the script says so loudly
and names the backup rather than exiting quietly — a mutated source file left
behind is worse than any bug this could find.

    python scripts/check_guarantees.py
    python scripts/check_guarantees.py --only timestamp

**Never run this alongside another test run.** It mutates source files that the
other run is importing, so the other run fails for reasons that have nothing to
do with the code under it. That has already happened once here, and the failures
looked real enough to chase.

## Adding a guarantee

Add a `Guarantee` below. `find` must match exactly once: a pattern matching zero
times is a stale entry and one matching twice is ambiguous, and both are
reported as errors rather than skipped — a silently skipped check is the exact
thing this script exists to find.

`tests` narrows the run with `-k` so a check takes seconds rather than twenty
minutes. Narrow it to the files that SHOULD catch the break, never to the one
test you already know catches it: the point is to discover that nothing else
would.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BACKUP_SUFFIX = ".guarantee-backup"


@dataclass(frozen=True)
class Guarantee:
    name: str
    claim: str          # the promise, in the words the codebase uses
    path: str           # relative to backend/
    find: str           # regex, must match exactly once
    replace: str        # what breaks it
    tests: str          # -k expression


GUARANTEES = (
    Guarantee(
        "timestamp",
        "A timestamp is never in the future.",
        "wavr/timebase.py",
        r"return Stamp\(at=min\(stated, received\), stated=stated,",
        "return Stamp(at=stated, stated=stated,",
        "timebase or fusion",
    ),
    Guarantee(
        "alignment",
        "Two correspondences is the minimum; one is refused, not solved.",
        "wavr/spatial_align.py",
        r"MIN_CORRESPONDENCES = 2",
        "MIN_CORRESPONDENCES = 1",
        "align or anchor or openxr or apple",
    ),
    Guarantee(
        "anchor_in_room",
        "An anchor's coordinates must fall inside the room it names.",
        "wavr/anchors.py",
        r"ROOM_SLACK_M = 0\.5",
        "ROOM_SLACK_M = 100000.0",
        "anchor",
    ),
    Guarantee(
        "no_feedback_loop",
        "Wavr's own published conclusions can never be mapped back in as evidence.",
        "wavr/ha_presence.py",
        r"(def is_wavr_entity\([^)]*\) -> bool:\n)",
        r"\1    return False  # broken on purpose\n",
        "ha_presence or fusion",
    ),
    Guarantee(
        "redact_credentials",
        "A credential in an error message never reaches source health.",
        "wavr/provider_runtime.py",
        r"(def redact\(text: str\) -> str:\n)",
        r"\1    return text  # broken on purpose\n",
        "runtime or source or coverage or doctor",
    ),
    Guarantee(
        "redact_allowlists",
        "A field added to the context later is absent from a restricted view.",
        "wavr/experience.py",
        r"out = \{k: body\[k\] for k in _ALWAYS_VISIBLE if k in body\}",
        "out = dict(body)",
        "experience or grant",
    ),
    Guarantee(
        "no_operator_names",
        "An experience never sees a name an operator typed.",
        "wavr/experience.py",
        r"(def _labelled\(rows\):\n)",
        r'\1    yield from ((r, str(r.get("sensor_id") or ""), "wavr") for r in rows)\n',
        "experience",
    ),
)


def run(g: Guarantee) -> bool:
    """True when the suite CATCHES the break, which is the passing outcome."""
    path = BACKEND / g.path
    original = path.read_text(encoding="utf-8")
    broken, n = re.subn(g.find, g.replace, original, count=1)
    if n != 1:
        print(f"  ERROR    {g.name}: pattern matched {n} times, expected 1 — "
              f"the guard moved and this check is stale")
        return False

    # Written and verified BEFORE the source is touched, so there is never a
    # moment where the only copy of this file is the mutated one.
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    backup.write_text(original, encoding="utf-8", newline="\n")
    if backup.read_text(encoding="utf-8") != original:
        print(f"  ERROR    {g.name}: could not write a verified backup, skipped")
        backup.unlink(missing_ok=True)
        return False

    path.write_text(broken, encoding="utf-8", newline="\n")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q",
             "-p", "no:unraisableexception", "-k", g.tests],
            cwd=BACKEND, capture_output=True, text=True)
        last = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
    finally:
        path.write_text(original, encoding="utf-8", newline="\n")
        if path.read_text(encoding="utf-8") == original:
            backup.unlink(missing_ok=True)
        else:
            print(f"  !! RESTORE FAILED for {path}. The original is at {backup} "
                  f"— put it back before doing anything else.")

    caught = "failed" in last or " error" in last.lower()
    print(f"  {'HELD  ' if caught else 'UNHELD'}   {g.name}: {g.claim}")
    print(f"           {last[:100]}")
    if not caught:
        print("           ^ nothing failed with the guard removed. The code is "
              "correct; the SUITE is not holding it there.")
    return caught


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Break each stated guarantee on purpose and check the "
                    "suite notices.")
    ap.add_argument("--only", help="run one guarantee by name")
    args = ap.parse_args()

    chosen = [g for g in GUARANTEES if not args.only or g.name == args.only]
    if not chosen:
        print(f"no guarantee named {args.only!r}. "
              f"Known: {', '.join(g.name for g in GUARANTEES)}")
        return 2

    stale = list(BACKEND.rglob(f"*{BACKUP_SUFFIX}"))
    if stale:
        print("A previous run left backups behind, which means a restore did "
              "not finish. Deal with these before running again:")
        for leftover in stale:
            print("   ", leftover)
        return 2

    print(f"Breaking {len(chosen)} guarantee(s) on purpose. Each file is backed "
          f"up first and restored afterwards.\n")
    results = [run(g) for g in chosen]
    unheld = [g.name for g, ok in zip(chosen, results) if not ok]
    print()
    if unheld:
        print(f"{len(unheld)} guarantee(s) NOT held by the suite: "
              f"{', '.join(unheld)}")
        return 1
    print("Every guarantee checked is held by a test that fails without it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
