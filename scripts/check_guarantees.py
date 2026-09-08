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

CI runs it as `guarantees` in `.github/workflows/tests.yml` — its own job, for
exactly that reason: a separate job is a separate checkout on a separate runner,
which is the isolation this needs. It ran nowhere at all until then, so between
one person remembering to run it and the next, a guarantee could stop being held
without anything saying so.

## What counts as held

A guarantee is HELD when the narrowed run FAILS with the guard removed. A run
that ERRORS holds nothing — see `run` for the version of this that got it
backwards.

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

# The three verdicts a check can reach. ERROR is not a near miss and not a soft
# UNHELD: it means the run produced no evidence either way, which is the one
# outcome that must never be reported as a guarantee holding. See `run`.
HELD, UNHELD, ERROR = "HELD", "UNHELD", "ERROR"


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
    Guarantee(
        "erase_never_setup",
        "\"Delete what Wavr learned\" never deletes what a person set up.",
        "wavr/data_erasure.py",
        r'DEFAULT_OBSERVATIONS: tuple\[str, \.\.\.\] = \(\n',
        'DEFAULT_OBSERVATIONS: tuple[str, ...] = (\n    "cameras",\n',
        "erasure",
    ),
    Guarantee(
        "space_projection",
        "A paired phone learns the Space's name and kind, and nothing else.",
        "wavr/app.py",
        r'return \{"name": d\.get\("name"\), "kind": d\.get\("kind"\)\}',
        "return d",
        "space_identity or status_shape",
    ),
    Guarantee(
        "silence_is_not_ok",
        "A house nothing is watching is never reported as fine.",
        "wavr/house_status.py",
        r'blind = sources_checked is not None and not sources_checked',
        "blind = False",
        "house_status",
    ),
    Guarantee(
        "tls_claim_is_the_socket",
        "Encryption is never claimed on a connection that is not encrypted.",
        "wavr/tls.py",
        r'return \(_current_scheme\.get\(\)',
        'return "https"  # broken on purpose\n    return (_current_scheme.get()',
        "tls_claim or multidevice or owner",
    ),
    Guarantee(
        "the_switch_opens_the_network",
        "\"Let other devices connect\" actually lets other devices connect.",
        "wavr/config.py",
        r'return "0\.0\.0\.0" if multidevice else "127\.0\.0\.1"',
        'return "127.0.0.1"',
        "letting_devices or onboarding",
    ),
    Guarantee(
        "an_advertised_address_answers",
        "An address handed to another device is one the socket is actually on.",
        "wavr/app.py",
        r'return _local_ip if serves_the_lan\(cfg\.bind_host\) else "127\.0\.0\.1"',
        "return _local_ip",
        "letting_devices",
    ),
    Guarantee(
        "no_ceremony_without_a_certificate",
        "A fingerprint compare is never staged against a certificate nobody serves.",
        "wavr/tls.py",
        r'return served_scheme\(state\) == "https"',
        "return True",
        "tls_claim or multidevice or owner or pair",
    ),
)


def run(g: Guarantee) -> str:
    """`HELD`, `UNHELD` or `ERROR` for one guarantee.

    ## An errored run is evidence of nothing

    The verdict used to be `"failed" in last or " error" in last.lower()`, and
    the second half of that scored a COLLAPSED run as a guarantee being held.
    Break an import — which is one of the more likely ways to break a guard by
    accident — and every test in the file errors during collection, pytest's
    last line says "1 error", and the check reported HELD. The one state in
    which nothing whatsoever is being verified was the state that read as
    strongest.

    So the verdict comes from pytest's exit code, which is defined:

        0  everything passed        nothing noticed the break   -> UNHELD
        1  tests failed             the suite caught it         -> HELD
        2  interrupted              no verdict was produced     -> ERROR
        5  no tests ran             the `tests` expression is stale -> ERROR

    ...and "N errors" in the summary is an ERROR even standing next to a
    failure: a run that half-collapsed cannot tell us which half the failure
    came from.

    ## Bytes, not text, and why that is not pedantry

    This edits source files, so "put it back exactly" is the whole safety
    story. The first version read and wrote with `read_text`/`write_text(
    newline="\\n")`, which silently rewrites a CRLF file to LF from end to
    end — and then verified the restore with `read_text`, which normalises
    newlines and therefore compares EQUAL to the file it just converted. The
    check that existed to catch a bad restore was structurally incapable of
    seeing this one, and 83 of the 143 files in `backend/wavr` are CRLF, two
    of them named below.

    So: bytes throughout, the file's own newline convention preserved, and the
    restore compared byte-for-byte.
    """
    path = BACKEND / g.path
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    flat = raw.decode("utf-8").replace("\r\n", "\n") if crlf \
        else raw.decode("utf-8")

    broken, n = re.subn(g.find, g.replace, flat, count=1)
    if n != 1:
        print(f"  ERROR    {g.name}: pattern matched {n} times, expected 1 — "
              f"the guard moved and this check is stale")
        return ERROR

    def to_bytes(text: str) -> bytes:
        return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")

    # Written and verified BEFORE the source is touched, so there is never a
    # moment where the only copy of this file is the mutated one.
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    backup.write_bytes(raw)
    if backup.read_bytes() != raw:
        print(f"  ERROR    {g.name}: could not write a verified backup, skipped")
        backup.unlink(missing_ok=True)
        return ERROR

    path.write_bytes(to_bytes(broken))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q",
             "-p", "no:unraisableexception", "-k", g.tests],
            cwd=BACKEND, capture_output=True, text=True)
        last = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
    finally:
        path.write_bytes(raw)
        if path.read_bytes() == raw:
            backup.unlink(missing_ok=True)
        else:
            print(f"  !! RESTORE FAILED for {path}. The original is at {backup} "
                  f"— put it back before doing anything else.")

    errored = (proc.returncode not in (0, 1)
               or re.search(r"\b\d+ errors?\b", last) is not None)
    if errored:
        print(f"  ERROR    {g.name}: the run produced no verdict "
              f"(pytest exit {proc.returncode})")
        print(f"           {last[:100]}")
        print("           ^ a run that errors has not tested the guarantee. "
              "Fix the run before reading this as held or unheld.")
        return ERROR

    caught = re.search(r"\b\d+ failed\b", last) is not None
    print(f"  {'HELD  ' if caught else 'UNHELD'}   {g.name}: {g.claim}")
    print(f"           {last[:100]}")
    if not caught:
        print("           ^ nothing failed with the guard removed. The code is "
              "correct; the SUITE is not holding it there.")
    return HELD if caught else UNHELD


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
    unheld = [g.name for g, r in zip(chosen, results) if r == UNHELD]
    errored = [g.name for g, r in zip(chosen, results) if r == ERROR]
    print()
    if errored:
        print(f"{len(errored)} check(s) produced NO VERDICT: "
              f"{', '.join(errored)}")
        print("A check that errors is not a guarantee that held — it is a "
              "check that did not run. Deal with these first: while one is "
              "erroring, nothing here knows whether its guarantee is held.")
    if unheld:
        print(f"{len(unheld)} guarantee(s) NOT held by the suite: "
              f"{', '.join(unheld)}")
    if errored or unheld:
        return 1
    print("Every guarantee checked is held by a test that fails without it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
