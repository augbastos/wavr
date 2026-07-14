"""ENRICH-surface connectors (backend/wavr/connectors/enrich/): Open-Meteo,
URLhaus, AbuseIPDB, Wikipedia (DESIGN-external-connectors.md sections 3.2/5).

Proves the shared guardrails every connector in this surface must hold:
  * DEFAULT-OFF -- a disabled connector never calls the injected HTTP client,
    not even to read a location/key env var.
  * MINIMAL EGRESS -- when enabled, exactly the documented payload (a
    lat/lon pair, one url/host/hash, one IP, one query string) reaches the
    fake client -- never house/device state, never a secret in the URL.
  * CLEAN DEGRADE -- a malformed upstream body or a transport error never
    raises into the caller; it returns a `{"ok": False, "status": ...}` shape.

Everything runs OFFLINE: every fetch/lookup takes an injectable HTTP client
(`get=`/`post=`), so this file makes ZERO real network calls.
"""
from __future__ import annotations

import inspect
import urllib.error

from wavr.connector_store import ConnectorStore
from wavr.connectors.enrich import abuseipdb, open_meteo, urlhaus, wikipedia


def _enabled_store(connector_id: str) -> ConnectorStore:
    store = ConnectorStore(":memory:")
    store.upsert(connector_id, "generic", connector_id)
    store.set_enabled(connector_id, True)
    return store


