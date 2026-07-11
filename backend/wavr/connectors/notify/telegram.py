"""Telegram Bot API NOTIFY connector (surface=notify, direction=outbound,
project_wavr_connectors_vision / DESIGN-external-connectors.md section 3.3).

Builds on `notifier.py`'s proven pattern (opt-in, failure-tolerant, injectable
transport) but sends STRUCTURED alert fields through one allowlist function
(`build_alert_text`) instead of an arbitrary caller-supplied string, so the
egress payload is provably bounded to kind/severity/room/short-summary --
mirrors `narrator.build_prompt`'s "same allowlist for every caller" discipline.

CONNECTOR ID: "telegram" (a `kind="generic"` row in `connector_store`).

GATE: `make_telegram_send(store, ...)` returns a `send(...)` closure that
checks `store.is_enabled("telegram")` via the shared `guarded_call` chokepoint
on EVERY call (not once at construction) -- default-OFF, revocable, no
restart. When disabled, `send()` returns `{"ok": False, "status": "disabled"}`
and NEVER calls the transport -- zero network attempted.

EGRESS: exactly `kind`, `severity`, `room`, `summary` -- rendered as one short
line by `build_alert_text`. Never a raw camera frame, occupancy vitals/target
geometry, MAC address, or credential. This is the SAME allowlist boundary the
caller (the away/rogue-device/fall-alert callbacks per the design doc) already
respects when it builds `wavr.house_status` reasons -- this module does not
invent new disclosure, it forwards what already left the box via
GET /api/alerts and GET /api/house-status.

SUMMARY IS FREE TEXT -- SANITISED, NOT JUST TRUSTED: `kind`/`severity`/`room`
are narrow, caller-constrained values, but `summary` is an arbitrary string
the caller composes -- the allowlist-by-signature discipline above bounds
which FIELDS travel, not what a well-behaved caller puts INSIDE the `summary`
field. So a bug in a future caller (or a room label a user mistakenly typed
as a coordinate) could smuggle exactly the data this connector promises never
to send. `build_alert_text` therefore runs `summary` through
`_sanitize_summary` before it is ever concatenated into the outbound text:
MAC-like tokens (`aa:bb:cc:dd:ee:ff`), coordinate-like pairs
(`52.6598, -8.6112`), `rtsp://` URLs, and `frame`-prefixed tokens are each
replaced with a fixed `[redacted]` marker (never partially -- no truncation
heuristic that could still leak a prefix). This is a defence-in-depth
runtime gate, not a substitute for callers still only passing derived text.

SECRETS: the bot token and chat id are read from environment variables BY
NAME (`token_env`/`chat_id_env`, default `WAVR_TELEGRAM_TOKEN` /
`WAVR_TELEGRAM_CHAT_ID`) at CALL time -- never stored in `connector_store`,
never logged. Telegram's Bot API has no header-auth option: the token is
part of the URL path (`https://api.telegram.org/bot<token>/sendMessage`),
unlike `narrator`/`notifier`'s header-only convention. To preserve the
no-leak guarantee anyway, this module NEVER logs the URL, the token, or a raw
exception (`str(exc)`/`logging.exception`) on failure -- only a fixed,
token-free message naming the connector id.

NON-BLOCKING: `send()` itself is a plain synchronous call (so it stays
trivially unit-testable with a fake transport and zero asyncio machinery). A
caller invoking it from a running event loop (e.g. the away/rogue-device
edge callbacks) is expected to offload the blocking POST itself via
`asyncio.to_thread(send, ...)`, the same pattern `notifier.make_notifier` and
the MCP tools already use for a blocking urllib call reached from async code
-- documented, not baked in here, since the offload decision belongs to the
caller's own event-loop context (see wiring spec).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable

from wavr.connectors.http import guarded_call, post_json

CONNECTOR_ID = "telegram"
DEFAULT_TOKEN_ENV = "WAVR_TELEGRAM_TOKEN"
DEFAULT_CHAT_ID_ENV = "WAVR_TELEGRAM_CHAT_ID"
API_BASE = "https://api.telegram.org"

# Telegram hard-caps messages at 4096 UTF-16 code units; this is far more
# generous than any short summary needs and only exists as a defensive
# clamp against a caller passing an unexpectedly large `summary`.
_MAX_TEXT_LEN = 2000

# Runtime blocklist over the caller-supplied free-text `summary` -- see
# module docstring "SUMMARY IS FREE TEXT" section for why. Each pattern is
# intentionally broad (favours over-redaction of a short alert summary over
# ever letting a real MAC/coordinate/rtsp-or-frame reference slip through):
#   * MAC-like: six colon/hyphen-separated hex byte pairs.
#   * coordinate-like: two >=2-decimal-place numbers separated by a comma,
#     the shape a lat/lon pair is rendered in everywhere else in this codebase.
#   * rtsp:// URLs (a live camera stream reference).
#   * any `frame`-prefixed token (frame ids/paths/filenames -- a raw camera
#     frame reference), including the bare word "frame" itself.
_MAC_LIKE_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_COORD_LIKE_RE = re.compile(r"[-+]?\d{1,3}\.\d{2,}\s*,\s*[-+]?\d{1,3}\.\d{2,}")
_RTSP_RE = re.compile(r"rtsp://\S+", re.IGNORECASE)
_FRAME_TOKEN_RE = re.compile(r"\bframe[\w:./-]*", re.IGNORECASE)
_REDACTED = "[redacted]"


def _sanitize_summary(summary: str) -> str:
    """Redact MAC-like / coordinate-like / rtsp-or-frame tokens from a
    caller-supplied free-text summary before it can reach `build_alert_text`'s
    output. Order doesn't matter (the patterns don't overlap); never raises;
    an empty/falsy `summary` passes through unchanged."""
    if not summary:
        return summary
    text = _MAC_LIKE_RE.sub(_REDACTED, summary)
    text = _COORD_LIKE_RE.sub(_REDACTED, text)
    text = _RTSP_RE.sub(_REDACTED, text)
    text = _FRAME_TOKEN_RE.sub(_REDACTED, text)
    return text


def build_alert_text(kind: str, severity: str, room: str | None, summary: str) -> str:
    """DERIVED-ONLY allowlist for the outbound message: exactly kind, severity,
    room, and a short summary -- the same four fields `wavr.house_status`
    reasons already expose (kind/what/severity/ts + a bare room label, never
    geometry/identity/MAC/frame). Any extra data a caller might try to smuggle
    in has no parameter to travel through -- the function signature itself is
    the boundary FOR WHICH FIELDS reach this function. `summary` alone is
    still free text once it's in scope, so it additionally passes through
    `_sanitize_summary` (a runtime filter) before being rendered -- the
    allowlist bounds the field, the filter bounds what's inside it."""
    head = f"[Wavr] {(severity or 'note').upper()}: {kind or 'alert'}"
    bits = [head]
    if room:
        bits.append(f"room: {room}")
    if summary:
        bits.append(_sanitize_summary(summary))
    text = " -- ".join(bits)
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN - 1] + "…"
    return text


