"""Tests for the READ half of the "brain on Home Assistant" (ADR-0005):

  * the read-only `wavr.ha_client.HAClient` (injected fake transport, zero network),
  * `client_from_config` (disabled when HA is unconfigured),
  * the `get_ha_entities` MCP tool logic (mocked client + clean degrade when unset),
  * that `import wavr.ha_client` / `import wavr.mcp` need nothing reachable.

Everything runs offline: the transport is injected, so no LAN / HA / cloud is touched.
"""

import importlib
import json

import pytest

from wavr.ha_client import HAClient, WavrHAError, client_from_config
from wavr.mcp import get_ha_entities

# A realistic slice of Home Assistant's GET /api/states response.
CANNED_STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light", "brightness": 254}},
    {"entity_id": "sensor.living_temp", "state": "21.5",
     "attributes": {"friendly_name": "Living Room Temp", "unit_of_measurement": "C"}},
    {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {}},
    {"no_entity_id": "malformed row is skipped"},
    "not even a dict — skipped too",
]


def _fake_fetch(body, spy=None):
    """Build an injectable transport returning `body` (str/bytes). If `spy` is a dict,
    the url + headers of the call are recorded so tests can assert the Bearer header."""
    def fetch(url, headers):
        if spy is not None:
            spy["url"] = url
            spy["headers"] = headers
        return body
    return fetch


# --- imports need nothing reachable ------------------------------------------------

def test_imports_succeed_with_no_ha_reachable():
    assert importlib.import_module("wavr.ha_client")
    m = importlib.import_module("wavr.mcp")
    assert hasattr(m, "get_ha_entities")


# --- HAClient.get_entities parsing -------------------------------------------------

def test_get_entities_parses_id_state_name_domain():
    spy = {}
    client = HAClient("http://ha.local:8123/", "tok-123",
                      fetch=_fake_fetch(json.dumps(CANNED_STATES), spy=spy))
    out = client.get_entities()

    assert out == [
        {"entity_id": "light.kitchen", "state": "on",
         "friendly_name": "Kitchen Light", "domain": "light"},
        {"entity_id": "sensor.living_temp", "state": "21.5",
         "friendly_name": "Living Room Temp", "domain": "sensor"},
        # no friendly_name in attributes -> falls back to entity_id
        {"entity_id": "binary_sensor.front_door", "state": "off",
         "friendly_name": "binary_sensor.front_door", "domain": "binary_sensor"},
    ]
    # transport was called with the joined URL (trailing slash trimmed) + Bearer token
    assert spy["url"] == "http://ha.local:8123/api/states"
    assert spy["headers"]["Authorization"] == "Bearer tok-123"


def test_get_entities_accepts_bytes_body():
    client = HAClient("http://ha.local:8123", "t",
                      fetch=_fake_fetch(json.dumps(CANNED_STATES).encode()))
    assert [e["entity_id"] for e in client.get_entities()] == [
        "light.kitchen", "sensor.living_temp", "binary_sensor.front_door"]


# --- empty / error transport handled -----------------------------------------------

def test_empty_body_yields_empty_list():
    assert HAClient("http://ha", "t", fetch=_fake_fetch("")).get_entities() == []
    assert HAClient("http://ha", "t", fetch=_fake_fetch(b"")).get_entities() == []
    assert HAClient("http://ha", "t", fetch=_fake_fetch(None)).get_entities() == []


def test_empty_json_list_yields_empty_list():
    assert HAClient("http://ha", "t", fetch=_fake_fetch("[]")).get_entities() == []


def test_transport_error_raises_wavr_ha_error():
    def boom(url, headers):
        raise OSError("connection refused")
    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", fetch=boom).get_entities()


def test_malformed_json_raises_wavr_ha_error():
    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", fetch=_fake_fetch("{not json")).get_entities()


def test_non_list_json_raises_wavr_ha_error():
    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", fetch=_fake_fetch('{"result": "ok"}')).get_entities()


# --- client_from_config: disabled unless both url + token set ----------------------

class _Cfg:
    def __init__(self, ha_url="", ha_token=""):
        self.ha_url = ha_url
        self.ha_token = ha_token


def test_client_from_config_none_when_unconfigured():
    assert client_from_config(_Cfg()) is None
    assert client_from_config(_Cfg(ha_url="http://ha")) is None      # token missing
    assert client_from_config(_Cfg(ha_token="tok")) is None          # url missing


def test_client_from_config_builds_client_when_configured():
    client = client_from_config(_Cfg("http://ha.local:8123", "tok"),
                                fetch=_fake_fetch(json.dumps(CANNED_STATES)))
    assert isinstance(client, HAClient)
    assert [e["entity_id"] for e in client.get_entities()][0] == "light.kitchen"


# --- get_ha_entities MCP tool logic ------------------------------------------------

class _FakeClient:
    def __init__(self, entities):
        self._entities = entities

    def get_entities(self):
        return self._entities


def test_tool_returns_parsed_list_with_mocked_client():
    parsed = [{"entity_id": "light.kitchen", "state": "on",
               "friendly_name": "Kitchen Light", "domain": "light"}]
    assert get_ha_entities(_FakeClient(parsed)) == parsed


def test_tool_degrades_cleanly_when_unconfigured():
    # None client == HA not configured -> empty list, never an exception.
    assert get_ha_entities(None) == []


def test_tool_handles_client_returning_none():
    assert get_ha_entities(_FakeClient(None)) == []


def test_tool_end_to_end_with_real_client_and_injected_fetch():
    client = HAClient("http://ha.local:8123", "tok",
                      fetch=_fake_fetch(json.dumps(CANNED_STATES)))
    out = get_ha_entities(client)
    assert {e["domain"] for e in out} == {"light", "sensor", "binary_sensor"}
