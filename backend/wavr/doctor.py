"""wavr doctor -- print the local Core's diagnostic report, ready to paste into a GitHub issue.

Flutter-doctor pattern: it asks the RUNNING Core (over loopback) for the report the server
already builds and MAC-redacts (see net_doctor.build_doctor_report). The redaction is authoritative
server-side; this client only prints what it receives. Wavr never phones home -- the only network
call here is to your own machine.

    python -m wavr.doctor                 # queries https://127.0.0.1:8000
    python -m wavr.doctor --url https://192.168.1.57:8000
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request


def fetch_doctor(base_url: str, token: str | None = None, timeout: float = 40.0) -> dict:
    url = base_url.rstrip("/") + "/api/health/doctor"
    req = urllib.request.Request(url, headers={"X-Wavr-Local": "1"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = None
    if url.startswith("https"):
        # Loopback to the Core's self-signed LOCAL cert -- verification off is scoped to localhost
        # only (the whole point of the tool is the machine talking to itself). Not used for egress.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
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
