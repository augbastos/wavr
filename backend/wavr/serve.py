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

F1 (appsec re-audit, 2026-07): this module is NOT the one place a listening uvicorn
socket opens -- the Dockerfile/docker-compose/scripts/wavr.ps1 all launch
`uvicorn wavr.app:app` DIRECTLY, bypassing this launcher entirely (that used to be
this module's own claim, and it was false). The global request-body-size cap
(`MaxBodySizeMiddleware`) is therefore now applied in `wavr.app`, wrapping the
MODULE-LEVEL `app` singleton itself, so every entry point -- this launcher included
-- carries it by construction. `main()` below re-reads `WAVR_MAX_BODY_BYTES` at call
time and updates that SAME instance's `_max_bytes` in place (never re-wraps), so a
same-process env override still takes effect without a double-wrapped ASGI chain.
"""
from __future__ import annotations

import os

import uvicorn

from wavr.app import DEFAULT_MAX_BODY_BYTES, MaxBodySizeMiddleware, app
from wavr.config import load_config


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
    # `app` (imported from wavr.app) is ALREADY wrapped in MaxBodySizeMiddleware at
    # module level (F1) -- re-wrapping it here would double-apply the guard (harmless
    # functionally, since an over-cap request would just be rejected by the outer
    # layer before the inner one ever saw it, but two ASGI hops for every request for
    # no benefit). Instead, re-read the env var at call time and update the SAME
    # instance's `_max_bytes` in place, preserving WAVR_MAX_BODY_BYTES's late-binding
    # configurability (e.g. a test that sets the env var right before calling main()).
    bound_app = app
    bound_app._max_bytes = int(os.getenv("WAVR_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
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