class _Spy:
    """Fake HTTP client: records every call (args, kwargs) and either returns
    a canned result or raises a canned exception -- no real socket, ever."""

    def __init__(self, result=None, exc: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self._result = result
        self._exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result


# --------------------------------------------------------------------------- #
# Open-Meteo (keyless, location-gated)
# --------------------------------------------------------------------------- #
def test_open_meteo_disabled_makes_no_call(monkeypatch):
    monkeypatch.setenv("WAVR_HOME_LAT", "52.6")
    monkeypatch.setenv("WAVR_HOME_LON", "-8.6")
    store = ConnectorStore(":memory:")  # never enabled -- absent row => default-off
    spy = _Spy(result={"current": {"temperature_2m": 15}})
    fetch = open_meteo.make_weather_fetch(store, get=spy)
    assert fetch() == {"ok": False, "status": "disabled"}
    assert spy.calls == []


def test_open_meteo_enabled_but_no_location_configured_never_calls(monkeypatch):
    monkeypatch.delenv("WAVR_HOME_LAT", raising=False)
    monkeypatch.delenv("WAVR_HOME_LON", raising=False)
    store = _enabled_store("open-meteo")
    spy = _Spy(result={"current": {}})
    fetch = open_meteo.make_weather_fetch(store, get=spy)
    assert fetch() == {"ok": False, "status": "unconfigured"}
    assert spy.calls == []  # kill-switch on AND no location -- still zero network


def test_open_meteo_enabled_sends_minimal_egress(monkeypatch):
    monkeypatch.setenv("WAVR_HOME_LAT", "52.66")
    monkeypatch.setenv("WAVR_HOME_LON", "-8.63")
    store = _enabled_store("open-meteo")
    spy = _Spy(result={"current": {"temperature_2m": 14.2, "precipitation": 0.0,
                                    "weather_code": 3, "wind_speed_10m": 12.1}})
    fetch = open_meteo.make_weather_fetch(store, get=spy)
    result = fetch()
    assert result == {"ok": True, "status": "fetched", "temperature_c": 14.2,
                       "precipitation_mm": 0.0, "weather_code": 3, "wind_speed_kmh": 12.1}
    assert len(spy.calls) == 1
    (url,), _kwargs = spy.calls[0]
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")  # pinned host
    assert "latitude=52.66" in url and "longitude=-8.63" in url
    assert "mac" not in url.lower() and "device" not in url.lower()   # nothing else leaks


def test_open_meteo_malformed_response_degrades_cleanly(monkeypatch):
    monkeypatch.setenv("WAVR_HOME_LAT", "1")
    monkeypatch.setenv("WAVR_HOME_LON", "1")
    store = _enabled_store("open-meteo")
    fetch = open_meteo.make_weather_fetch(store, get=_Spy(result={"unexpected": "shape"}))
    assert fetch() == {"ok": False, "status": "malformed"}


def test_open_meteo_transport_error_degrades_cleanly(monkeypatch):
    monkeypatch.setenv("WAVR_HOME_LAT", "1")
    monkeypatch.setenv("WAVR_HOME_LON", "1")
    store = _enabled_store("open-meteo")
    fetch = open_meteo.make_weather_fetch(store, get=_Spy(exc=OSError("network unreachable")))
    assert fetch() == {"ok": False, "status": "error"}


def test_open_meteo_coarsens_lat_lon_before_egress(monkeypatch):
    # Delivers the module docstring's "COARSE latitude/longitude" claim in
    # code: a full-precision configured coordinate must never reach the
    # query string verbatim -- only the 2dp-rounded value.
    monkeypatch.setenv("WAVR_HOME_LAT", "52.659876")
    monkeypatch.setenv("WAVR_HOME_LON", "-8.611234")
    store = _enabled_store("open-meteo")
    spy = _Spy(result={"current": {"temperature_2m": 14.2, "precipitation": 0.0,
                                    "weather_code": 3, "wind_speed_10m": 12.1}})
    fetch = open_meteo.make_weather_fetch(store, get=spy)
    assert fetch()["ok"] is True
    (url,), _kwargs = spy.calls[0]
    assert "latitude=52.66" in url and "longitude=-8.61" in url
    assert "52.659876" not in url and "8.611234" not in url  # full precision never leaves


def test_open_meteo_kill_switch_takes_effect_without_rebuilding(monkeypatch):
    monkeypatch.setenv("WAVR_HOME_LAT", "1")
    monkeypatch.setenv("WAVR_HOME_LON", "1")
    store = _enabled_store("open-meteo")
    spy = _Spy(result={"current": {"temperature_2m": 10}})
    fetch = open_meteo.make_weather_fetch(store, get=spy)
    assert fetch()["ok"] is True
    store.set_enabled("open-meteo", False)          # revoked mid-lifetime, no restart
    assert fetch() == {"ok": False, "status": "disabled"}
    assert len(spy.calls) == 1                       # the second call never reached the transport


# --------------------------------------------------------------------------- #
# URLhaus (keyless, url/host/hash)
# --------------------------------------------------------------------------- #
def test_urlhaus_disabled_makes_no_call():
    store = ConnectorStore(":memory:")
    spy = _Spy(result={"query_status": "ok"})
    lookup = urlhaus.make_urlhaus_lookup(store, post=spy)
    assert lookup(url="http://evil.example/payload") == {"ok": False, "status": "disabled"}
    assert spy.calls == []


def test_urlhaus_bad_request_on_zero_or_multiple_values():
    store = _enabled_store("urlhaus")
    spy = _Spy(result={})
    lookup = urlhaus.make_urlhaus_lookup(store, post=spy)
    assert lookup() == {"ok": False, "status": "bad_request"}
    assert lookup(url="http://x", host="x.example") == {"ok": False, "status": "bad_request"}
    assert spy.calls == []


def test_urlhaus_enabled_sends_minimal_egress_url_lookup():
    store = _enabled_store("urlhaus")
    spy = _Spy(result={"query_status": "ok", "threat": "malware_download",
                        "url_status": "online", "tags": ["exe", "elf"]})
    lookup = urlhaus.make_urlhaus_lookup(store, post=spy)
    result = lookup(url="http://evil.example/payload")
    assert result == {"ok": True, "status": "fetched", "query_status": "ok",
                       "malicious": True, "threat": "malware_download",
                       "listing_status": "online", "tags": ["exe", "elf"]}
    assert len(spy.calls) == 1
    (endpoint, fields), _kwargs = spy.calls[0]
    assert endpoint == "https://urlhaus-api.abuse.ch/v1/url/"          # pinned host
    assert fields == {"url": "http://evil.example/payload"}            # ONLY the queried value


def test_urlhaus_host_lookup_no_match_is_not_malicious():
    store = _enabled_store("urlhaus")
    lookup = urlhaus.make_urlhaus_lookup(store, post=_Spy(result={"query_status": "no_results"}))
    result = lookup(host="benign.example.com")
    assert result["ok"] is True
    assert result["malicious"] is False


def test_urlhaus_malformed_response_degrades_cleanly():
    store = _enabled_store("urlhaus")
    lookup = urlhaus.make_urlhaus_lookup(store, post=_Spy(result={}))
    assert lookup(url="http://x") == {"ok": False, "status": "malformed"}


def test_urlhaus_transport_error_degrades_cleanly():
    store = _enabled_store("urlhaus")
    lookup = urlhaus.make_urlhaus_lookup(store, post=_Spy(exc=TimeoutError("timed out")))
    assert lookup(url="http://x") == {"ok": False, "status": "error"}


# --------------------------------------------------------------------------- #
# AbuseIPDB (keyed by ENV NAME, rate-limit-aware)
# --------------------------------------------------------------------------- #
def test_abuseipdb_disabled_makes_no_call(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "secret-key-value")
    store = ConnectorStore(":memory:")
    spy = _Spy(result={"data": {"abuseConfidenceScore": 10}})
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=spy)
    assert lookup("8.8.8.8") == {"ok": False, "status": "disabled"}
    assert spy.calls == []


def test_abuseipdb_enabled_but_no_key_never_calls(monkeypatch):
    monkeypatch.delenv("WAVR_ABUSEIPDB_KEY", raising=False)
    store = _enabled_store("abuseipdb")
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=_Spy(result={}))
    assert lookup("8.8.8.8") == {"ok": False, "status": "unconfigured"}


