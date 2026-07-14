"""Shared egress chokepoint + dependency-free transport for every connector
module in this package (project_wavr_connectors_vision /
DESIGN-external-connectors.md section 1.3).

Pieces, deliberately small:

  * guarded_call(store, connector_id, fn) -- the ONE gate every connector's
    egress code runs through. store.is_enabled(connector_id) is read fresh
    on every call (no caching), so flipping the kill-switch takes effect on
    the very next call -- REVOCABLE, no restart, no lingering grant. When the
    connector is off, fn is never invoked -- zero network is even attempted.
    This mirrors connector_store.py's own documented generic contract
    (module docstring lines 22-25) made concrete and uniform: no connector in
    this package may call a transport function without going through this
    chokepoint first, so a reviewer only has to audit ONE place.

  * post_json -- a minimal stdlib JSON POST (mirrors narrator._post_json /
    notifier._urllib_post; no new runtime dependency). Callers that need
    credentials in the URL path (Telegram's Bot API has no other option) must
    NOT log the URL on failure -- see notify/telegram.py's docstring for why.

  * get_json -- the read counterpart to post_json: a minimal stdlib JSON
    GET, credentials (when a connector needs one) passed ONLY in headers
    (never the URL/query), same no-leak discipline. Used by the enrich/
    surface's keyless and keyed lookups (Open-Meteo, AbuseIPDB, Wikipedia).

  * post_form -- a minimal stdlib application/x-www-form-urlencoded POST +
    JSON response parse, for the rare upstream API that expects form fields
    rather than a JSON body (URLhaus). Same no-credentials-in-URL discipline;
    URLhaus itself is keyless.

All three transports route through _NO_REDIRECT_OPENER (never the bare
urllib.request.urlopen/global opener) and cap the response read at
MAX_RESPONSE_BYTES -- see those two definitions below for why.

get_json/post_form RAISE on transport failure or a non-JSON body (mirrors
post_json, which already raises the same way) -- they are bare transports,
not resilience wrappers. Each connector's own _gated() closure catches
around its transport call and returns a clean {"ok": False, "status": ...}
result (never letting a malformed response or an unreachable host raise into
the caller) -- see enrich/open_meteo.py etc. for the pattern.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

DEFAULT_TIMEOUT = 10.0

MAX_RESPONSE_BYTES = 262_144  # 256 KiB -- generous headroom for these small
# JSON APIs (weather/threat-intel/wiki-extract payloads). Hostile self-review
# fix (appsec finding, LOW): resp.read() with no argument reads an unbounded
# body into memory before json.loads -- a slow or malicious upstream (or a
# compromised host behind a pinned URL) could stream gigabytes and OOM the
# process. See _read_capped below.

_DISABLED_RESULT = {"ok": False, "status": "disabled"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Blocks HTTP redirects entirely (identical pattern to sources/ssdp.py's
    _NoRedirect / sources/onvif.py's _NoRedirect). Hostile self-review fix
    (appsec finding, HIGH): urllib.request.urlopen() with no custom opener
    follows 3xx redirects by DEFAULT and forwards the original request
    headers -- including a connector credential, e.g. AbuseIPDB's Key header
    -- to whatever host the redirect points at. A compromised or malicious
    upstream (or a DNS/redirect trick) could exfiltrate the credential, or
    redirect the request to an internal LAN host, bypassing the pinned/
    official-host discipline every connector documents. Returning None here
    tells urllib to treat the redirect response as terminal: no follow, no
    header forwarded anywhere else -- it surfaces to the caller as an
    urllib.error.HTTPError on the original status code, same as any other
    non-2xx response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _read_capped(resp, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read at most max_bytes from resp, and raise a clean ValueError if the
    body is larger instead of either (a) reading it all into memory
    unbounded, or (b) silently truncating it (which would otherwise surface
    downstream as a confusing json.JSONDecodeError on truncated JSON)."""
    body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("response exceeded %d-byte cap" % max_bytes)
    return body


def guarded_call(store, connector_id: str, fn: Callable[[], dict],
                  disabled_result: dict | None = None) -> dict:
    """Run fn() only if store.is_enabled(connector_id) is True right now AND the
    system-toggles egress master (store.egress_allowed()) is on. Returns
    disabled_result (default {"ok": False, "status": "disabled"}) with NO call
    to fn at all when either gate is off -- fail-closed, zero network attempted.
    store needs only an is_enabled(id) -> bool method (the real ConnectorStore
    or a test double both satisfy this); `egress_allowed` is read via getattr
    with a True default so every existing test double lacking that method
    (pre-dating the system-toggles feature) keeps behaving exactly as before --
    only the real ConnectorStore's actual master switch can ever block here."""
    if not store.is_enabled(connector_id):
        return dict(disabled_result) if disabled_result is not None else dict(_DISABLED_RESULT)
    gate = getattr(store, "egress_allowed", None)
    if gate is not None and not gate():
        return dict(disabled_result) if disabled_result is not None else dict(_DISABLED_RESULT)
    return fn()


def post_json(url: str, payload: dict, headers: dict | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Minimal stdlib JSON POST -- no third-party HTTP dependency. Whenever a
    connector can put a credential in a header instead of the URL, it should
    (mirrors narrator._post_json's guarantee that a urllib error/traceback,
    which surfaces the URL + status but not headers, can never echo a key).
    Redirects are blocked and the response read is capped -- see
    _NO_REDIRECT_OPENER / MAX_RESPONSE_BYTES above."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # nosec B310 (fixed https endpoint, timeout set, redirects blocked)
        return json.loads(_read_capped(resp).decode("utf-8"))


def get_json(url: str, headers: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Minimal stdlib JSON GET -- no third-party HTTP dependency. A credential
    (when a connector sends one, e.g. AbuseIPDB's API key) belongs ONLY in
    headers, never appended to url -- so a urllib error/traceback (which
    surfaces the URL, never headers) can never echo it. Redirects are blocked
    and the response read is capped -- see _NO_REDIRECT_OPENER /
    MAX_RESPONSE_BYTES above."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # nosec B310 (pinned official host, timeout set, redirects blocked)
        return json.loads(_read_capped(resp).decode("utf-8"))


def post_form(url: str, fields: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Minimal stdlib application/x-www-form-urlencoded POST + JSON response
    parse -- for the rare upstream API (URLhaus) that expects form fields
    rather than a JSON body. Same no-credentials-in-URL discipline as
    post_json; URLhaus itself is keyless so this carries no auth header.
    Redirects are blocked and the response read is capped -- see
    _NO_REDIRECT_OPENER / MAX_RESPONSE_BYTES above."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # nosec B310 (pinned official host, timeout set, redirects blocked)
        return json.loads(_read_capped(resp).decode("utf-8"))
