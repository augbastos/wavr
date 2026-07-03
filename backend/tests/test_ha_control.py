"""Tests for the CONTROL/WRITE half of the "brain on Home Assistant" (ADR-0005):

  * the low-level `HAClient.call_service` primitive (injected POST transport, zero
    network) — right URL / Bearer header / JSON payload, plus defensive parsing,
  * the gated `call_ha_service` MCP tool logic and its gate chain
    (control-flag -> allowlist -> sensitive-domain refusal -> HA call),
  * the camera boot-OFF invariant: the POST transport is NEVER reached for a camera,
  * config defaults (`mcp_control` OFF, safe `ha_allowed_services`) + CSV parsing.

Everything runs offline: transports are injected, so no LAN / HA / cloud is touched.
The tool LOGIC is a plain function, so none of this needs the optional [mcp] SDK.
"""

import importlib
import json

import pytest

from wavr.config import load_config
from wavr.ha_client import HAClient, WavrHAError
from wavr.mcp import SENSITIVE_DOMAINS, call_ha_service

# The SAFE default allowlist (mirrors config.DEFAULT_HA_ALLOWED_SERVICES).
ALLOW = {"light.turn_on", "light.turn_off", "switch.turn_on",
         "switch.turn_off", "scene.turn_on"}


class _SpyClient:
    """A fake HAServiceCaller that records every call_service() and returns a canned
    HA-style response. `calls` staying empty proves NO actuation happened."""

    def __init__(self):
        self.calls = []

    def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return [{"entity_id": (data or {}).get("entity_id"), "state": "on"}]


# --- imports need nothing reachable / no [mcp] SDK ---------------------------------

def test_control_tool_logic_imports_without_mcp_sdk():
    m = importlib.import_module("wavr.mcp")
    assert hasattr(m, "call_ha_service") and hasattr(m, "SENSITIVE_DOMAINS")


# --- Gate 1: control flag OFF -> inert, no call ------------------------------------

def test_control_off_refuses_and_makes_no_call():
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "turn_on", "light.kitchen",
                          control_enabled=False, allowed_services=ALLOW)
    assert out["ok"] is False and out["status"] == "control_disabled"
    assert spy.calls == []            # nothing actuated while control is off


# --- Happy path: allowlisted + control ON -> delegates to HA -----------------------

def test_allowlisted_service_calls_ha_with_entity_id():
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "turn_on", "light.kitchen",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["ok"] is True and out["status"] == "called"
    assert out["result"] == [{"entity_id": "light.kitchen", "state": "on"}]
    # delegated exactly once, with the entity_id wrapped as HA expects
    assert spy.calls == [("light", "turn_on", {"entity_id": "light.kitchen"})]


# --- Gate 2: allowlist -------------------------------------------------------------

def test_non_allowlisted_service_refused_no_call():
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "toggle", "light.kitchen",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["ok"] is False and out["status"] == "not_allowed"
    assert "light.toggle" in out["message"]
    assert spy.calls == []


def test_empty_allowlist_denies_everything():
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "turn_on", "light.kitchen",
                          control_enabled=True, allowed_services=set())
    assert out["status"] == "not_allowed"
    assert spy.calls == []


# --- Gate 3: sensitive-domain consent refusal (even if allowlisted) ----------------

@pytest.mark.parametrize("domain,service,entity", [
    ("camera", "turn_on", "camera.front"),            # camera boot-OFF invariant
    ("lock", "unlock", "lock.front_door"),            # physical security boundary
    ("media_player", "turn_on", "media_player.echo"),  # could open a mic / record
    ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home"),
])
def test_sensitive_domain_refused_even_if_allowlisted(domain, service, entity):
    spy = _SpyClient()
    # deliberately put the sensitive pair IN the allowlist to prove the code backstop
    allow = ALLOW | {f"{domain}.{service}"}
    out = call_ha_service(spy, domain, service, entity,
                          control_enabled=True, allowed_services=allow)
    assert out["ok"] is False and out["status"] == "consent_required"
    assert spy.calls == []            # sensitive actuation NEVER reaches HA


def test_sensitive_hint_service_in_nonsensitive_domain_refused():
    # A camera/mic-enabling service smuggled into a non-sensitive domain is still
    # refused by the name-hint backstop (ADR-0005 §4 "anything that could enable a mic").
    spy = _SpyClient()
    out = call_ha_service(spy, "switch", "start_recording", "switch.doorbell",
                          control_enabled=True,
                          allowed_services={"switch.start_recording"})
    assert out["status"] == "consent_required"
    assert spy.calls == []


def test_case_variant_camera_cannot_bypass_sensitive_gate():
    # Mixed-case input must not evade the (lowercased) sensitive-domain set.
    spy = _SpyClient()
    out = call_ha_service(spy, "Camera", "Turn_On", "camera.front",
                          control_enabled=True, allowed_services={"camera.turn_on"})
    assert out["status"] == "consent_required"
    assert spy.calls == []


