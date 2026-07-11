"""Open-Meteo (keyless) current-weather lookup -- house CONTEXT for
`house_status` / the assistant ("rain in 20 min -> close the windows";
DESIGN-external-connectors.md section 3.1/5 #2).

CONNECTOR ID: "open-meteo" (a `kind="generic"` row in `connector_store`).

EGRESS -- disclose precisely: the house's own COARSE latitude/longitude, and
NOTHING else (no device inventory, no occupancy, no MAC/IP). "COARSE" is
enforced in code, not just claimed: `fetch()` rounds the configured
`WAVR_HOME_LAT`/`WAVR_HOME_LON` to 2 decimal places (~1.1km of grid, see
`_COARSE_DP` below) before it ever reaches the query string -- Open-Meteo's
hourly/current forecast is uniform at that resolution, so weather accuracy is
unaffected, but the exact rooftop coordinate never leaves the box. This is
still genuine LOCATION egress -- Open-Meteo learns roughly (to ~1km) where the
house is -- so it is gated TWICE: the connector kill-switch (default OFF,
`store.is_enabled`) AND an EXPLICITLY-configured location (`WAVR_HOME_LAT`/
`WAVR_HOME_LON`, read only inside the gate). With no location set, the
connector cannot run even when enabled -- there is no default coordinate to
fall back to, since guessing one would be its own privacy leak. Keyless: no
account, no API key, no cookies.

GATE: `make_weather_fetch(store, ...)` returns a `fetch() -> dict` closure
that checks `store.is_enabled("open-meteo")` via the shared `guarded_call`
chokepoint on EVERY call -- default-OFF, revocable, no restart. When
disabled, `fetch()` returns `{"ok": False, "status": "disabled"}` and the
location env vars are never even read.

SSRF: `_BASE_URL` is a fixed, pinned official host
(`api.open-meteo.com`) -- never derived from caller/user input.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Callable

from wavr.connectors.http import get_json, guarded_call

CONNECTOR_ID = "open-meteo"
DEFAULT_LAT_ENV = "WAVR_HOME_LAT"
DEFAULT_LON_ENV = "WAVR_HOME_LON"
_BASE_URL = "https://api.open-meteo.com/v1/forecast"  # pinned official host -- never user-controlled

# Decimal places the configured lat/lon is rounded to before it ever reaches
# the query string -- 2dp is ~1.1km of grid at the equator, well inside
# Open-Meteo's own forecast-cell resolution, so the reported weather is
# unaffected while the exact coordinate never leaves the box. Delivers the
# module docstring's "COARSE latitude/longitude" claim in code, not just prose.
_COARSE_DP = 2

_CURRENT_FIELDS = "temperature_2m,precipitation,weather_code,wind_speed_10m"


def make_weather_fetch(store, *, connector_id: str = CONNECTOR_ID,
                        lat_env: str = DEFAULT_LAT_ENV, lon_env: str = DEFAULT_LON_ENV,
                        get: Callable[..., dict] | None = None,
                        timeout: float = 10.0) -> Callable[[], dict]:
    """Factory mirroring `notify/telegram.make_telegram_send`: returns a
    `fetch() -> dict` closure closed over `store` (the broker) and the
    location env-var NAMES to read at call time. `get` is injectable (mirrors
    `wavr.connectors.http.get_json`'s seam) -- tests supply a fake that
    records the call and returns/raises whatever the test needs, so `fetch()`
    is exercised with zero real network.

    Return shapes (never raises):
      {"ok": False, "status": "disabled"}     -- connector not enabled in the broker
      {"ok": False, "status": "unconfigured"} -- enabled but WAVR_HOME_LAT/LON unset/unparsable
      {"ok": False, "status": "error"}        -- enabled+configured but the GET failed
      {"ok": False, "status": "malformed"}    -- enabled+configured but the body was unusable
      {"ok": True,  "status": "fetched", "temperature_c":, "precipitation_mm":,
       "weather_code":, "wind_speed_kmh":}
    """
    _get = get or get_json

    def fetch() -> dict:
        def _gated() -> dict:
            lat_raw, lon_raw = os.getenv(lat_env), os.getenv(lon_env)
            if not lat_raw or not lon_raw:
                return {"ok": False, "status": "unconfigured"}
            try:
                lat, lon = float(lat_raw), float(lon_raw)
            except (TypeError, ValueError):
                return {"ok": False, "status": "unconfigured"}
            # Coarsen BEFORE it touches the query string -- see _COARSE_DP above.
            lat, lon = round(lat, _COARSE_DP), round(lon, _COARSE_DP)
            qs = urllib.parse.urlencode({
                "latitude": lat, "longitude": lon,
                "current": _CURRENT_FIELDS, "timezone": "auto",
            })
            url = f"{_BASE_URL}?{qs}"
            try:
                data = _get(url, timeout=timeout)
            except Exception:
                logging.warning("open-meteo fetch failed (connector=%s)", connector_id)
                return {"ok": False, "status": "error"}
            current = data.get("current") if isinstance(data, dict) else None
            if not isinstance(current, dict):
                return {"ok": False, "status": "malformed"}
            return {
                "ok": True, "status": "fetched",
                "temperature_c": current.get("temperature_2m"),
                "precipitation_mm": current.get("precipitation"),
                "weather_code": current.get("weather_code"),
                "wind_speed_kmh": current.get("wind_speed_10m"),
            }
        return guarded_call(store, connector_id, _gated)

    return fetch
