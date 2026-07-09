"""Outbound HTTP to another Wavr instance's API (peer pairing/fusion/remote
config, Phase 1+). Same discipline as ha_client.py: stdlib `urllib`/`ssl`/
`http.client` only, no third-party HTTP client added to Wavr's runtime deps,
transport fully injectable so every caller is unit-testable with zero real
network.

Unlike ha_client.py (which talks to the user's OWN Home Assistant over plain
HTTP on a network the user already trusts), a peer connection is
self-signed-HTTPS with an admin-confirmed pinned fingerprint -- see
`wavr.tls.remote_cert_fingerprint` for the fetch-time TOFU probe used during
pairing itself, and `pinned_fingerprint` here for every call AFTER pairing
(where the peer's identity should already be known and MUST be re-verified
every time, not just once at pairing -- a cert that silently changed after
pairing is exactly the "someone is intercepting your network" case the
existing Mobile pairing flow's MitM screen already treats as a hard stop)."""
from __future__ import annotations

import http.client
import json
import ssl
import urllib.parse
from typing import Callable

from wavr.tls import format_fingerprint


class PeerClientError(RuntimeError):
    """A peer call failed: unreachable, TLS fingerprint mismatch, or an
    unparseable response. Callers decide how to degrade (Phase 2's
    RemoteSource reconnect-forever; Phase 4's remote-config per-peer
    failure report) -- this module only ever raises, never guesses."""


# (method, url, headers, body_bytes_or_None, pinned_fingerprint, timeout) -> response bytes
Transport = Callable[[str, str, dict, bytes | None, str | None, float], bytes]


def _default_transport(method: str, url: str, headers: dict, body: bytes | None,
                        pinned_fingerprint: str | None, timeout: float) -> bytes:
    """Real transport: opens the connection over an SSLContext that accepts
    ANY cert (self-signed peers have no CA) but, when `pinned_fingerprint` is
    given, verifies the ACTUAL presented certificate's fingerprint matches
    before trusting the response -- the same TOFU-then-pin model the
    pairing/Mobile flow already uses, just enforced on every call, not only
    at pairing time.

    Implemented with `http.client.HTTPSConnection` rather than
    `urllib.request.urlopen`: urlopen only exposes the peer certificate by
    reaching into private internals (`resp.fp.raw._sock`), which is fragile
    and version-dependent. `HTTPSConnection.sock.getpeercert()` is the same
    underlying `ssl.SSLSocket`, reached through a documented public
    attribute, and lets the fingerprint be checked BEFORE the response body
    is read -- so a mismatched peer never gets its response trusted."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or 443
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))

    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        if pinned_fingerprint is not None:
            der = conn.sock.getpeercert(binary_form=True)
            observed = format_fingerprint(der)
            if observed != pinned_fingerprint:
                raise PeerClientError(
                    f"peer certificate fingerprint mismatch: expected "
                    f"{pinned_fingerprint}, got {observed} -- possible MitM")
        resp = conn.getresponse()
        if resp.status >= 400:
            raise PeerClientError(f"peer returned HTTP {resp.status}: {resp.read()!r}")
        return resp.read()
    finally:
        conn.close()


def _call(base_url: str, path: str, method: str, body: dict | None, token: str | None,
          pinned_fingerprint: str | None, timeout: float, transport) -> dict:
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body_bytes = json.dumps(body).encode() if body is not None else None
    xport = transport or _default_transport
    try:
        raw = xport(method, url, headers, body_bytes, pinned_fingerprint, timeout)
    except PeerClientError:
        raise
    except Exception as exc:
        raise PeerClientError(f"peer call failed: {exc}") from exc
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise PeerClientError(f"peer returned unparseable response: {exc}") from exc


def post_json(base_url: str, path: str, body: dict, token: str | None = None,
              pinned_fingerprint: str | None = None, timeout: float = 5.0,
              transport=None) -> dict:
    return _call(base_url, path, "POST", body, token, pinned_fingerprint, timeout, transport)


def get_json(base_url: str, path: str, token: str | None = None,
             pinned_fingerprint: str | None = None, timeout: float = 5.0,
             transport=None) -> dict:
    return _call(base_url, path, "GET", None, token, pinned_fingerprint, timeout, transport)
