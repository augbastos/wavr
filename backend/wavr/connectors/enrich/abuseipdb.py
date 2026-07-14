"""AbuseIPDB IP-reputation lookup -- ALERT ENRICHER for the network guard,
"who is my house talking to?" (DESIGN-external-connectors.md section 3.2/5 #4).

CONNECTOR ID: "abuseipdb" (a `kind="generic"` row in `connector_store`).

EGRESS -- disclose precisely, and bluntly: a single IP the house's own
devices have been observed talking to (an OUTBOUND endpoint from the network
inventory / alert pipeline) -- NEVER one of the house's own device IPs/MACs.
AbuseIPDB receives that remote peer's IP and returns its abuse reputation.
Unlike URLhaus, this cannot be made zero-egress (the query itself IS the
per-IP lookup) -- the broker's `scope` badge must state this plainly so the
user opts into the leak deliberately.

SECRETS: the API key is read from an environment variable BY NAME (`key_env`,
default `WAVR_ABUSEIPDB_KEY`) at CALL time only -- never stored in
`connector_store`, never logged. It is sent ONLY in the `Key` header (never
the URL/query), mirroring `wavr.connectors.http.get_json`'s no-leak guarantee.

RATE-LIMIT-AWARE: the free tier caps around 1000 checks/day. A 429 response
is surfaced as its own clean `"rate_limited"` status (distinct from a generic
transport error) so a caller can back off / cache instead of retrying in a
tight loop -- this module makes exactly one request per `lookup()` call and
never retries internally.

GATE: `make_abuseipdb_lookup(store, ...)` returns a `lookup(ip) -> dict`
closure that checks `store.is_enabled("abuseipdb")` via the shared
`guarded_call` chokepoint on EVERY call -- default-OFF, revocable, no
restart. When disabled, `lookup()` returns `{"ok": False, "status":
"disabled"}` and NEVER even reads the API-key env var.

ENRICHMENT IS FAIL-OPEN: a disabled or failing lookup must never suppress or
crash the base alert it enriches (DESIGN-external-connectors.md section 3.2).

SSRF: `_BASE_URL` is a fixed, pinned official host (`api.abuseipdb.com`) --
never derived from caller input; `ip` is validated as a real IP address
before ever reaching the query string.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import urllib.error
import urllib.parse
from typing import Callable

from wavr.connectors.http import get_json, guarded_call

CONNECTOR_ID = "abuseipdb"
DEFAULT_KEY_ENV = "WAVR_ABUSEIPDB_KEY"
_BASE_URL = "https://api.abuseipdb.com/api/v2/check"  # pinned official host -- never user-controlled


def make_abuseipdb_lookup(store, *, connector_id: str = CONNECTOR_ID,
                           key_env: str = DEFAULT_KEY_ENV,
                           max_age_days: int = 90,
                           get: Callable[..., dict] | None = None,
                           timeout: float = 10.0) -> Callable[[str], dict]:
    """Factory mirroring `notify/telegram.make_telegram_send`: returns a
    `lookup(ip: str) -> dict` closure closed over `store` (the broker) and the
    API-key env-var NAME to read at call time. `get` is injectable (mirrors
    `wavr.connectors.http.get_json`'s seam) -- tests supply a fake that
    records the call (and can assert the key never lands in the URL) and
    returns/raises whatever the test needs, so `lookup()` is exercised with
    zero real network.

    Return shapes (never raises):
      {"ok": False, "status": "disabled"}      -- connector not enabled in the broker
      {"ok": False, "status": "unconfigured"}  -- enabled but the key env is unset
      {"ok": False, "status": "bad_request"}   -- `ip` is not a valid IP address
      {"ok": False, "status": "rate_limited"}  -- upstream returned HTTP 429
      {"ok": False, "status": "error"}         -- enabled+configured but the GET failed
      {"ok": False, "status": "malformed"}     -- enabled+configured but the body was unusable
      {"ok": True,  "status": "fetched", "ip":, "abuse_score":,
       "total_reports":, "country_code":, "is_whitelisted":}
    """
    _get = get or get_json

    def lookup(ip: str) -> dict:
        def _gated() -> dict:
            api_key = os.getenv(key_env)
            if not api_key:
                return {"ok": False, "status": "unconfigured"}
            try:
                ipaddress.ip_address(ip)
            except (ValueError, TypeError):
                return {"ok": False, "status": "bad_request"}
            qs = urllib.parse.urlencode({"ipAddress": ip, "maxAgeInDays": max_age_days})
            url = f"{_BASE_URL}?{qs}"
            headers = {"Key": api_key, "Accept": "application/json"}
            try:
                data = _get(url, headers=headers, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    return {"ok": False, "status": "rate_limited"}
                logging.warning("abuseipdb lookup failed (connector=%s, http=%s)",
                                connector_id, exc.code)
                return {"ok": False, "status": "error"}
            except Exception:
                logging.warning("abuseipdb lookup failed (connector=%s)", connector_id)
                return {"ok": False, "status": "error"}
            payload = data.get("data") if isinstance(data, dict) else None
            if not isinstance(payload, dict):
                return {"ok": False, "status": "malformed"}
            return {
                "ok": True, "status": "fetched",
                "ip": payload.get("ipAddress", ip),
                "abuse_score": payload.get("abuseConfidenceScore"),
                "total_reports": payload.get("totalReports"),
                "country_code": payload.get("countryCode"),
                "is_whitelisted": payload.get("isWhitelisted"),
            }
        return guarded_call(store, connector_id, _gated)

    return lookup
