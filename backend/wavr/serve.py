"""`python -m wavr.serve` — the launcher that binds uvicorn to the wired app.

This is where local TLS (ADR-0006 §6, Phase 2) is applied. The FastAPI app itself
does not serve; uvicorn does, and only uvicorn knows the socket, so cert selection
lives here rather than in `app.py`.

Two modes, decided by `WAVR_MULTIDEVICE`:

  * OFF (default) — plain HTTP on 127.0.0.1, exactly as before. No cert is touched
    and `wavr.tls` (hence `cryptography`) is never imported.
  * ON — bind `WAVR_BIND` over HTTPS/WSS, generating (or reusing) a local
    self-signed cert via `ensure_cert`, so LAN tokens/stream are no longer
    plaintext (closes audit H2).

Port comes from `WAVR_PORT` (default 8000).
"""
from __future__ import annotations

import os

import uvicorn
from starlette.responses import JSONResponse

from wavr.app import app
from wavr.config import load_config

# Global request-body-size cap (audit HIGH: pre-auth resource exhaustion) -- app.py
# itself is transport-agnostic (imported by both this launcher AND every test's
# create_app(), which never binds a socket), so this is the one place a listening
# uvicorn actually starts and where the cap belongs. Default sits comfortably above
# the largest legitimate body Wavr accepts today (housemap.MAX_DOC_BYTES = 5 MiB for a
# full house-map PUT; calib_store._MAX_JSON_BYTES = 4 KiB for a calibration blob) so a
# real house-map/calibration save is never rejected, while still bounding an
# unauthenticated caller's worst case to a few MB rather than uvicorn's unbounded
# default. Configurable via WAVR_MAX_BODY_BYTES; <= 0 disables the guard (an explicit
# opt-out, not the default).
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024  # 8 MiB


class MaxBodySizeMiddleware:
    """Pure-ASGI wrapper -- deliberately NOT installed via FastAPI's
    ``app.add_middleware()`` (Starlette's ``ServerErrorMiddleware`` sits OUTSIDE every
    ``add_middleware()`` entry; an exception raised from inside our own guard would
    still propagate through it, which sends its OWN 500 response before re-raising --
    a double `send()`). Wrapping the ASGI callable directly here instead puts this
    guard entirely OUTSIDE that stack, so it can hand back one clean response itself.

    Two checks, cheapest first:
      1. ``Content-Length``: an honest client's declared size is checked BEFORE any
         body is read -- the common case, zero bytes consumed for an oversized request.
      2. Streamed drain-and-replay: covers a client that omits ``Content-Length``
         (chunked transfer) or under-declares it. Reads at most ``max_bytes + 1`` bytes
         before either rejecting (413, nothing forwarded to the app) or replaying the
         buffered chunks verbatim to the wrapped app (the same trick
         ``wavr.mcp_http._buffer_body`` already uses) -- a within-budget request is
         byte-identical to today; an over-budget one never reaches the app at all, so
         it can never partially consume memory/CPU parsing it.
    """

    def __init__(self, asgi_app, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self._app = asgi_app
        self._max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        # Only HTTP requests carry a body this way; websocket/lifespan scopes pass
        # straight through untouched (e.g. /ws/live streaming is unaffected).
        if scope.get("type") != "http" or self._max_bytes <= 0:
            await self._app(scope, receive, send)
            return

        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None  # malformed header -- fall through to the drain guard
                if declared is not None and declared > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
                break

        total = 0
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            total += len(message.get("body", b"") or b"")
            if total > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break

        it = iter(messages)

        async def _replay():
            try:
                return next(it)
            except StopIteration:
                return await receive()

        await self._app(scope, _replay, send)

    async def _reject(self, scope, receive, send) -> None:
        await JSONResponse({"detail": "request body too large"}, status_code=413)(
            scope, receive, send)


def main() -> None:
    cfg = load_config()
    # Warm up torch/ultralytics in the MAIN thread before uvicorn's event loop starts.
    # On Windows, torch's c10.dll initialization (WinError 1114) fails when torch is first
    # imported LATE inside a `to_thread` worker of the already-running server, even though
    # it imports cleanly standalone — so YOLO person-detection silently died and camera
    # rooms vanished. Importing it here, early, in the main thread fixes the load context.
    # Fully guarded: a base install without the [camera] extra just skips this (no torch),
    # and a genuine torch/DLL failure is logged, never crashing startup.
    import logging as _lg
    try:
        from ultralytics import YOLO as _Y  # noqa: F401  (import for its side effect: load torch now)
        _lg.getLogger("wavr").info("torch/ultralytics warmed up in main thread (camera detection ready)")
    except ImportError:
        pass  # [camera] extra not installed — normal for a network-only install
    except Exception:
        _lg.getLogger("wavr").warning(
            "torch/ultralytics main-thread warm-up failed; camera detection may be unavailable",
            exc_info=True,
        )
    max_body_bytes = int(os.getenv("WAVR_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
    bound_app = MaxBodySizeMiddleware(app, max_bytes=max_body_bytes)
    if cfg.multidevice:
        # LAN mode: HTTPS/WSS with a local cert. tls + cryptography are imported
        # ONLY here, so the default (off) path never needs the [tls] extra.
        from wavr.sources.network import _local_ipv4
        from wavr.tls import ensure_cert

        local_ip = _local_ipv4() or "127.0.0.1"
        cert_file, key_file = ensure_cert(cfg.tls_cert, cfg.tls_key, local_ip)
        uvicorn.run(
            bound_app,
            host=cfg.bind_host,
            port=cfg.port,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
    else:
        # Default: loopback-only plain HTTP, byte-identical to today.
        uvicorn.run(bound_app, host="127.0.0.1", port=cfg.port)


if __name__ == "__main__":
    main()
