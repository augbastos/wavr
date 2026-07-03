"""Home Assistant REST client (the READ + low-level CONTROL primitive of ADR-0005).

Wavr's device story is "brain on Home Assistant": HA already speaks 2000+ devices, so
Wavr READS HA's entity list and — in the opt-in, allowlist+consent-gated control slice —
asks HA to run a service, instead of building its own driver hub.

Hard constraints (match the rest of Wavr's privacy-first design):
  * MINIMAL SURFACE. `get_entities()` reads; `call_service()` is the low-level POST that
    actuation is delegated to. NEITHER method carries any POLICY: the allowlist, the
    sensitive-domain refusal, and the consent + control-flag gates all live one layer up
    at the MCP tool (`wavr.mcp.call_ha_service`). This module is a transport, not a guard
    — never call `call_service()` without having passed those gates first.
  * CONTROL = DELEGATION. `call_service()` never drives a device directly; it asks the
    user's own HA to execute (ADR-0005 §1). Wavr stays the fusion/explainability brain.
  * LOCAL-ONLY. `base_url` is the user's OWN Home Assistant on the LAN and the token is
    a locally-stored long-lived token. Nothing is sent to any cloud; the only network
    call is Wavr -> HA on the local network.
  * DEPENDENCY-FREE at runtime. The default transports are stdlib `urllib.request` — no
    httpx/requests added to Wavr's runtime deps. Both the `fetch` (GET) and `post`
    transports are INJECTABLE, so tests drive canned bytes with zero network and zero new
    dependency.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable

# A GET transport takes (url, headers) and returns the raw response body (bytes or str).
# Default is _urllib_get below; tests inject a fake that returns canned /api/states JSON.
FetchFn = Callable[[str, dict], object]

# A POST transport takes (url, headers, body_bytes) and returns the raw response body.
# Default is _urllib_post below; tests inject a fake that records the call + returns bytes.
PostFn = Callable[[str, dict, bytes], object]


class WavrHAError(RuntimeError):
    """Home Assistant could not be reached or returned an unusable response.

    LOCAL-ONLY failure: base_url is the user's own HA on the LAN. Raised by
    `HAClient.get_entities()` on transport failure or malformed data. Callers that want
    to degrade quietly (e.g. the MCP tool) can catch this; an empty/absent body is NOT
    an error — it yields an empty list, not an exception.
    """


def _urllib_get(url: str, headers: dict, timeout: float = 5.0) -> bytes:
    """Default GET transport: a plain stdlib GET with the caller-supplied headers (which
    include `Authorization: Bearer <token>`). No third-party HTTP dependency."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local LAN URL)
        return resp.read()


def _urllib_post(url: str, headers: dict, body: bytes, timeout: float = 5.0) -> bytes:
    """Default POST transport: a plain stdlib POST of `body` with the caller-supplied
    headers (Bearer token + JSON content-type). No third-party HTTP dependency."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local LAN URL)
        return resp.read()


class HAClient:
    """Minimal read-only Home Assistant REST client.

    Args:
        base_url: the LOCAL Home Assistant base URL, e.g. "http://homeassistant.local:8123".
        token: a long-lived access token, stored locally (never committed / never sent
            anywhere but this HA instance).
        fetch: INJECTABLE GET transport `(url, headers) -> body`. Defaults to a stdlib
            urllib GET. Inject a fake in tests — no network, no extra dependency.
        post: INJECTABLE POST transport `(url, headers, body) -> body`. Defaults to a
            stdlib urllib POST. Inject a fake in tests — no network, no extra dependency.
        timeout: seconds for the default transports (ignored by an injected transport).
    """

    def __init__(self, base_url: str, token: str, fetch: FetchFn | None = None,
                 post: PostFn | None = None, timeout: float = 5.0):
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._fetch: FetchFn = fetch or (lambda url, headers: _urllib_get(url, headers, timeout))
        self._post: PostFn = post or (
            lambda url, headers, body: _urllib_post(url, headers, body, timeout))

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

    def call_service(self, domain: str, service: str, data: dict | None = None) -> object:
        """`POST {base_url}/api/services/{domain}/{service}` with the Bearer token and a
        JSON `data` body (e.g. `{"entity_id": "light.kitchen"}`). Returns HA's response
        (the list of states HA changed) or raises `WavrHAError` on failure.

        LOW-LEVEL PRIMITIVE ONLY — carries NO policy. It performs whatever service call it
        is handed; it does NOT consult an allowlist, check for sensitive domains, or
        verify consent/the control flag. Those gates live at the MCP tool layer
        (`wavr.mcp.call_ha_service`), which is the ONLY thing that should call this. This
        is the delegation seam of ADR-0005 §1: Wavr asks HA to act; HA executes.

        Defensive by design (mirrors `get_entities`):
          * Any transport failure (network down, timeout, HTTP error) -> WavrHAError.
          * An empty/absent body (HA changed nothing) -> `[]` (not an error).
          * Malformed JSON -> WavrHAError.
        """
        url = f"{self._base_url}/api/services/{domain}/{service}"
        payload = json.dumps(data or {}).encode()
        try:
            raw = self._post(url, self._headers(), payload)
        except WavrHAError:
            raise
        except Exception as exc:  # any transport can fail differently — normalize it
            raise WavrHAError(f"Home Assistant service call failed at {url}: {exc}") from exc

        if raw is None:
            return []
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not text.strip():
            return []
        try:
            return json.loads(text)
        except (ValueError, TypeError) as exc:
            raise WavrHAError(f"Invalid JSON from Home Assistant service call: {exc}") from exc


def client_from_config(cfg, fetch: FetchFn | None = None,
                       post: PostFn | None = None) -> HAClient | None:
    """Build an HAClient from a Config, or return None when HA is not configured.

    None means the read-side is DISABLED (empty `ha_url` or `ha_token`) — the MCP tool
    degrades to an empty list rather than failing. One place owns the "configured?"
    rule so callers stay trivial.
    """
    url = getattr(cfg, "ha_url", "") or ""
    token = getattr(cfg, "ha_token", "") or ""
    if not url or not token:
        return None
    return HAClient(url, token, fetch=fetch, post=post)
