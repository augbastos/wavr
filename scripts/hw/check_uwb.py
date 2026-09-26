"""Check a local UWB reading source using Wavr's existing Nearby Interaction parser.

The UWB broker only distributes session parameters; it does not expose radio
readings. This probe expects a local HTTP bridge returning one JSON object with
discovery_token and distance_m (and optional direction), as consumed by
wavr.apple.parse_nearby_object. It never prints the session token.

A PASS proves the bridge produces a parseable Nearby Interaction reading. It does
NOT prove the Core ingests it: no Core route calls this parser; UWB readings reach
fusion through the provider ingest route (backend/wavr/api_provider_ingest.py).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def verdict(code: int, summary: str, *evidence: str) -> int:
    print(f"{('PASS', 'FAIL', 'CANNOT TEST')[code]}: {summary}")
    for line in evidence:
        print(line)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="local HTTP URL returning one Nearby Interaction JSON reading")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()
    if not args.source:
        return verdict(2, "no UWB reading source supplied", "The Core's UWB broker has no reading endpoint; supply --source for a local bridge.")
    try:
        parsed = urllib.parse.urlsplit(args.source)
    except ValueError:
        return verdict(2, "source URL is malformed")
    host = parsed.hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_private
    except ValueError:
        local = host == "localhost"
    if parsed.scheme != "http" or not local or parsed.username or parsed.password or args.timeout <= 0:
        return verdict(2, "source must be a local IP/localhost http:// URL without credentials, and timeout must be positive")
    try:
        from wavr.apple import AppleError, parse_nearby_object
    except ImportError:
        return verdict(2, "Wavr Nearby Interaction parser is unavailable", "Set PYTHONPATH to this checkout's backend directory.")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(args.source, timeout=args.timeout) as response:
            payload = json.loads(response.read(65537))
    except (OSError, urllib.error.URLError) as exc:
        # Nothing answered: the source is missing, so nothing was tested.
        return verdict(2, "UWB reading source is unreachable", f"reason={type(exc).__name__}")
    except ValueError as exc:
        return verdict(1, "UWB reading source returned invalid JSON", f"reason={type(exc).__name__}")
    try:
        reading = parse_nearby_object(payload)
    except (AppleError, TypeError, ValueError) as exc:
        return verdict(1, "source did not produce a valid UWB reading", f"reason={type(exc).__name__}")
    if reading["distance_m"] is None:
        return verdict(1, "source returned direction but no range", "A distance_m reading is required for this check.")
    return verdict(0, "source reachable and UWB range parsed", f"distance_m={reading['distance_m']:g}", f"has_direction={reading['has_direction']}")


if __name__ == "__main__":
    sys.exit(main())
