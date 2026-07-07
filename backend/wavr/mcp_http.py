"""In-app MCP-over-streamable-HTTP mount (ADR-0008, Slice 1: secure read transport).

Mounts FastMCP's streamable-HTTP transport INSIDE the main FastAPI app at ``/mcp`` so
it inherits the app's load-bearing gates -- ``loopback_or_authed`` (app.py), the
``TrustedHostMiddleware`` Host allowlist, the self-signed TLS cert, and the
``DeviceStore`` paired-token check. There is NO second FastMCP uvicorn on its own port
(that would bypass the middleware = "inventing auth" = rejected).

Golden invariant (ADR-0008): this deliberately opens an inbound network listener. It is
only defensible because ALL THREE hold: (a) LAN-only, never internet (``authorize`` 403s
out-of-subnet peers); (b) it reuses the existing paired-token + cert-pin gate; (c) it is
DEFAULT-OFF behind the ``mcp-http`` Connectors toggle (per-request kill-switch here).

READ-ONLY BY CONSTRUCTION: the server is built with ``expose_control=False`` so
``call_ha_service`` is ABSENT from ``list_tools`` -- not merely inert. ``mcp.py``'s
control gate is process-global (not per-caller), so control must never be reachable over
the network where every paired token would get it equally. The stdio bridge
(``wavr.mcp_serve``) keeps the full gated toolset, unchanged.

Request path for ``/mcp`` (each gate fail-closed):
  1. ``loopback_or_authed`` (app.py, upstream)  -- unpaired / out-of-subnet / revoked
     -> 403 BEFORE this guard ever runs (proven: the mounted sub-app is wrapped by the
     parent ``@app.middleware('http')``).
  2. kill-switch  -- the ``mcp-http`` connector is default-OFF; a per-request
     ``is_enabled`` check 503s when disabled (REVOCABLE, no restart).
  3. Origin  -- DNS-rebind defence (the streamable-HTTP spec requirement).
     ``TrustedHostMiddleware`` covers the Host header but NOT Origin, so we validate it
     here against a host allowlist (loopback + the central's own LAN IP). Native
     (non-browser) MCP clients send no Origin and are allowed.
  4. rate-limit  -- token bucket per peer IP (stdio had none; it was local-by-
     construction). Defence-in-depth against a hammering paired peer.
  5. dispatch  -- delegate to the FastMCP session manager's ``handle_request``.

The FastMCP transport is built with ``stateless_http=True`` (Wavr auth is already
per-request stateless -- ``authorize`` re-verifies every call) and ``json_response=True``
(single JSON reply, which plays cleanly with Starlette's BaseHTTPMiddleware). FastMCP's
own ``transport_security`` DNS-rebind guard is DISABLED here because Host is enforced by
``TrustedHostMiddleware`` and Origin by this guard -- one source of truth per check,
matching the ``/ws/live`` Origin convention.

The [mcp] SDK is a LAZY import (inside ``build_mcp_http_mount``) so importing this module
never needs the extra -- mirrors ``wavr.mcp``. app.py wires the mount only when
``WAVR_MULTIDEVICE`` is on AND the extra is importable.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from starlette.responses import JSONResponse

_log = logging.getLogger("wavr.mcp.http")

# Rate-limit defaults (token bucket, per peer IP). Generous enough for an active agent
# (burst then a steady stream of read calls) while still cutting off a hammering peer.
# Module-level so a test can monkeypatch them low before create_app builds the guard.
_RATE_CAPACITY = 120          # burst size
_RATE_REFILL_PER_SEC = 4.0    # sustained rate once the burst is spent
_RATE_MAX_KEYS = 4096         # bound the per-IP table (evict oldest beyond this)

# Origin host allowlist for /mcp (DNS-rebind defence). An Origin is `scheme://host[:port]`;
# we compare the HOST only (the security identity), ignoring scheme/port. `local_ip` (the
# central's own LAN IP) is added at guard-build time so a same-origin browser client
# served by the central is allowed; an attacker page on any other host is refused.
_LOOPBACK_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_ORIGIN_RE = re.compile(r"^(?:https?)://(\[[0-9A-Fa-f:]+\]|[^/:]+)(?::\d+)?$")


def _header(scope, name: bytes) -> str | None:
    """Read a single header value (first match) from the ASGI scope, or None."""
    for k, v in scope.get("headers") or ():
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def _origin_ok(origin: str | None, local_ip: str) -> bool:
    """True if the Origin header is absent (native MCP client) or its HOST is in the
    allowlist (loopback + the central's own LAN IP). Fail-closed on any malformed Origin."""
    if origin is None:
        return True
    m = _ORIGIN_RE.match(origin.strip())
    if not m:
        return False
    host = m.group(1)
    allowed = set(_LOOPBACK_ORIGIN_HOSTS)
    if local_ip:
        allowed.add(local_ip)
    return host in allowed


class _RateLimiter:
    """Per-key token bucket. Sync + lock-guarded (the brief critical section is safe to
    hold from the event loop). `now_fn` is injectable for deterministic tests."""

    def __init__(self, capacity: int = _RATE_CAPACITY,
                 refill_per_sec: float = _RATE_REFILL_PER_SEC,
                 max_keys: int = _RATE_MAX_KEYS, now_fn=time.monotonic):
        self._cap = float(capacity)
        self._refill = float(refill_per_sec)
        self._max_keys = max_keys
        self._now = now_fn
        self._buckets: dict[str, list[float]] = {}   # key -> [tokens, last_ts]
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                if len(self._buckets) >= self._max_keys:
                    # Bound memory: drop the oldest-inserted key (CPython dict order).
                    self._buckets.pop(next(iter(self._buckets)), None)
                self._buckets[key] = [self._cap - 1.0, now]
                return True
            tokens = min(self._cap, b[0] + (now - b[1]) * self._refill)
            b[1] = now
            if tokens < 1.0:
                b[0] = tokens
                return False
            b[0] = tokens - 1.0
            return True


class _McpHttpGuard:
    """ASGI app fronting the FastMCP streamable-HTTP session manager. Applies the
    kill-switch, Origin, and rate-limit gates, then delegates. Only AUTHED peers reach
    here (``loopback_or_authed`` is upstream), so these are the transport-specific gates
    on top of authentication."""

    def __init__(self, session_manager, *, is_enabled, local_ip: str,
                 rate_limiter: _RateLimiter | None = None):
        self._sm = session_manager
        self._is_enabled = is_enabled            # callable() -> bool (per-request switch)
        self._local_ip = local_ip or ""
        self._rl = rate_limiter or _RateLimiter()

    async def __call__(self, scope, receive, send):
        # A Starlette Route only routes http scopes here, but stay fail-closed for anything
        # unexpected (never fall through to dispatch without the checks below).
        if scope.get("type") != "http":
            return

        # Gate 2 -- kill-switch (mcp-http connector default-OFF). Fail-closed if the store
        # read raises (e.g. during shutdown): treat as disabled, never dispatch.
        try:
            enabled = bool(self._is_enabled())
        except Exception:
            _log.warning("mcp-http kill-switch check failed; treating as disabled",
                         exc_info=True)
            enabled = False
        if not enabled:
            await JSONResponse({"detail": "mcp-http disabled"}, status_code=503)(
                scope, receive, send)
            return

        # Gate 3 -- Origin (DNS-rebind; TrustedHostMiddleware covers Host, not Origin).
        if not _origin_ok(_header(scope, b"origin"), self._local_ip):
            await JSONResponse({"detail": "forbidden origin"}, status_code=403)(
                scope, receive, send)
            return

        # Gate 4 -- rate-limit per peer IP (defence-in-depth).
        client = scope.get("client")
        key = client[0] if client else "unknown"
        if not self._rl.allow(key):
            await JSONResponse({"detail": "rate limited"}, status_code=429)(
                scope, receive, send)
            return

        # Gate 5 -- dispatch to FastMCP (read-only tool set; call_ha_service is absent).
        await self._sm.handle_request(scope, receive, send)


def build_mcp_http_mount(provider, *, is_enabled, local_ip: str, name: str = "wavr",
                         ha_client=None, rate_capacity: int | None = None,
                         rate_refill: float | None = None):
    """Build the READ-ONLY, stateless MCP-over-streamable-HTTP mount.

    Returns ``(route, session_manager)``:
      * ``route`` -- a ``starlette.routing.Route('/mcp', endpoint=guard)`` to append to
        the app's routes. A Route (not a Mount) resolves at EXACTLY ``/mcp`` with no
        trailing-slash redirect, mirroring how the MCP SDK exposes its own transport.
      * ``session_manager`` -- the FastMCP ``StreamableHTTPSessionManager`` whose
        ``run()`` MUST be entered in the app's lifespan (once per process) so the
        transport's task group is live.

    ``is_enabled`` is a callable ``() -> bool`` read per request (the ``mcp-http``
    kill-switch). ``local_ip`` seeds the Origin allowlist. ``ha_client`` (or None) is
    passed to the read tool ``get_ha_entities`` (LOCAL HA on the LAN; None -> []).

    The [mcp] SDK is imported HERE, lazily -- importing this module never needs the extra.
    """
    from starlette.routing import Route
    from mcp.server.transport_security import TransportSecuritySettings

    from wavr.mcp import build_mcp_server

    server = build_mcp_server(
        provider, name=name, ha_client=ha_client,
        # READ-ONLY over the network: call_ha_service is NOT registered (absent, not inert).
        control_enabled=False, expose_control=False,
        # Streamable-HTTP transport shaping.
        stateless_http=True, json_response=True,
        # Host is enforced by TrustedHostMiddleware, Origin by this guard -> disable the
        # SDK's overlapping DNS-rebind validator (one source of truth per check).
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False),
    )
    # Lazily create the session manager (SDK does this on first streamable_http_app()).
    server.streamable_http_app()
    session_manager = server.session_manager

    limiter = _RateLimiter(
        capacity=rate_capacity if rate_capacity is not None else _RATE_CAPACITY,
        refill_per_sec=rate_refill if rate_refill is not None else _RATE_REFILL_PER_SEC,
    )
    guard = _McpHttpGuard(session_manager, is_enabled=is_enabled, local_ip=local_ip,
                          rate_limiter=limiter)
    return Route("/mcp", endpoint=guard), session_manager
