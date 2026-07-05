"""A5.1 optional local-API token — same-machine defense-in-depth (DEFAULT-OFF).

Wavr's loopback middleware already denies every LAN/internet peer (multidevice
off). What loopback does NOT stop is ANOTHER process on the same box (other local
software, or a malicious http://127.0.0.1 page) opening a loopback socket and
driving the local API. The `require_local` CSRF header defends the browser case;
it does NOT stop a native local process, which can set any header it likes.

This module adds an OPTIONAL shared secret that, when configured, must accompany
every /api/* request even on loopback. It is pure defense-in-depth and is a strict
no-op when unset:

    WAVR_LOCAL_TOKEN unset/empty  -> disabled; behavior byte-identical to before.
    WAVR_LOCAL_TOKEN=<secret>     -> that literal secret is required.
    WAVR_LOCAL_TOKEN=auto         -> Wavr generates a token once, persists it next
                                     to the db with best-effort 0600, and prints it
                                     ONE time to stdout. It is never returned by any
                                     API, never placed in /api/status, never logged
                                     after that single line.

HONEST LIMITATION: a same-machine process that can read the shell HTML or the token
file could still obtain the token. 0600 + never-in-responses raise the bar; they do
NOT make loopback a hard trust boundary. This is depth, not a fix.
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

_LOG = logging.getLogger(__name__)
_TOKEN_FILENAME = "local_token"


def _token_path(db_path: str) -> Path:
    """Persist the auto-token next to the db file (same git-ignored dir as wavr.db).
    For ':memory:' / empty db paths, fall back to the current directory."""
    if not db_path or db_path == ":memory:":
        return Path.cwd() / _TOKEN_FILENAME
    return Path(db_path).resolve().parent / _TOKEN_FILENAME


def _read_or_create(path: Path) -> str:
    """Return the persisted token, generating+persisting one on first use. Never
    raises: on any FS error we fall back to an in-memory-only token so the feature
    still works (it just won't survive a restart)."""
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except Exception:
        _LOG.warning("local token read failed; regenerating", exc_info=True)
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token, encoding="utf-8")
        with __import__("contextlib").suppress(Exception):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (best-effort on Windows)
    except Exception:
        _LOG.warning("local token persist failed; using in-memory token", exc_info=True)
    return token


def resolve_local_token(local_token_cfg: str, db_path: str) -> str:
    """Resolve WAVR_LOCAL_TOKEN into the effective secret.

    "" -> disabled (returns ""); "auto" -> read/generate+persist and print ONCE;
    anything else -> used verbatim. The raw token must NEVER be echoed by any API
    or logged beyond the single 'auto' stdout line here."""
    cfg = (local_token_cfg or "").strip()
    if not cfg:
        return ""
    if cfg.lower() == "auto":
        token = _read_or_create(_token_path(db_path))
        # ONE-time disclosure to the operator on stdout (Jupyter-style). Not via the
        # logging module (which could ship to a file/handler) and never repeated.
        print(f"Wavr local token: {token}", flush=True)
        return token
    return cfg
