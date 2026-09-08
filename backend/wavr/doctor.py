"""wavr doctor -- print the local Core's diagnostic report, ready to paste into a GitHub issue.

Flutter-doctor pattern: it asks the RUNNING Core (over loopback) for the report the server
already builds and MAC-redacts (see net_doctor.build_doctor_report). The redaction is authoritative
server-side; this client only prints what it receives. Wavr never phones home -- the only network
call here is to your own machine.

    python -m wavr.doctor                 # queries https://127.0.0.1:8000
    python -m wavr.doctor --url https://192.168.1.10:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from wavr.status import context_for, is_loopback


def fetch_doctor(base_url: str, token: str | None = None, timeout: float = 40.0) -> dict:
    url = base_url.rstrip("/") + "/api/health/doctor"
    req = urllib.request.Request(url, headers={"X-Wavr-Local": "1"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    # The comment here used to say the exemption was "scoped to localhost only".
    # It was not: `--url` accepts anything, so pointing this at a Core across the
    # LAN turned verification off there too and silently accepted whatever
    # certificate was offered. A claimed scope that nothing enforces is the
    # failure this codebase keeps finding; `wavr.status.context_for` enforces it,
    # so this uses that rather than keeping a second, weaker copy.
    ctx = context_for(url)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="wavr doctor",
        description="Print the local Wavr Core's diagnostic report (MACs redacted).")
    ap.add_argument("--url", default=os.environ.get("WAVR_DOCTOR_URL", "https://127.0.0.1:8000"),
                    help="Core base URL (default https://127.0.0.1:8000)")
    ap.add_argument("--token", default=os.environ.get("WAVR_LOCAL_TOKEN"),
                    help="local API token, if the Core requires one (or set WAVR_LOCAL_TOKEN)")
    args = ap.parse_args(argv)
    try:
        data = fetch_doctor(args.url, args.token)
    except Exception as exc:  # noqa: BLE001 -- a CLI should print a friendly reason, not a traceback
        print(f"wavr doctor: couldn't reach the Core at {args.url} ({exc}).\n"
              f"Is it running? Start it with:  python -m wavr.serve", file=sys.stderr)
        return 2
    report = data.get("report")
    if not report:
        print("wavr doctor: the Core responded but sent no report "
              "(is it up to date?).", file=sys.stderr)
        return 3
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