def test_abuseipdb_bad_ip_never_calls(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "secret-key-value")
    store = _enabled_store("abuseipdb")
    spy = _Spy(result={})
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=spy)
    assert lookup("not-an-ip") == {"ok": False, "status": "bad_request"}
    assert spy.calls == []


def test_abuseipdb_enabled_sends_minimal_egress_key_in_header_only(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "secret-key-value")
    store = _enabled_store("abuseipdb")
    spy = _Spy(result={"data": {"ipAddress": "203.0.113.7", "abuseConfidenceScore": 42,
                                 "totalReports": 3, "countryCode": "US",
                                 "isWhitelisted": False}})
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=spy)
    result = lookup("203.0.113.7")
    assert result == {"ok": True, "status": "fetched", "ip": "203.0.113.7",
                       "abuse_score": 42, "total_reports": 3, "country_code": "US",
                       "is_whitelisted": False}
    assert len(spy.calls) == 1
    (url,), kwargs = spy.calls[0]
    assert "secret-key-value" not in url                    # key NEVER in the URL/query
    assert kwargs["headers"]["Key"] == "secret-key-value"    # key ONLY in the header
    assert "ipAddress=203.0.113.7" in url                    # exactly the queried IP leaves


def test_abuseipdb_rate_limited_degrades_cleanly(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "k")
    store = _enabled_store("abuseipdb")
    exc = urllib.error.HTTPError("https://api.abuseipdb.com/api/v2/check", 429,
                                  "Too Many Requests", {}, None)
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=_Spy(exc=exc))
    assert lookup("8.8.8.8") == {"ok": False, "status": "rate_limited"}


def test_abuseipdb_malformed_response_degrades_cleanly(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "k")
    store = _enabled_store("abuseipdb")
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=_Spy(result={"unexpected": True}))
    assert lookup("8.8.8.8") == {"ok": False, "status": "malformed"}


def test_abuseipdb_transport_error_degrades_cleanly(monkeypatch):
    monkeypatch.setenv("WAVR_ABUSEIPDB_KEY", "k")
    store = _enabled_store("abuseipdb")
    lookup = abuseipdb.make_abuseipdb_lookup(store, get=_Spy(exc=OSError("unreachable")))
    assert lookup("8.8.8.8") == {"ok": False, "status": "error"}


# --------------------------------------------------------------------------- #
# Wikipedia (keyless, query-only)
# --------------------------------------------------------------------------- #
def test_wikipedia_disabled_makes_no_call():
    store = ConnectorStore(":memory:")
    spy = _Spy(result={"query": {"pages": {}}})
    lookup = wikipedia.make_wikipedia_lookup(store, get=spy)
    assert lookup("heat pump") == {"ok": False, "status": "disabled"}
    assert spy.calls == []


def test_wikipedia_blank_query_never_calls():
    store = _enabled_store("wikipedia")
    spy = _Spy(result={})
    lookup = wikipedia.make_wikipedia_lookup(store, get=spy)
    assert lookup("   ") == {"ok": False, "status": "bad_request"}
    assert spy.calls == []


def test_wikipedia_enabled_sends_minimal_egress():
    store = _enabled_store("wikipedia")
    spy = _Spy(result={"query": {"pages": {"123": {
        "title": "Heat pump", "extract": "A heat pump is a device..."}}}})
    lookup = wikipedia.make_wikipedia_lookup(store, get=spy)
    result = lookup("heat pump")
    assert result == {"ok": True, "status": "fetched", "found": True,
                       "title": "Heat pump", "extract": "A heat pump is a device..."}
    assert len(spy.calls) == 1
    (url,), _kwargs = spy.calls[0]
    assert url.startswith("https://en.wikipedia.org/w/api.php?")   # pinned host
    assert "gsrsearch=heat" in url                                  # only the query text leaves


def test_wikipedia_no_match_reports_found_false():
    store = _enabled_store("wikipedia")
    lookup = wikipedia.make_wikipedia_lookup(store, get=_Spy(result={"query": {"pages": {}}}))
    assert lookup("asdkfjasldkfj nonsense query") == {"ok": True, "status": "fetched", "found": False}


def test_wikipedia_malformed_response_degrades_cleanly():
    store = _enabled_store("wikipedia")
    lookup = wikipedia.make_wikipedia_lookup(store, get=_Spy(result={}))
    assert lookup("heat pump") == {"ok": False, "status": "malformed"}


def test_wikipedia_transport_error_degrades_cleanly():
    store = _enabled_store("wikipedia")
    lookup = wikipedia.make_wikipedia_lookup(store, get=_Spy(exc=OSError("dns failure")))
    assert lookup("heat pump") == {"ok": False, "status": "error"}


def test_wikipedia_lookup_signature_carries_only_query():
    # Boundary discipline (mirrors narrator.build_prompt's allowlist-by-
    # signature): the closure accepts nothing but the query text, so a
    # caller has no parameter through which to smuggle house/device state.
    lookup = wikipedia.make_wikipedia_lookup(ConnectorStore(":memory:"))
    assert list(inspect.signature(lookup).parameters) == ["query"]