def make_telegram_send(store, *, connector_id: str = CONNECTOR_ID,
                        token_env: str = DEFAULT_TOKEN_ENV,
                        chat_id_env: str = DEFAULT_CHAT_ID_ENV,
                        post: Callable[..., dict] | None = None,
                        timeout: float = 10.0) -> Callable[..., dict]:
    """Factory mirroring `notifier.make_notifier` / `narrator.make_*_generate`:
    returns a `send(kind, severity="note", room=None, summary="") -> dict`
    closure closed over `store` (the broker) and the env-var NAMES to read at
    call time. `post` is injectable (mirrors `notifier.py`'s `PostFn` seam) --
    tests supply a fake that records calls and returns/raises whatever the
    test needs, so `send()` is exercised with zero real network.

    Return shapes (never raises):
      {"ok": False, "status": "disabled"}     -- connector not enabled in the broker
      {"ok": False, "status": "unconfigured"} -- enabled but token/chat_id env unset
      {"ok": False, "status": "error"}        -- enabled+configured but the POST failed
      {"ok": True,  "status": "sent", "text": <the message actually sent>}
    """
    _post = post or post_json

    def send(kind: str, severity: str = "note", room: str | None = None,
              summary: str = "") -> dict:
        # `guarded_call` is the ONE gate: is_enabled() is read fresh on every
        # call, so a kill-switch flip takes effect on the very next send() --
        # no restart, no lingering grant, and NOTHING below this line runs
        # (not even the env-var read) when the connector is off.
        def _gated() -> dict:
            token = os.getenv(token_env)
            chat_id = os.getenv(chat_id_env)
            if not token or not chat_id:
                return {"ok": False, "status": "unconfigured"}
            text = build_alert_text(kind, severity, room, summary)
            try:
                resp = _post(f"{API_BASE}/bot{token}/sendMessage",
                             {"chat_id": chat_id, "text": text}, timeout=timeout)
            except Exception:
                # Never str(exc)/logging.exception here: a urllib error can
                # surface the request URL (which contains the token) in its
                # message/traceback. Fixed, token-free text only.
                logging.warning(
                    "telegram notify failed (connector=%s); message not delivered",
                    connector_id)
                return {"ok": False, "status": "error"}
            # Telegram's Bot API answers HTTP 200 with {"ok": false, ...} for
            # several real failures (chat not found, bot blocked by the user,
            # etc.) -- a bare "the POST didn't raise" is NOT "delivered".
            if isinstance(resp, dict) and resp.get("ok") is False:
                logging.warning(
                    "telegram notify rejected by the API (connector=%s); "
                    "message not delivered", connector_id)
                return {"ok": False, "status": "error"}
            return {"ok": True, "status": "sent", "text": text}
        return guarded_call(store, connector_id, _gated)

    return send