def test_sensitive_domains_constant_is_expected_set():
    assert SENSITIVE_DOMAINS == frozenset({
        "camera", "media_player", "lock", "alarm_control_panel"})


# --- Graceful degrade: HA unconfigured (ha_client None) ----------------------------

def test_ha_unconfigured_degrades_cleanly():
    # control ON + allowlisted + non-sensitive, but no HA client -> clean refusal.
    out = call_ha_service(None, "light", "turn_on", "light.kitchen",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["ok"] is False and out["status"] == "ha_unconfigured"


# --- Low-level HAClient.call_service primitive (injected POST transport) ------------

def test_call_service_posts_to_right_url_with_bearer_and_payload():
    spy = {}

    def fake_post(url, headers, body):
        spy["url"] = url
        spy["headers"] = headers
        spy["body"] = body
        return json.dumps([{"entity_id": "light.kitchen", "state": "on"}]).encode()

    client = HAClient("http://ha.local:8123/", "tok-xyz", post=fake_post)
    out = client.call_service("light", "turn_on", {"entity_id": "light.kitchen"})

    assert spy["url"] == "http://ha.local:8123/api/services/light/turn_on"
    assert spy["headers"]["Authorization"] == "Bearer tok-xyz"
    assert json.loads(spy["body"].decode()) == {"entity_id": "light.kitchen"}
    assert out == [{"entity_id": "light.kitchen", "state": "on"}]


def test_call_service_defaults_body_to_empty_object():
    spy = {}

    def fake_post(url, headers, body):
        spy["body"] = body
        return b"[]"

    HAClient("http://ha", "t", post=fake_post).call_service("scene", "turn_on")
    assert json.loads(spy["body"].decode()) == {}


def test_call_service_empty_body_returns_empty_list():
    assert HAClient("http://ha", "t", post=lambda u, h, b: b"").call_service(
        "light", "turn_on") == []
    assert HAClient("http://ha", "t", post=lambda u, h, b: None).call_service(
        "light", "turn_on") == []


def test_call_service_transport_error_raises_wavr_ha_error():
    def boom(url, headers, body):
        raise OSError("connection refused")
    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", post=boom).call_service("light", "turn_on")


def test_call_service_malformed_json_raises_wavr_ha_error():
    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", post=lambda u, h, b: b"{not json").call_service(
            "light", "turn_on")


# --- End-to-end: plain tool over a REAL HAClient with an injected POST --------------

def test_end_to_end_allowlisted_call_reaches_injected_post():
    spy = {}

    def fake_post(url, headers, body):
        spy["url"] = url
        spy["body"] = body
        return b"[]"

    client = HAClient("http://ha.local:8123", "tok", post=fake_post)
    out = call_ha_service(client, "switch", "turn_on", "switch.fan",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["ok"] is True
    assert spy["url"] == "http://ha.local:8123/api/services/switch/turn_on"
    assert json.loads(spy["body"].decode()) == {"entity_id": "switch.fan"}


def test_end_to_end_camera_never_reaches_post_transport():
    # The strongest form of the camera boot-OFF invariant: even with a real client and
    # camera.turn_on allowlisted, the POST transport is never invoked.
    called = {"n": 0}

    def fake_post(url, headers, body):
        called["n"] += 1
        return b"[]"

    client = HAClient("http://ha", "tok", post=fake_post)
    out = call_ha_service(client, "camera", "turn_on", "camera.front",
                          control_enabled=True, allowed_services={"camera.turn_on"})
    assert out["status"] == "consent_required"
    assert called["n"] == 0            # HA was NEVER asked to enable the camera


# --- Config: control defaults OFF + safe allowlist + CSV parsing -------------------

def test_config_control_defaults_off_and_safe_allowlist(monkeypatch):
    for v in ("WAVR_MCP_CONTROL", "WAVR_HA_ALLOWED_SERVICES"):
        monkeypatch.delenv(v, raising=False)
    cfg = load_config()
    assert cfg.mcp_control is False                     # opt-in: off by default
    assert cfg.ha_allowed_services == ALLOW
    # the safe default excludes every sensitive domain
    assert not any(s.split(".", 1)[0] in SENSITIVE_DOMAINS
                   for s in cfg.ha_allowed_services)


def test_config_control_reads_env_and_parses_csv(monkeypatch):
    monkeypatch.setenv("WAVR_MCP_CONTROL", "true")
    monkeypatch.setenv("WAVR_HA_ALLOWED_SERVICES", " Light.Turn_On , switch.turn_off ,")
    cfg = load_config()
    assert cfg.mcp_control is True
    # trimmed, lowercased, blanks dropped
    assert cfg.ha_allowed_services == {"light.turn_on", "switch.turn_off"}


def test_config_empty_allowlist_env_denies_all(monkeypatch):
    # set-but-empty -> fail closed (deny all), NOT fall back to the default set.
    monkeypatch.setenv("WAVR_HA_ALLOWED_SERVICES", "")
    cfg = load_config()
    assert cfg.ha_allowed_services == set()
