"""Read-only Home Assistant REST client (the READ half of ADR-0005).

Wavr's device story is "brain on Home Assistant": HA already speaks 2000+ devices, so
Wavr READS HA's entity list (and, in a FUTURE opt-in + consent-gated slice, triggers HA
services) instead of building its own driver hub. This module is the READ half only.

Hard constraints (match the rest of Wavr's privacy-first design):
  * READ-ONLY. The only method is `get_entities()` -> a plain list. Nothing here
    mutates HA, calls a service, actuates a device, or turns a camera/mic on. The
    control half (`call_ha_service`) is deliberately NOT here (ADR-0005: opt-in +
    consent + re-audit before it exists).
  * LOCAL-ONLY. `base_url` is the user's OWN Home Assistant on the LAN and the token is
    a locally-stored long-lived token. Nothing is sent to any cloud; the only network
    call is Wavr -> HA on the local network.
  * DEPENDENCY-FREE at runtime. The default transport is stdlib `urllib.request` — no
    httpx/requests added to Wavr's runtime deps. The `fetch` transport is INJECTABLE, so
    tests drive canned bytes with zero network and zero new dependency.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable

# A transport takes (url, headers) and returns the raw response body (bytes or str).
# Default is _urllib_get below; tests inject a fake that returns canned /api/states JSON.
FetchFn = Callable[[str, dict], object]


class WavrHAError(RuntimeError):
    """Home Assistant could not be reached or returned an unusable response.

    LOCAL-ONLY failure: base_url is the user's own HA on the LAN. Raised by
    `HAClient.get_entities()` on transport failure or malformed data. Callers that want
    to degrade quietly (e.g. the MCP tool) can catch this; an empty/absent body is NOT
    an error — it yields an empty list, not an exception.
    """


def _urllib_get(url: str, headers: dict, timeout: float = 5.0) -> bytes:
    """Default transport: a plain stdlib GET with the caller-supplied headers (which
    include `Authorization: Bearer <token>`). No third-party HTTP dependency."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local LAN URL)
        return resp.read()


class HAClient:
    """Minimal read-only Home Assistant REST client.

    Args:
        base_url: the LOCAL Home Assistant base URL, e.g. "http://homeassistant.local:8123".
        token: a long-lived access token, stored locally (never committed / never sent
            anywhere but this HA instance).
        fetch: INJECTABLE transport `(url, headers) -> body`. Defaults to a stdlib
            urllib GET. Inject a fake in tests — no network, no extra dependency.
        timeout: seconds for the default transport (ignored by an injected `fetch`).
    """

    def __init__(self, base_url: str, token: str, fetch: FetchFn | None = None,
                 timeout: float = 5.0):
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._fetch: FetchFn = fetch or (lambda url, headers: _urllib_get(url, headers, timeout))

    def _headers(self) -> dict:
        # Bearer auth to the LOCAL HA only. Content-Type kept for parity with HA's API.
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def get_entities(self) -> list[dict]:
        """`GET {base_url}/api/states` -> a list of compact entity dicts:
        `{entity_id, state, friendly_name, domain}`.

        Defensive by design:
          * Any transport failure (network down, timeout, HTTP error) -> WavrHAError.
          * An empty/absent body -> `[]` (no entities is not an error).
          * Malformed JSON, or a body that isn't a JSON list -> WavrHAError.
          * Rows without an `entity_id` are skipped; `friendly_name` falls back to the
            entity_id, `domain` is the part before the first '.'.
        """
        url = f"{self._base_url}/api/states"
        try:
            raw = self._fetch(url, self._headers())
        except WavrHAError:
            raise
        except Exception as exc:  # any transport can fail differently — normalize it
            raise WavrHAError(f"Home Assistant unreachable at {self._base_url}: {exc}") from exc

        if raw is None:
            return []
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise WavrHAError(f"Invalid JSON from Home Assistant /api/states: {exc}") from exc
        if not isinstance(data, list):
            raise WavrHAError("Unexpected /api/states response (expected a JSON list)")

        entities: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            if not entity_id or not isinstance(entity_id, str):
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            entities.append({
                "entity_id": entity_id,
                "state": item.get("state"),
                "friendly_name": attrs.get("friendly_name") or entity_id,
                "domain": entity_id.split(".", 1)[0],
            })
        return entities


def client_from_config(cfg, fetch: FetchFn | None = None) -> HAClient | None:
    """Build an HAClient from a Config, or return None when HA is not configured.

    None means the read-side is DISABLED (empty `ha_url` or `ha_token`) — the MCP tool
    degrades to an empty list rather than failing. One place owns the "configured?"
    rule so callers stay trivial.
    """
    url = getattr(cfg, "ha_url", "") or ""
    token = getattr(cfg, "ha_token", "") or ""
    if not url or not token:
        return None
    return HAClient(url, token, fetch=fetch)
