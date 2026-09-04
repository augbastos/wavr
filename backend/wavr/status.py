"""`wavr status` — is it running, and is anything waiting for me.

## Why a CLI answer exists at all

The tray answers this on a desktop and the chip answers it in a browser. Neither
is available to somebody on a Raspberry Pi over SSH, which is where a great many
of these will run — and "check the dashboard" is not an answer when the whole
question is whether the thing serving the dashboard is alive.

NO INVISIBLE SUCCESS applies to a terminal too.

## It renders; it does not decide

Every conclusion comes from `/api/runtime` and `/api/attention`, the same
endpoints the tray and the shell read. This file computes no health of its own,
for the same reason none of the others do: a fourth implementation of "is it
healthy" is a fourth thing that can disagree, in front of somebody with no way
to tell which is right.

## Designed as an interface, not as debug output

A CLI is a human surface. `core=true websocket=ok db=1 src=8/9` is a machine
talking to itself in front of a person.

  * **Aligned labels, one fact per line**, so it can be read at a glance.
  * **`--json` for machines.** The moment a script greps the human output, its
    wording is frozen forever; this gives scripts something better to grep.
  * **Exit codes that mean something**: 0 healthy, 1 needs attention, 2 cannot
    reach the Core. Usable in a cron job or a monitor with no parsing at all.
  * **A failure is a sentence, never a traceback.** Somebody running this is
    already worried; a stack trace answers a question they did not ask.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

DEFAULT_URL = "https://127.0.0.1:8000"

# Exit codes, which are part of the interface. Chosen so `wavr status && …`
# reads correctly: success means "nothing needs you".
OK = 0
ATTENTION = 1
UNREACHABLE = 2

# Hosts where a self-signed certificate is the EXPECTED answer, because the Core
# generated it on this machine and the packet never reaches a network.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "[::1]"}


def is_loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in _LOOPBACK or host.startswith("127.")


def context_for(url: str):
    """TLS settings, and the reason for them.

    The Core's certificate is self-signed and generated on first run, so
    verification cannot succeed against it. But turning verification off
    UNCONDITIONALLY — which the sibling `doctor` tool did — means a `--url`
    pointing at a Core across the LAN silently accepts any certificate anybody
    offers. Its comment said the exemption was "scoped to localhost". Nothing
    enforced that; `--url` accepts anything.

    So the scope is real here: off for loopback, where there is no network to be
    in the middle of, and ON everywhere else. A Core across the network with a
    certificate this machine does not trust fails loudly, with a sentence saying
    why, rather than being quietly downgraded to no protection at all.
    """
    if not url.lower().startswith("https"):
        return None
    ctx = ssl.create_default_context()
    if is_loopback(url):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(base: str, path: str, token: str | None, timeout: float = 6.0):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"X-Wavr-Local": "1"})
    if token:
        req.add_header("X-Wavr-Token", token)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=context_for(base)) as r:
        return json.loads(r.read())


def _human(seconds) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


_MARK = {
    "healthy": "●",      # a filled dot: it is doing something
    "starting": "○",
    "updating": "○",
    "paused": "○",
    "degraded": "!",
    "attention": "!",
    "unavailable": "×",
}


def _rows(runtime: dict, attention: dict | None) -> list[tuple[str, str]]:
    """The facts, in the order somebody scans them.

    State first, because it is the question. Everything after it either
    qualifies that answer or is a thing they came to do something about.
    """
    state = runtime.get("state", "unknown")
    rows = [
        ("Space", runtime.get("space") or "—"),
        ("Core", _MARK.get(state, "•") + " " + state.capitalize()),
    ]
    if runtime.get("role"):
        rows.append(("Role", runtime["role"]))
    rows.append(("Uptime", _human(runtime.get("uptime_s"))))
    # The spine: the number that distinguishes a Core that is WORKING from one
    # that is merely running.
    age = runtime.get("last_state_age_s")
    rows.append(("Last reading",
                 f"{_human(age)} ago" if age is not None else "none yet"))

    for f in runtime.get("findings") or []:
        if f.get("key") in ("sensors", "sources", "nodes", "egress", "storage"):
            rows.append((f["key"].capitalize(), f.get("text", "")))

    # "Could not check" is not "nothing waiting". A CLI that prints reassurance
    # over a source it failed to read is the same lie as a green tray icon.
    rows.append(("Attention",
                 attention.get("headline", "") if attention
                 else "could not check"))
    return rows


def render(runtime: dict, attention: dict | None) -> str:
    rows = _rows(runtime, attention)
    width = max(len(k) for k, _ in rows)
    lines = ["Wavr"]
    lines += [f"  {k.ljust(width)}  {v}" for k, v in rows]
    for f in runtime.get("findings") or []:
        # The reason, under the answer, for the states where somebody is about
        # to ask "why".
        if f.get("key") == "state" and f.get("state") in (
                "degraded", "attention", "unavailable"):
            lines.append("")
            lines.append(f"  {f.get('text', '')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="wavr status",
        description="Is Wavr running, and is anything waiting for you.")
    ap.add_argument("--url",
                    default=os.environ.get("WAVR_DOCTOR_URL", DEFAULT_URL),
                    help=f"Core base URL (default {DEFAULT_URL})")
    ap.add_argument("--token", default=os.environ.get("WAVR_LOCAL_TOKEN"),
                    help="local API token, if the Core requires one")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output; the human format is not a "
                         "stable interface and this one is")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="print nothing; the exit code is the answer")
    args = ap.parse_args(argv)

    try:
        runtime = _get(args.url, "/api/runtime", args.token)
    except Exception as exc:                      # noqa: BLE001
        # A sentence, not a traceback. Somebody running this is already worried,
        # and a stack trace answers a question they did not ask.
        if args.json:
            print(json.dumps({"state": "unavailable", "error": str(exc),
                              "url": args.url}))
        elif not args.quiet:
            hint = ("  Start it with:  python -m wavr.serve"
                    if is_loopback(args.url) else
                    "  This is not a loopback address, so the certificate was "
                    "checked.\n"
                    "  A Core across the network needs one this machine already "
                    "trusts.")
            print(f"Wavr is not answering at {args.url}.\n  {exc}\n{hint}",
                  file=sys.stderr)
        return UNREACHABLE

    # Best-effort: failing to read it must not turn a working Core into a
    # failure, and must not be reported as "nothing waiting" either.
    try:
        attention = _get(args.url, "/api/attention", args.token)
    except Exception:                             # noqa: BLE001
        attention = None

    if args.json:
        print(json.dumps({"runtime": runtime, "attention": attention}, indent=2))
    elif not args.quiet:
        print(render(runtime, attention))

    state = runtime.get("state")
    if state == "unavailable":
        return UNREACHABLE
    if state in ("degraded", "attention") or (attention or {}).get("total"):
        return ATTENTION
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
