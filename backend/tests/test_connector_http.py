"""Transport-layer appsec tests for connectors/http.py -- the shared egress
chokepoint used by every enrich connector (open_meteo/urlhaus/abuseipdb/
wikipedia) via get_json/post_json/post_form.

Both fixes here run through the REAL transport (get_json/post_json/
post_form calling the REAL _NO_REDIRECT_OPENER / _NoRedirect), against a
throwaway loopback (127.0.0.1) HTTP server started per test -- never a
real/external host, so this stays inside Wavr's zero-cloud-egress invariant
while still proving urllib's actual redirect/read behavior. A dependency-
injected fake (as used by the enrich/notify connector tests) would only
prove the CALL SITE routes through get_json/post_json/post_form -- not that
the shared transport module itself blocks redirects or caps reads.

  * SSRF-adjacent redirect fix (HIGH): a 3xx response from the fake server
    must NOT be followed -- it must surface as urllib.error.HTTPError,
    proving _NO_REDIRECT_OPENER (not the raw global urlopen, which
    auto-follows) is what all three transports actually use.

  * Unbounded-read fix (LOW): a body larger than MAX_RESPONSE_BYTES must
    raise a clean error rather than being read unbounded into memory.
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.error

import pytest

from wavr.connectors import http as wavr_http


class _Handler(http.server.BaseHTTPRequestHandler):
    """Scripted per-test response via class attributes, read fresh on every
    request -- each test sets these explicitly before calling the
    transport, so no state leaks between tests."""

    response_status = 200
    response_headers: dict = {}
    response_body = b"{}"

    def _respond(self):
        body = self.response_body
        self.send_response(self.response_status)
        for k, v in self.response_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._respond()

    def log_message(self, *args):
        pass  # keep pytest output clean -- no per-request access log


@pytest.fixture
def fake_server():
    """A throwaway loopback (127.0.0.1) HTTP server, one per test, torn
    down at the end. Never reaches outside the box."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _url(server, path: str = "/") -> str:
    host, port = server.server_address
    return "http://%s:%d%s" % (host, port, path)


# --------------------------------------------------------------------------- #
# Fix 1 (HIGH): redirects are blocked, not followed.
# --------------------------------------------------------------------------- #
def test_get_json_does_not_follow_redirect(fake_server):
    _Handler.response_status = 302
    _Handler.response_headers = {"Location": "http://example.invalid/evil"}
    _Handler.response_body = b""
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        wavr_http.get_json(_url(fake_server), headers={"Key": "secret-value"})
    assert exc_info.value.code == 302  # surfaced as an error on the ORIGINAL status


def test_post_json_does_not_follow_redirect(fake_server):
    _Handler.response_status = 303
    _Handler.response_headers = {"Location": "http://example.invalid/evil"}
    _Handler.response_body = b""
    with pytest.raises(urllib.error.HTTPError):
        wavr_http.post_json(_url(fake_server), {"q": "x"})


def test_post_form_does_not_follow_redirect(fake_server):
    _Handler.response_status = 301
    _Handler.response_headers = {"Location": "http://example.invalid/evil"}
    _Handler.response_body = b""
    with pytest.raises(urllib.error.HTTPError):
        wavr_http.post_form(_url(fake_server), {"url": "http://x"})


def test_no_redirect_handler_blocks_at_the_source():
    # Direct unit test of the exact mechanism the fix relies on:
    # HTTPRedirectHandler.redirect_request returning None is what makes
    # urllib treat a 3xx as terminal instead of auto-following it.
    result = wavr_http._NoRedirect().redirect_request(
        None, None, 302, "Found", {}, "http://example.invalid/evil")
    assert result is None


# --------------------------------------------------------------------------- #
# Fix 2 (LOW): oversize response body is capped, not read unbounded.
# --------------------------------------------------------------------------- #
def test_get_json_caps_oversize_body(fake_server):
    oversize = b"x" * (wavr_http.MAX_RESPONSE_BYTES + 1)
    _Handler.response_status = 200
    _Handler.response_headers = {"Content-Type": "application/json"}
    _Handler.response_body = oversize
    with pytest.raises(ValueError):
        wavr_http.get_json(_url(fake_server))


def test_get_json_accepts_body_at_the_cap(fake_server):
    # Exactly MAX_RESPONSE_BYTES must still succeed -- proves the cap isn't
    # off-by-one in the wrong direction.
    payload = json.dumps({"k": "v"}).encode("utf-8")
    padded = payload + b" " * (wavr_http.MAX_RESPONSE_BYTES - len(payload))
    assert len(padded) == wavr_http.MAX_RESPONSE_BYTES
    _Handler.response_status = 200
    _Handler.response_headers = {"Content-Type": "application/json"}
    _Handler.response_body = padded
    assert wavr_http.get_json(_url(fake_server)) == {"k": "v"}


def test_post_form_caps_oversize_body(fake_server):
    oversize = b"y" * (wavr_http.MAX_RESPONSE_BYTES + 1)
    _Handler.response_status = 200
    _Handler.response_headers = {"Content-Type": "application/json"}
    _Handler.response_body = oversize
    with pytest.raises(ValueError):
        wavr_http.post_form(_url(fake_server), {"host": "x.example"})


# --------------------------------------------------------------------------- #
# guarded_call: the system-toggles egress master ANDs on top of is_enabled --
# a blocked master must stop fn() from EVER running, same fail-closed shape
# as the connector-off path (feature "system-toggles").
# --------------------------------------------------------------------------- #
class _Store:
    def __init__(self, enabled: bool, egress: bool = True):
        self._enabled = enabled
        self._egress = egress

    def is_enabled(self, connector_id: str) -> bool:
        return self._enabled

    def egress_allowed(self) -> bool:
        return self._egress


class _StoreNoMaster:
    """Pre-system-toggles test double: no egress_allowed() at all. guarded_call
    must fall back to allowed (getattr default) -- zero behaviour change for
    every existing connector test double that predates this feature."""
    def __init__(self, enabled: bool):
        self._enabled = enabled

    def is_enabled(self, connector_id: str) -> bool:
        return self._enabled


def test_guarded_call_blocked_by_egress_master_even_when_connector_enabled():
    calls = []
    result = wavr_http.guarded_call(_Store(enabled=True, egress=False), "open-meteo",
                                    lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": False, "status": "disabled"}
    assert calls == []                 # fn() never invoked -- zero network attempted


def test_guarded_call_allowed_when_connector_enabled_and_egress_allowed():
    calls = []
    result = wavr_http.guarded_call(_Store(enabled=True, egress=True), "open-meteo",
                                    lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": True}
    assert calls == [1]


def test_guarded_call_still_blocked_by_connector_off_regardless_of_master():
    calls = []
    result = wavr_http.guarded_call(_Store(enabled=False, egress=True), "open-meteo",
                                    lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": False, "status": "disabled"}
    assert calls == []


def test_guarded_call_tolerates_store_without_egress_allowed_method():
    # A pre-system-toggles test double (no egress_allowed method) must behave
    # exactly as before: enabled -> fn() runs, no AttributeError.
    calls = []
    result = wavr_http.guarded_call(_StoreNoMaster(enabled=True), "open-meteo",
                                    lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": True}
    assert calls == [1]
