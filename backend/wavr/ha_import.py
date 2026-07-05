"""Home Assistant -> Wavr device-registry IMPORT (A4.1): the reverse of the
existing one-way Wavr->HA publish (wavr.ha_discovery).

The user's local Home Assistant already knows manufacturer / model / sw_version /
MAC for its 2000+ integrations. Importing that registry, mapping each device to
Wavr's fixed taxonomy, correlating by MAC, and feeding it into wavr.recog as the
`ha` signal (A4.0) is the single biggest LOCAL identity-accuracy win with zero new
protocol code -- it is how the 50 "via-home-assistant" catalog devices get named.

HARD INVARIANTS (this module holds all of them):
  * LOCAL-ONLY + SSRF-SAFE. The ONLY host ever contacted is the operator-configured
    `ha_url` -- the WebSocket URL is derived STRICTLY from it (`_ws_url`, http->ws /
    https->wss, any other scheme refused). NO URL from the HA response is ever
    fetched: the registry carries MACs and model strings, never a URL Wavr follows.
    So a hostile registry payload cannot steer Wavr off-box (structural SSRF guard).
  * TOKEN STAYS LOCAL. The HA long-lived token is read from config (env/`.env`) at
    import time and passed to the WS auth frame only. It is NEVER returned in a
    response, NEVER interpolated into an error/log string, NEVER persisted.
  * NEVER-RAISE PARSE. A malformed / hostile / truncated HA payload degrades to
    empty lists, never crashes the service (mirrors wavr.ha_client's defensive style).
  * SELF-DESCRIPTION CAP. The imported identity feeds recog as a `self_report`-family
    `ha` signal capped at medium-alone (wavr.recog A4.0) -- a spoofed HA model can
    never forge a high-confidence verdict on its own.

The WS transport is INJECTABLE (`ws_fn`), same seam as wavr.ha_client's fetch/post:
tests drive canned registry JSON with zero network and zero new required dependency.
The production default lazily imports `websockets` (already present transitively via
starlette) only when an import is actually triggered -- no socket is opened otherwise.
"""
from __future__ import annotations

import asyncio
import json
import logging

from wavr.data.deviceclass import hostname_type
from wavr.device_meta import normalize_mac
from wavr.ha_client import WavrHAError

_LOG = logging.getLogger(__name__)

_MAX_FIELD_LEN = 200

# HA entity-domain -> Wavr taxonomy (wavr.data.deviceclass.DEVICE_TYPES). Only used
# when a make/model/name pattern (hostname_type) does NOT already resolve a more
# specific type. License-safe: HA domains are an open, public interface; the mapping
# wording is Wavr's own. `camera` is handled first (unambiguous) in _resolve_type.
_DOMAIN_TYPE: dict[str, str] = {
    "camera": "camera",
    "vacuum": "iot_sensor",
    "climate": "iot_sensor",
    "sensor": "iot_sensor",
    "binary_sensor": "iot_sensor",
    "lock": "iot_sensor",
    "cover": "iot_sensor",
    "light": "smart_plug",
    "switch": "smart_plug",
    "fan": "smart_plug",
}


