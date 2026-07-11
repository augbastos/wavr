"""Wikipedia (keyless) knowledge lookup -- an assistant/voice TOOL ("OK Wavr,
what's a heat pump?"; DESIGN-external-connectors.md section 3.1/5 #5).

CONNECTOR ID: "wikipedia" (a `kind="generic"` row in `connector_store`).

EGRESS -- disclose precisely: the user's SEARCH QUERY TEXT ONLY (e.g. "heat
pump"). Never house state, device data, occupancy, or any locally-derived
context is folded into the query -- the caller (the assistant tool wrapper)
is responsible for passing only the user's own question text, mirroring
`narrator.build_prompt`'s allowlist discipline at the boundary of what a
caller is permitted to pass in. Keyless: no account, no API key, no cookies.

GATE: `make_wikipedia_lookup(store, ...)` returns a `lookup(query) -> dict`
closure that checks `store.is_enabled("wikipedia")` via the shared
`guarded_call` chokepoint on EVERY call -- default-OFF, revocable, no
restart. When disabled, `lookup()` returns `{"ok": False, "status":
"disabled"}` and NEVER calls the transport -- zero network attempted.

SSRF: `_BASE_URL` is a fixed, pinned official host (`en.wikipedia.org`) --
never derived from caller/user input; the query is sent as a query-string
VALUE, never used to build the request path/host.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Callable

from wavr.connectors.http import get_json, guarded_call

CONNECTOR_ID = "wikipedia"
_BASE_URL = "https://en.wikipedia.org/w/api.php"  # pinned official host -- never user-controlled

# A voice/assistant query is a short phrase, not a document -- this is a
# defensive clamp against a caller passing an unexpectedly large string.
_MAX_QUERY_LEN = 300


def make_wikipedia_lookup(store, *, connector_id: str = CONNECTOR_ID,
                           get: Callable[..., dict] | None = None,
                           timeout: float = 10.0) -> Callable[[str], dict]:
    """Factory mirroring `notify/telegram.make_telegram_send`: returns a
    `lookup(query: str) -> dict` closure closed over `store` (the broker).
    `get` is injectable (mirrors `wavr.connectors.http.get_json`'s seam) --
    tests supply a fake that records the call and returns/raises whatever the
    test needs, so `lookup()` is exercised with zero real network.

    Return shapes (never raises):
      {"ok": False, "status": "disabled"}     -- connector not enabled in the broker
      {"ok": False, "status": "bad_request"}  -- blank/whitespace-only query
      {"ok": False, "status": "error"}        -- enabled but the GET failed
      {"ok": False, "status": "malformed"}    -- enabled but the body was unusable
      {"ok": True,  "status": "fetched", "found": False}                       -- no matching article
      {"ok": True,  "status": "fetched", "found": True, "title":, "extract":}  -- match
    """
    _get = get or get_json

    def lookup(query: str) -> dict:
        def _gated() -> dict:
            q = (query or "").strip()
            if not q:
                return {"ok": False, "status": "bad_request"}
            q = q[:_MAX_QUERY_LEN]
            qs = urllib.parse.urlencode({
                "action": "query", "generator": "search", "gsrsearch": q,
                "gsrlimit": 1, "prop": "extracts", "exintro": 1,
                "explaintext": 1, "format": "json",
            })
            url = f"{_BASE_URL}?{qs}"
            try:
                data = _get(url, headers={"User-Agent": "Wavr/1.0 (local home assistant)"},
                            timeout=timeout)
            except Exception:
                logging.warning("wikipedia lookup failed (connector=%s)", connector_id)
                return {"ok": False, "status": "error"}
            if not isinstance(data, dict) or not data:
                return {"ok": False, "status": "malformed"}
            query_block = data.get("query")
            pages = query_block.get("pages") if isinstance(query_block, dict) else None
            if not isinstance(pages, dict) or not pages:
                return {"ok": True, "status": "fetched", "found": False}
            page = next(iter(pages.values()), {})
            if not isinstance(page, dict):
                return {"ok": True, "status": "fetched", "found": False}
            extract = (page.get("extract") or "").strip()
            if not extract:
                return {"ok": True, "status": "fetched", "found": False}
            return {"ok": True, "status": "fetched", "found": True,
                     "title": page.get("title"), "extract": extract}
        return guarded_call(store, connector_id, _gated)

    return lookup
