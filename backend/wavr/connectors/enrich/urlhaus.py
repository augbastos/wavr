"""abuse.ch URLhaus (keyless) malware-URL/host/hash lookup -- ALERT ENRICHER
for the network guard ("is this a known-bad endpoint?"; DESIGN-external-
connectors.md section 3.2/5 #3).

CONNECTOR ID: "urlhaus" (a `kind="generic"` row in `connector_store`).

EGRESS -- disclose precisely: exactly ONE of a URL, a bare hostname, or a
file hash being checked -- the single value the network-inventory service (or
the guard's alert pipeline) is asking about. Nothing about the house itself
(device inventory, MAC, the QUERIER's own IP) leaves. Keyless: no account, no
API key, no cookies.

This is a per-query, per-lookup enricher today (`cache_mode="none"` in
practice for this build); DESIGN-external-connectors.md section 2 notes
URLhaus's bulk feed CAN be mirrored on-box for zero-egress lookups -- that
mirror is a SEPARATE, second-wave feature (not built here); this module is
the direct per-query API path.

GATE: `make_urlhaus_lookup(store, ...)` returns a `lookup(...) -> dict`
closure that checks `store.is_enabled("urlhaus")` via the shared
`guarded_call` chokepoint on EVERY call -- default-OFF, revocable, no
restart. When disabled, `lookup()` returns `{"ok": False, "status":
"disabled"}` and NEVER calls the transport -- zero network attempted.

ENRICHMENT IS FAIL-OPEN: a disabled or failing lookup must never suppress or
crash the base alert it enriches -- callers wire a `{"ok": False, ...}`
result in additively (see DESIGN-external-connectors.md section 3.2).

SSRF: `_ENDPOINTS` values are fixed, pinned official abuse.ch hosts -- never
derived from the URL/host being looked up (the looked-up value is sent as a
form FIELD in the POST body, never used to build the request URL itself).
"""
from __future__ import annotations

import logging
from typing import Callable

from wavr.connectors.http import guarded_call, post_form

CONNECTOR_ID = "urlhaus"

# kind -> (pinned official endpoint, form field name). The looked-up VALUE is
# always sent as a form field, never interpolated into the request URL.
_ENDPOINTS = {
    "url": ("https://urlhaus-api.abuse.ch/v1/url/", "url"),
    "host": ("https://urlhaus-api.abuse.ch/v1/host/", "host"),
    "hash": ("https://urlhaus-api.abuse.ch/v1/payload/", "sha256_hash"),
}


def make_urlhaus_lookup(store, *, connector_id: str = CONNECTOR_ID,
                         post: Callable[..., dict] | None = None,
                         timeout: float = 10.0) -> Callable[..., dict]:
    """Factory mirroring `notify/telegram.make_telegram_send`: returns a
    `lookup(*, url=None, host=None, sha256_hash=None) -> dict` closure (EXACTLY
    one of the three kwargs must be given per call -- the network guard picks
    the right one for what it is checking). `post` is injectable (mirrors
    `wavr.connectors.http.post_form`'s seam) -- tests supply a fake that
    records the call and returns/raises whatever the test needs, so `lookup()`
    is exercised with zero real network.

    Return shapes (never raises):
      {"ok": False, "status": "disabled"}     -- connector not enabled in the broker
      {"ok": False, "status": "bad_request"}  -- zero or more than one of url/host/hash given
      {"ok": False, "status": "error"}        -- enabled but the POST failed
      {"ok": False, "status": "malformed"}    -- enabled but the body was unusable
      {"ok": True,  "status": "fetched", "query_status":, "malicious":,
       "threat":, "listing_status":, "tags": [...]}
    """
    _post = post or post_form

    def lookup(*, url: str | None = None, host: str | None = None,
                sha256_hash: str | None = None) -> dict:
        def _gated() -> dict:
            picked = [(k, v) for k, v in
                      (("url", url), ("host", host), ("hash", sha256_hash)) if v]
            if len(picked) != 1:
                return {"ok": False, "status": "bad_request"}
            kind, value = picked[0]
            endpoint, field = _ENDPOINTS[kind]
            try:
                data = _post(endpoint, {field: value}, timeout=timeout)
            except Exception:
                logging.warning("urlhaus lookup failed (connector=%s)", connector_id)
                return {"ok": False, "status": "error"}
            if not isinstance(data, dict) or not data:
                return {"ok": False, "status": "malformed"}
            query_status = data.get("query_status")
            return {
                "ok": True, "status": "fetched",
                "query_status": query_status,
                # "ok" from URLhaus means a match was FOUND (i.e. it IS known-bad);
                # "no_results" (or any other status) means no match -- not malicious.
                "malicious": query_status == "ok",
                "threat": data.get("threat"),
                "listing_status": data.get("url_status") or data.get("host_status"),
                "tags": list(data.get("tags") or []),
            }
        return guarded_call(store, connector_id, _gated)

    return lookup
