"""Android bootstrap for the Wavr Core — the ONLY Python this app adds.

ADR-0010 chose to embed the existing Core rather than re-implement it, and this
module is the seam that makes that literal: it applies an environment, starts
`wavr.app` under uvicorn, and hands two questions back to Kotlin. It contains no
fusion, no auth, no storage, no protocol. If it ever grows any of those, the
decision has been quietly reversed.

Called from `PythonRuntime.Chaquopy` via `Python.getModule("wavr_android")`:

    start(env_json) -> json    boot the server with this environment
    stop()          -> json    ask uvicorn to exit (the interpreter stays)
    status()        -> json    what is actually happening right now
    recommend(m)    -> json    capabilities.recommend() on an Android manifest

Every function returns a JSON **string**, never raises, and never returns a
value it did not verify. `serving` is read off uvicorn's own `started` flag, not
inferred from "we called start and nothing threw".
"""
from __future__ import annotations

import json
import os
import threading
import traceback

__all__ = ["start", "stop", "status", "recommend"]

_LOCK = threading.Lock()
_SERVER = None          # uvicorn.Server, once running
_THREAD = None          # the thread it runs on
_LAST_ERROR = None      # str | None — the last failure, class name + message
_BIND = None            # (host, port, scheme) actually configured

# THE CORE STARTS ONCE PER PROCESS, and this flag is how we refuse to pretend
# otherwise. Measured, not assumed: after `stop()`, calling `start()` again in
# the same interpreter brings uvicorn back up but the Core behind it is already
# torn down — `wavr.app` builds its stores as module-level singletons and closes
# them in its shutdown handler, so the second boot logs
# `sqlite3.ProgrammingError: Cannot operate on a closed database` and the sensing
# sources crash. Reloading 128 modules with `importlib.reload` is not a fix, it
# is a different set of bugs.
#
# So a restart is a PROCESS restart. Kotlin's side of that contract is
# `CoreService.requestRestart()`, which kills the process and lets Android bring
# the app back; this flag makes the Python side fail loudly instead of serving a
# half-dead Core that looks fine from the outside.
_STOPPED_ONCE = False


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw})


def _err(code: str, detail: str = "") -> str:
    return json.dumps({"ok": False, "error": code, "detail": detail})


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

def start(env_json: str) -> str:
    """Apply `env_json` to `os.environ` and start the Core in a daemon thread.

    Idempotent: a second call while the server is already up is a no-op that
    reports the existing bind, because `CoreService` is `START_STICKY` and will
    genuinely call this twice after a process restart.

    WHY NOT `wavr.serve.main()`. That launcher calls `uvicorn.run()`, which
    constructs a Server internally and never hands it back — so there would be
    no object to set `should_exit` on and `stop()` could only be implemented by
    killing the process. The branching below is deliberately the *same* branching
    `serve.py` performs (same `load_config`, same `ensure_cert`, same two
    branches) with a Server handle kept; it reuses those helpers rather than
    re-deciding anything.
    """
    global _SERVER, _THREAD, _LAST_ERROR, _BIND

    with _LOCK:
        if _SERVER is not None and getattr(_SERVER, "started", False):
            host, port, scheme = _BIND or ("127.0.0.1", 8000, "http")
            return _ok(already=True, host=host, port=port, scheme=scheme)

        if _STOPPED_ONCE:
            # See _STOPPED_ONCE. Refusing here is the honest answer; silently
            # re-serving a Core whose database handles are closed would look
            # healthy and behave like a haunted house.
            return json.dumps({
                "ok": False,
                "error": "restart_unsupported",
                "restartRequired": True,
                "detail": (
                    "The Wavr Core has already run in this process and cannot be "
                    "started again without restarting the app."
                ),
            })

        try:
            _apply_env(env_json)
        except Exception as exc:  # noqa: BLE001
            _LAST_ERROR = f"{type(exc).__name__}"
            return _err("bad_env", type(exc).__name__)

        try:
            import uvicorn
            from wavr.app import DEFAULT_MAX_BODY_BYTES, app
            from wavr.config import load_config
        except Exception as exc:  # noqa: BLE001
            # An import failure here is the single most likely way this whole
            # architecture breaks (a wheel that did not make it into the APK), so
            # it gets a real traceback in logcat rather than a shrug. The
            # traceback is code paths and module names, never user data.
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            return _err("import_failed", type(exc).__name__)

        try:
            cfg = load_config()
            # Same in-place update serve.py performs: `app` is already wrapped in
            # MaxBodySizeMiddleware at module level, so re-wrapping would double
            # the ASGI chain.
            app._max_bytes = int(
                os.getenv("WAVR_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))
            )

            if cfg.multidevice:
                # ADR-0006 opt-in: LAN bind over self-signed TLS.
                from wavr.sources.network import _local_ipv4
                from wavr.tls import ensure_cert

                local_ip = _local_ipv4() or "127.0.0.1"
                cert_file, key_file = ensure_cert(cfg.tls_cert, cfg.tls_key, local_ip)
                conf = uvicorn.Config(
                    app,
                    host=cfg.bind_host,
                    port=cfg.port,
                    ssl_certfile=cert_file,
                    ssl_keyfile=key_file,
                    log_level="info",
                    # No uvloop/httptools on Android — neither has a wheel, and
                    # asking for them would fail at import instead of degrading.
                    loop="asyncio",
                    http="h11",
                )
                _BIND = (cfg.bind_host, cfg.port, "https")
            else:
                # ADR-0002 §1 default: loopback, plain HTTP, nothing else.
                conf = uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=cfg.port,
                    log_level="info",
                    loop="asyncio",
                    http="h11",
                )
                _BIND = ("127.0.0.1", cfg.port, "http")

            server = uvicorn.Server(conf)
            # uvicorn only installs signal handlers on the main thread, so a
            # worker thread is safe here — and required, because this call must
            # return to Kotlin promptly.
            thread = threading.Thread(
                target=server.run, name="wavr-uvicorn", daemon=True
            )
            thread.start()
            _SERVER, _THREAD, _LAST_ERROR = server, thread, None
            host, port, scheme = _BIND
            return _ok(host=host, port=port, scheme=scheme)
        except Exception as exc:  # noqa: BLE001
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            return _err("start_failed", type(exc).__name__)