def _clean(value) -> str | None:
    """A trimmed, length-bounded string, or None. Never raises on odd input."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v[:_MAX_FIELD_LEN] if v else None


def _extract_mac(connections) -> str | None:
    """Pull the first ('mac', '<addr>') pair from HA's device `connections`
    list and normalize it. Bluetooth / zigbee / other connection types and any
    non-6-octet value are ignored (return None) -- recog is keyed by LAN MAC."""
    if not isinstance(connections, (list, tuple)):
        return None
    for conn in connections:
        if (isinstance(conn, (list, tuple)) and len(conn) == 2
                and conn[0] == "mac" and isinstance(conn[1], str)):
            try:
                return normalize_mac(conn[1])
            except ValueError:
                continue
    return None


def _resolve_type(domains: set[str], make, model, name) -> str:
    """Map an HA device to a Wavr taxonomy value. A specific make/model/name
    pattern wins (reuses wavr.data.deviceclass.hostname_type -- no parallel
    classifier), then the unambiguous camera domain, then the domain table,
    else `unknown` (never overclaim)."""
    for text in (model, name, make):
        t = hostname_type(text)
        if t:
            return t
    if "camera" in domains:
        return "camera"
    for dom in domains:
        if dom in _DOMAIN_TYPE:
            return _DOMAIN_TYPE[dom]
    return "unknown"


def _match_catalog(make, model, catalog) -> dict | None:
    """Advisory catalog enrichment: match make+model against the `via-home-
    assistant` entries of Wavr's static device catalog. Conservative -- requires
    a brand-token match AND a model-token overlap, so it never guesses. Returns
    {id, name} or None. Authoritative identity stays the taxonomy device_type;
    this is UI enrichment only (drives the rung-2 card)."""
    if not make or not model or not isinstance(catalog, list):
        return None
    mk = make.lower()
    md_tokens = {t for t in model.lower().replace("/", " ").split() if len(t) > 2}
    if not md_tokens:
        return None
    for e in catalog:
        if not isinstance(e, dict) or e.get("status") != "via-home-assistant":
            continue
        brand = (e.get("brand") or "").lower()
        brand_parts = brand.split()
        if not brand_parts:
            continue
        brand_first = brand_parts[0]
        if brand_first not in mk and (mk.split()[0] if mk.split() else mk) not in brand:
            continue
        name = (e.get("name") or "").lower()
        if any(tok in name for tok in md_tokens):
            return {"id": e.get("id"), "name": e.get("name")}
    return None


def map_device(dev: dict, entities: list, catalog) -> dict:
    """Map ONE HA device-registry entry (+ its entity rows) to a Wavr view:
    {mac?, make, model, os, device_type, area, entity_count, catalog_match?}.
    Pure/offline, never raises on malformed input."""
    make = _clean(dev.get("manufacturer"))
    model = _clean(dev.get("model"))
    name = _clean(dev.get("name_by_user")) or _clean(dev.get("name"))
    os_name = _clean(dev.get("sw_version"))
    area = _clean(dev.get("area_id"))
    domains: set[str] = set()
    for e in entities:
        eid = e.get("entity_id") if isinstance(e, dict) else None
        if isinstance(eid, str) and "." in eid:
            domains.add(eid.split(".", 1)[0])
    view = {
        "mac": _extract_mac(dev.get("connections")),
        "make": make,
        "model": model,
        "os": os_name,
        "device_type": _resolve_type(domains, make, model, name),
        "area": area,
        "entity_count": len(entities),
    }
    cat = _match_catalog(make, model, catalog)
    if cat:
        view["catalog_match"] = cat
    return view


def import_devices(registry: dict, catalog, store=None, dry_run: bool = False) -> dict:
    """Map every HA device in `registry` (`{devices:[...], entities:[...]}`) to a
    Wavr view, persist the MAC-bearing ones to `store` (unless `dry_run`), and
    return a summary. Never raises on malformed rows -- they land in `skipped`.

    Counts (all additive, no secrets): `imported` = MAC-bearing devices persisted
    (fed to recog); `matched_to_lan` = same (a MAC is what correlates to the LAN
    inventory); `matched_to_catalog` = of those, how many hit a catalog entry;
    `unmatched` = devices with no MAC (can't feed per-MAC recog). `devices` lists
    the MAC-bearing views; `skipped` lists the rest with a reason (never a token)."""
    devices = registry.get("devices") if isinstance(registry, dict) else None
    entities = registry.get("entities") if isinstance(registry, dict) else None
    devices = devices if isinstance(devices, list) else []
    entities = entities if isinstance(entities, list) else []

    ent_by_dev: dict[str, list] = {}
    for e in entities:
        if isinstance(e, dict) and isinstance(e.get("device_id"), str):
            ent_by_dev.setdefault(e["device_id"], []).append(e)

    out_devices: list[dict] = []
    skipped: list[dict] = []
    matched_to_catalog = 0

    for dev in devices:
        if not isinstance(dev, dict):
            skipped.append({"reason": "malformed registry entry", "name": None})
            continue
        view = map_device(dev, ent_by_dev.get(dev.get("id"), []), catalog)
        label = view.get("model") or view.get("make") or view.get("area")
        if view.get("mac"):
            if view.get("catalog_match"):
                matched_to_catalog += 1
            if not dry_run and store is not None:
                # Store an UNRESOLVED type as NULL, never the literal "unknown":
                # recog weights `ha` at 0.82, so persisting "unknown" would feed a
                # strong "unknown" opinion that could mask a better hostname/port
                # verdict. make/model/os are still stored (they always enrich).
                dtype = view.get("device_type")
                if dtype == "unknown":
                    dtype = None
                try:
                    store.upsert(view["mac"], dtype,
                                 view.get("make"), view.get("model"), view.get("os"))
                except ValueError:
                    # A MAC that passed _extract_mac but the store rejects: skip the
                    # persist, still report the view (never abort the whole import).
                    _LOG.warning("ha_import: store rejected a MAC; skipping persist")
            out_devices.append(view)
        else:
            skipped.append({"reason": "no MAC in HA registry", "name": label})

    return {
        "imported": len(out_devices),
        "matched_to_lan": len(out_devices),
        "matched_to_catalog": matched_to_catalog,
        "unmatched": sum(1 for s in skipped if s["reason"] == "no MAC in HA registry"),
        "dry_run": dry_run,
        "devices": out_devices,
        "skipped": skipped,
    }


def _ws_url(base_url: str) -> str:
    """Derive the HA WebSocket URL STRICTLY from the configured base URL --
    http->ws, https->wss, any other scheme refused. This is the SSRF guard: the
    host is ALWAYS exactly the operator-configured HA host; no URL from a
    response is ever used. Never contains the token."""
    u = (base_url or "").rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):] + "/api/websocket"
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):] + "/api/websocket"
    raise WavrHAError("Home Assistant URL must start with http:// or https://")


async def _ws_list(ws, msg_id: int, command: str) -> list:
    """Send one HA WS registry-list command and return its `result` list. Reads
    until the matching `id` result frame; a non-success or non-list result
    degrades to `[]` (never-raise)."""
    await ws.send(json.dumps({"id": msg_id, "type": command}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id and msg.get("type") == "result":
            result = msg.get("result")
            return result if isinstance(result, list) else []


async def _default_ws_registry(ws_url: str, token: str, timeout: float) -> dict:
    """Production WS transport: lazily import `websockets` (present transitively
    via starlette; not a NEW required dep), auth with the local token, and read
    the device + entity registries. LOCAL-ONLY (ws_url is `_ws_url`-derived from
    the configured HA host); no redirect following (WS has none). The token is
    used ONLY in the auth frame and never appears in any raised message.

    NOTE: exercised only against a real local HA -- CI injects `ws_fn` with
    canned bytes, so this path opens no socket in tests (same lazy pattern as
    the mdns/ssdp collectors)."""
    try:
        import websockets  # lazy-optional, resolved only on a real import call
    except ImportError as exc:  # pragma: no cover - depends on runtime env
        raise WavrHAError(
            "Home Assistant registry import needs the 'websockets' package") from exc

    async def _run() -> dict:
        async with websockets.connect(ws_url, open_timeout=timeout,
                                       close_timeout=timeout) as ws:
            first = json.loads(await ws.recv())            # auth_required
            if first.get("type") != "auth_required":
                raise WavrHAError("Unexpected Home Assistant WebSocket greeting")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                # NEVER echo the token -- name only the failure.
                raise WavrHAError("Home Assistant rejected the access token")
            devices = await _ws_list(ws, 1, "config/device_registry/list")
            entities = await _ws_list(ws, 2, "config/entity_registry/list")
            return {"devices": devices, "entities": entities}

    return await asyncio.wait_for(_run(), timeout=timeout * 4)


async def fetch_registry(base_url: str, token: str, ws_fn=None,
                         timeout: float = 5.0) -> dict:
    """Fetch HA's device + entity registry from the LOCAL, configured HA only.
    `ws_fn(ws_url, token, timeout)` is the injectable async transport (tests pass
    a canned one). Returns `{devices:[...], entities:[...]}`, both always lists.
    Any transport/parse failure -> WavrHAError whose message NEVER contains the
    token; a malformed shape -> empty lists (never-raise)."""
    ws_url = _ws_url(base_url)   # SSRF guard: host is the configured HA, always
    fn = ws_fn or _default_ws_registry
    try:
        raw = await fn(ws_url, token, timeout)
    except WavrHAError:
        raise
    except Exception as exc:  # normalize; must not leak the token in the message
        raise WavrHAError(f"Home Assistant registry unreachable at {ws_url}") from exc
    if not isinstance(raw, dict):
        return {"devices": [], "entities": []}
    devices = raw.get("devices")
    entities = raw.get("entities")
    return {
        "devices": devices if isinstance(devices, list) else [],
        "entities": entities if isinstance(entities, list) else [],
    }
