"""Self-hosted ntfy notifications (opt-in, local, derived-only).

Wavr can optionally push a short human-readable alert to the user's OWN
self-hosted ntfy (https://ntfy.sh/ software, self-hosted) topic when a derived
edge event happens: the house becomes occupied/empty (AwayMonitor), or an
unknown device appears on the LAN (NetworkInventoryService). Matches Wavr's
privacy identity, same as mqtt_publisher/ha_client:

  * OPT-IN, default OFF: only active when WAVR_NTFY_URL is set (config.ntfy_url).
    Nothing is built, and no import/network happens, unless a caller injects a
    `notify` or the URL is configured.
  * DERIVED-ONLY: the payload is a short text message -- never a room name +
    coordinate, vitals, MAC, or any other raw sensing detail.
  * SELF-HOSTED: `ntfy_url` is a full topic URL on the user's own ntfy server
    (e.g. http://nas.local:8080/wavr); nothing is sent to any third-party
    cloud unless the user points the URL there themselves.
  * DEPENDENCY-FREE + INJECTABLE transport, mirroring wavr.ha_client: the
    default POST transport is stdlib `urllib.request` -- no new runtime dep.
    Tests inject a fake transport, so notify() is exercised with zero network.
  * FAILURE-TOLERANT: a dead/unreachable ntfy server must never raise into the
    caller (the away/rules loop) -- any transport error is caught and warned
    once, then silently no-ops.
  * NON-BLOCKING: `notify()` is called SYNCHRONOUSLY from async code (AwayMonitor
    on the arrived/left edge, NetworkInventoryService's on_rogue callback), so the
    (up to 5s) blocking POST must never run inline on the event loop -- it would
    freeze every other coroutine (ingest, websockets, HTTP) for the duration of a
    slow/unreachable ntfy server. When a loop is running, the POST is offloaded to
    a worker thread and notify() returns immediately (fire-and-forget); outside a
    running loop (e.g. a plain sync caller/test) it runs inline, same as before.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.request
from typing import Callable

_WARNED = False

# Fire-and-forget background tasks (the offloaded POST) must be kept referenced
# until they finish, or asyncio may garbage-collect them mid-flight and warn
# "Task was destroyed but it is pending". Discarded via the done-callback below.
_BACKGROUND_TASKS: set = set()

# A POST transport takes (url, body_bytes) and returns the raw response (or
# raises). Default is _urllib_post below; tests inject a fake that records
# the call and returns / raises whatever the test needs -- no network.
PostFn = Callable[[str, bytes], object]


def _urllib_post(url: str, body: bytes, timeout: float = 5.0) -> bytes:
    """Default POST transport: a plain stdlib POST of `body` (the ntfy message
    text) to the user's own self-hosted ntfy topic URL. No third-party HTTP
    dependency."""
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (self-hosted, user-configured URL)
        return resp.read()


def make_notifier(url: str, post: PostFn | None = None) -> Callable[[str], None]:
    """Build a `notify(message)` function that POSTs `message` (plain text) to
    `url` (a self-hosted ntfy topic).

    The returned function NEVER raises: a dead/unreachable server (or any
    other transport error) is logged once at WARNING and every call after
    that is a silent no-op, so it can never crash the rules/away loop that
    calls it. `post` is injectable for tests (no network, no new dependency).

    NON-BLOCKING: when called from inside a running event loop, the (blocking)
    POST is offloaded to a worker thread via `asyncio.to_thread` and notify()
    returns immediately -- a slow/unreachable ntfy server can no longer freeze
    the event loop. Outside a running loop there is nothing to offload from,
    so it runs the POST inline (still failure-tolerant) -- this also keeps
    notify() trivially testable synchronously with zero network.
    """
    _post = post or _urllib_post

    def _send(message: str) -> None:
        global _WARNED
        try:
            _post(url, message.encode("utf-8"))
        except Exception:
            if not _WARNED:
                logging.warning(
                    "ntfy notification failed (server unreachable at %s); "
                    "further failures will be silent", url)
                _WARNED = True

    def notify(message: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            _send(message)   # no event loop to protect -- just do it inline
            return
        # Fire-and-forget: schedule the blocking POST on a worker thread and
        # return immediately. `_send` already swallows every exception, so the
        # task can never end in an unhandled-exception warning either.
        task = loop.create_task(asyncio.to_thread(_send, message))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    return notify
