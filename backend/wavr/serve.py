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

import uvicorn

from wavr.app import app
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
    if cfg.multidevice:
        # LAN mode: HTTPS/WSS with a local cert. tls + cryptography are imported
        # ONLY here, so the default (off) path never needs the [tls] extra.
        from wavr.sources.network import _local_ipv4
        from wavr.tls import ensure_cert

        local_ip = _local_ipv4() or "127.0.0.1"
        cert_file, key_file = ensure_cert(cfg.tls_cert, cfg.tls_key, local_ip)
        uvicorn.run(
            app,
            host=cfg.bind_host,
            port=cfg.port,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
    else:
        # Default: loopback-only plain HTTP, byte-identical to today.
        uvicorn.run(app, host="127.0.0.1", port=cfg.port)


if __name__ == "__main__":
    main()
