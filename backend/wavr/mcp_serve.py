"""`python -m wavr.mcp_serve` -- a stdio MCP server exposing the LIVE house.

READ-ONLY + LOCAL. This does NOT start a second sensing process: it reads the
DERIVED room state from the ALREADY-RUNNING Wavr app over loopback HTTP
(127.0.0.1:<WAVR_PORT>, the same box), and reads/controls the user's OWN Home
Assistant on the LAN via `wavr.ha_client`. So an MCP host (Claude Code, the future
Agentic OS) can ask "what does my house sense right now" and get the live answer
from the running :8000, with zero restart.

Transport = stdio (the best-tested `claude mcp add` path). Nothing binds a public
interface: the server is spawned locally by the host and speaks stdio; it only ever
makes a loopback GET to the running app + a LAN call to the user's HA. Loopback/
stdio-only exposure holds by construction.

Privacy seam: `/api/state` returns full RoomState dicts (which include vitals/targets)
to THIS bridge over loopback -- but the `get_room_context` tool strips vitals/targets
at the tool layer (wavr.mcp, audit CRITICAL-1) before anything reaches the agent, and
`list_rooms` only surfaces room/occupied/confidence. Per-person biometric/positional
data transits loopback into the local bridge only; it never reaches the agent and
never leaves the box -- the same guarantee as the in-process provider, one extra
loopback hop.

Control stays DEFAULT-OFF (ADR-0005): `cfg.mcp_control` defaults False, and the
sensitive-domain refusal + ARP-block exclusion live in `wavr.mcp` untouched.

The [mcp] extra is needed only to RUN this module; `import wavr.mcp` stays dependency
-free (the SDK import is lazy inside `build_mcp_server`).
"""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.request
from urllib.parse import urlsplit

from wavr.config import load_config
from wavr.ha_client import client_from_config
from wavr.local_token import resolve_local_token
from wavr.mcp import build_mcp_server


def _is_loopback_target(base_url: str) -> bool:
    """True only if `base_url`'s host is a loopback host (127.0.0.0/8, ::1, or the
    literal `localhost`).

    FAIL-CLOSED: any unparseable, empty, or off-box host returns False. This guards
    the WAVR_MCP_TARGET override -- the resolved local-API token is attached as
    X-Wavr-Token to whatever this URL points at (LocalApiStateProvider._urllib_get),
    so a non-loopback target would send the same-box secret off the box AND make the
    fetch a real egress, breaking Wavr's loopback/stdio-only invariant. The bridge is
    designed to read the SAME-box app only, so rejecting a non-loopback target is
    correct, not a limitation."""
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalApiStateProvider:
    """A `wavr.mcp.StateProvider` backed by the running Wavr app on loopback.

    LIVE + READ-ONLY: every call issues a plain GET to the local app; no state is
    mutated and nothing is cached across calls, so the agent always sees what the
    house senses right now. The `fetch` transport is INJECTABLE (mirrors
    `wavr.ha_client`) so tests drive canned bytes with zero network.
    """

    def __init__(self, base_url: str, token: str = "", fetch=None, timeout: float = 5.0):
        self._base = (base_url or "").rstrip("/")
        self._token = token or ""
        self._timeout = timeout
        self._fetch = fetch or self._urllib_get     # injectable seam (tests)

    def _urllib_get(self, path: str) -> bytes:
        # Loopback GET to the same-box app. The optional local-API token (A5.1) is sent
        # as X-Wavr-Token -- the same header the app's middleware checks (app.py:485).
        headers = {"X-Wavr-Token": self._token} if self._token else {}
        req = urllib.request.Request(self._base + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310 (loopback)
            return r.read()

    def _json(self, path: str):
        raw = self._fetch(path)
        if not raw:
            return None
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not text.strip():
            return None
        return json.loads(text)

    def list_rooms(self) -> list[str]:
        return sorted((self._json("/api/state") or {}).keys())

    def room_state(self, room: str) -> dict | None:
        return (self._json("/api/state") or {}).get(room)

    def house_map(self) -> dict:
        return self._json("/api/house") or {}


def make_server(cfg=None):
    """Wire a LIVE MCP server from config: the loopback bridge provider + the LOCAL HA
    client, with control DEFAULT-OFF. `cfg` is injectable for tests."""
    cfg = cfg or load_config()
    base = os.getenv("WAVR_MCP_TARGET", "").strip() or f"http://127.0.0.1:{cfg.port}"
    # Loopback invariant (fail-closed): WAVR_MCP_TARGET may only override the same-box
    # port/scheme, never point off the box. A non-loopback target would ship the
    # local-API token below off-box and turn the loopback GET into real egress, so we
    # refuse it outright rather than leak the secret (see _is_loopback_target).
    if not _is_loopback_target(base):
        raise ValueError(
            f"WAVR_MCP_TARGET must resolve to a loopback host (127.0.0.0/8, ::1, or "
            f"localhost); refused {base!r}. The Wavr MCP bridge reads the same-box app "
            f"only -- a non-loopback target would send the local-API token off-box and "
            f"break the loopback/stdio invariant.")
    # Same secret the app persists/reads (A5.1). Empty => the token header is omitted,
    # which is correct when the app has no local token configured.
    token = resolve_local_token(cfg.local_token, cfg.db_path)
    provider = LocalApiStateProvider(base, token)
    ha = client_from_config(cfg)     # LOCAL HA on the LAN; None => get_ha_entities -> []
    return build_mcp_server(
        provider, name="wavr", ha_client=ha,
        control_enabled=cfg.mcp_control, allowed_services=cfg.ha_allowed_services)


def main() -> None:
    # FastMCP.run() defaults to the stdio transport. The host (claude mcp add) owns the
    # process lifetime; if the app on :8000 is down, tool calls raise a clear transport
    # error rather than fabricating an empty house.
    make_server().run()


if __name__ == "__main__":
    main()