def stop() -> str:
    """Ask uvicorn to finish. The interpreter stays — see PythonRuntime.stop.

    After this, THIS PROCESS CAN NEVER SERVE AGAIN (see _STOPPED_ONCE), so the
    reply carries `restartRequired` and Kotlin must treat a subsequent "start"
    as a process restart, not a method call.
    """
    global _SERVER, _THREAD, _STOPPED_ONCE
    with _LOCK:
        server, thread = _SERVER, _THREAD
        if server is None:
            return _ok(stopped=True, already=True, restartRequired=_STOPPED_ONCE)
        try:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=10.0)
            still_alive = bool(thread is not None and thread.is_alive())
            _SERVER, _THREAD = None, None
            _STOPPED_ONCE = True
            # Report the truth: if the thread did not join we say so rather than
            # claiming a clean stop the operator can act on.
            return _ok(stopped=not still_alive, lingering=still_alive,
                       restartRequired=True)
        except Exception as exc:  # noqa: BLE001
            _STOPPED_ONCE = True
            return _err("stop_failed", type(exc).__name__)


def status() -> str:
    """Never guesses. `serving` is uvicorn's own flag."""
    server = _SERVER
    thread = _THREAD
    host, port, scheme = _BIND or (None, None, None)
    payload = {
        "kind": "chaquopy",
        "bundled": True,
        "running": bool(thread is not None and thread.is_alive()),
        "serving": bool(server is not None and getattr(server, "started", False)),
        "host": host,
        "port": port,
        "scheme": scheme,
        # True once this interpreter has served and stopped: nothing short of a
        # new process will make it serve again.
        "restartRequired": _STOPPED_ONCE,
    }
    if _LAST_ERROR:
        payload["lastError"] = _LAST_ERROR
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Capability manifest + recommendation
# ---------------------------------------------------------------------------

def recommend(android_manifest_json: str) -> str:
    """`capabilities.recommend()`, fed a manifest Android actually knows.

    Two halves, and the split is the point:

      * `scan_host()` runs IN THIS PROCESS and gets the things a process can see
        about itself — RAM, CPU count, `detect_platform()` (which already
        special-cases Android by name), plus the software abilities (`docker`,
        `raw_socket`, `mdns`, `network_scan`, `serial_port_support`) that it can
        genuinely test.
      * The Kotlin manifest supplies what only `PackageManager` knows: the
        hardware feature flags.

    Merged, then handed to the *unmodified* `recommend()`. No Kotlin
    reimplementation exists and none is wanted — a phone and a laptop must not be
    able to disagree about what a phone is for.
    """
    try:
        from wavr.capabilities import (
            CapabilityManifest, ROLE_CORE, recommend as _recommend, scan_host,
        )
    except Exception as exc:  # noqa: BLE001
        return _err("import_failed", type(exc).__name__)

    try:
        host = scan_host()
    except Exception:  # noqa: BLE001 -- scan_host already swallows, belt and braces
        host = CapabilityManifest(platform="android")

    try:
        raw = json.loads(android_manifest_json or "{}")
    except Exception:  # noqa: BLE001
        raw = {}

    # Parsed through the SAME bounded validator any manifest off the wire goes
    # through (§5.2): allowlisted keys, booleans only, clamped ints. Our own
    # Kotlin wrote this one, but "we wrote it" is not a security property.
    try:
        android = CapabilityManifest.from_dict(raw)
    except Exception:  # noqa: BLE001
        android = CapabilityManifest(platform="android")

    merged = _merge(host, android)

    try:
        space_has_core = _space_has_primary_core()
    except Exception:  # noqa: BLE001
        # Unknown, and it must not become a silent "no": a wrong False here
        # recommends a SECOND primary Core into a Space that already has one.
        return json.dumps({
            "ok": True,
            "manifest": merged.to_dict(),
            "recommendation": None,
            "undetermined": "space_state",
            "detail": "Could not read whether this Space already has a Core.",
        })

    rec = _recommend(merged, space_has_core=space_has_core)
    return json.dumps({
        "ok": True,
        "manifest": merged.to_dict(),
        "recommendation": rec.to_dict(),
        "spaceHasCore": space_has_core,
        "canHostCore": merged.supports(ROLE_CORE),
    })


def _merge(host, android):
    """Android's hardware answers win; everything else is the host scan's.

    `hasSystemFeature` is definitive, so an Android `true`/`false` overrides a
    host `None` **and** a host guess. Keys the Kotlin omitted are untouched, so
    an unknown stays unknown rather than being flipped to False — the §5.1
    honesty rule survives the merge, which is the only place it could quietly
    have been lost.
    """
    from wavr.capabilities import CapabilityManifest

    caps = dict(host.capabilities)
    for key, value in (android.capabilities or {}).items():
        if value is not None:
            caps[key] = value

    functions = tuple(dict.fromkeys(tuple(android.functions_supported) or tuple(host.functions_supported)))

    return CapabilityManifest(
        platform="android",
        arch=android.arch or host.arch,
        os_version=android.os_version or host.os_version,
        functions_supported=functions,
        capabilities=caps,
        # The host scan owns RAM/CPU/tier: `_compute_tier` must keep classifying
        # every platform the same way. Android's figure is only a fallback.
        ram_mb=host.ram_mb if host.ram_mb is not None else android.ram_mb,
        cpu_count=host.cpu_count if host.cpu_count is not None else android.cpu_count,
        compute_tier=host.compute_tier,
        model=android.model or host.model,
        protocol_version=host.protocol_version,
    )


def _space_has_primary_core() -> bool:
    """Does this Space already have a primary Core?

    Read with the same public stores `api_space.capability_scan` uses, against
    the same `WAVR_DB`. Raises if it cannot tell, so the caller reports
    "undetermined" instead of a confident wrong answer.
    """
    from wavr.core_registry import CoreRegistry, STATUS_PRIMARY
    from wavr.space_store import SpaceStore

    db = os.getenv("WAVR_DB", "wavr.db")
    space = SpaceStore(db).get_space()
    if not space:
        return False
    return any(c.status == STATUS_PRIMARY for c in CoreRegistry(db).list_cores())


# ---------------------------------------------------------------------------

def _apply_env(env_json: str) -> None:
    """Apply Kotlin's environment.

    An empty value is WRITTEN AS "", never deleted, and that is a correctness
    fix rather than a style choice. The first version of this function popped
    empty keys, on the reasoning that removing a variable is the conservative
    direction. Running it proved the opposite: `wavr.config` calls
    `dotenv.load_dotenv()` at import, which walks up from the package and fills
    in anything `os.environ` does not already define. Popping `WAVR_MULTIDEVICE`
    therefore handed the decision to a `.env` file — and the Core came up on
    HTTPS with LAN mode ON when the caller had explicitly asked for loopback.

    Setting `""` is both safe and dotenv-proof: `load_config` reads the flag as
    `os.getenv(...).lower() in ("1","true","yes")`, for which `""` is false, and
    an explicitly-present variable is never overwritten by dotenv.

    A JSON `null` is the one thing that still deletes, for the rare case where a
    caller genuinely wants the default rather than an override.
    """
    env = json.loads(env_json or "{}")
    if not isinstance(env, dict):
        raise ValueError("environment must be an object")
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
