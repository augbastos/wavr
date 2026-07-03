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
ALLOW = {"light.turn_on", "light.turn_off", "switch.turn_on", "switch.turn_off"}


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
        "camera", "media_player", "lock", "alarm_control_panel",
        "cover", "valve", "siren", "lawn_mower"})


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


# === Security-audit regression tests (HA-control write surface) ====================
# HIGH-1 (indirect sensitive actuation), MEDIUM-2 (domain set), MEDIUM-3 (hints),
# MEDIUM-4 (mass actuation), LOW-5 (name validation).

# --- HIGH-1: the TARGET entity is gated, not just the service ----------------------

def test_switch_turn_on_fronting_a_camera_is_refused():
    # The core HIGH-1 exploit: `switch.turn_on` (allowlisted, non-sensitive service) aimed
    # at an entity that IS a camera must be refused -- else a switch becomes a camera door.
    spy = _SpyClient()
    out = call_ha_service(spy, "switch", "turn_on", "camera.front_door",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["status"] == "consent_required"
    assert spy.calls == []            # camera boot-OFF invariant holds via the target gate


def test_switch_named_like_a_camera_is_refused():
    # MEDIUM-3: a switch whose id hints at a camera/mic/stream is refused even though its
    # own domain (`switch`) is benign.
    spy = _SpyClient()
    for entity in ("switch.living_room_webcam", "switch.hallway_mic",
                   "switch.front_doorbell", "switch.garden_rtsp_stream"):
        out = call_ha_service(spy, "switch", "turn_on", entity,
                              control_enabled=True, allowed_services=ALLOW)
        assert out["status"] == "consent_required", entity
    assert spy.calls == []


def test_switch_named_like_surveillance_gear_is_refused():
    # MEDIUM-3 expanded: NVR/DVR/CCTV/baby-monitor/brand-name power switches must be
    # refused too -- an allowlisted `switch.turn_on` must not power a camera/DVR/NVR
    # just because it's fronted by a "switch" entity.
    spy = _SpyClient()
    for entity in ("switch.nvr_power", "switch.cctv_relay", "switch.dvr_plug",
                   "switch.baby_monitor", "switch.reolink_poe", "switch.unifi_protect",
                   "switch.hikvision_relay", "switch.dahua_cam_power",
                   "switch.arlo_base", "switch.wyze_plug", "switch.blink_sync_module",
                   "switch.ring_doorbell_power", "switch.nest_cam_power",
                   "switch.eufy_hub", "switch.amcrest_relay"):
        out = call_ha_service(spy, "switch", "turn_on", entity,
                              control_enabled=True, allowed_services=ALLOW)
        assert out["status"] == "consent_required", entity
    assert spy.calls == []


def test_nvr_power_switch_refused_with_default_allowlist():
    # The exact scenario from the audit finding: an allowlisted switch.turn_on aimed at
    # switch.nvr_power (default allowlist + control on) must be refused, not actuated.
    spy = _SpyClient()
    out = call_ha_service(spy, "switch", "turn_on", "switch.nvr_power",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["ok"] is False and out["status"] == "consent_required"
    assert spy.calls == []


def test_lock_or_garage_modeled_as_a_switch_is_refused():
    # HIGH: a physical-access device modeled in HA as a bare `switch` (a common
    # smart-relay wiring for a lock/garage/gate) must not sail past the sensitive
    # gate just because its own domain is `switch`. Default allowlist + control on.
    spy = _SpyClient()
    for entity in ("switch.front_door_lock", "switch.garage_door", "switch.gate",
                   "switch.back_gate_portao", "switch.fechadura_principal",
                   "switch.deadbolt", "switch.mag_lock_relay", "switch.side_door"):
        out = call_ha_service(spy, "switch", "turn_on", entity,
                              control_enabled=True, allowed_services=ALLOW)
        assert out["ok"] is False and out["status"] == "consent_required", entity
    assert spy.calls == []            # lock/garage/gate NEVER actuated via a switch


def test_scene_turn_on_is_refused_as_opaque_indirection():
    # HIGH-1: a scene is an opaque bundle that could enable a camera/unlock a door, so
    # `scene.turn_on` on a scene entity is refused even if the pair is allowlisted.
    spy = _SpyClient()
    out = call_ha_service(spy, "scene", "turn_on", "scene.movie_night",
                          control_enabled=True, allowed_services=ALLOW | {"scene.turn_on"})
    assert out["status"] == "consent_required"
    assert spy.calls == []


def test_script_and_automation_targets_are_refused():
    spy = _SpyClient()
    for domain, entity in (("script", "script.open_garage"),
                           ("automation", "automation.arm_at_night"),
                           ("group", "group.all_locks")):
        out = call_ha_service(spy, "switch", "turn_on", entity,
                              control_enabled=True,
                              allowed_services=ALLOW | {f"{domain}.turn_on"})
        assert out["status"] == "consent_required", entity
    assert spy.calls == []


def test_plain_switch_and_light_still_actuate():
    # The fixes must NOT break the benign happy path: a real light/switch still delegates.
    spy = _SpyClient()
    for domain, entity in (("light", "light.kitchen"), ("switch", "switch.desk_lamp")):
        out = call_ha_service(spy, domain, "turn_on", entity,
                              control_enabled=True, allowed_services=ALLOW)
        assert out["ok"] is True and out["status"] == "called", entity
    assert len(spy.calls) == 2


# --- MEDIUM-2: expanded sensitive domains (cover / valve / siren / lawn_mower) ------

@pytest.mark.parametrize("domain,service,entity", [
    ("cover", "open_cover", "cover.garage_door"),
    ("valve", "open_valve", "valve.main_water"),
    ("siren", "turn_on", "siren.alarm"),
    ("lawn_mower", "start_mowing", "lawn_mower.backyard"),
])
def test_expanded_sensitive_domains_refused_even_if_allowlisted(domain, service, entity):
    spy = _SpyClient()
    out = call_ha_service(spy, domain, service, entity, control_enabled=True,
                          allowed_services=ALLOW | {f"{domain}.{service}"})
    assert out["status"] == "consent_required"
    assert spy.calls == []


# --- MEDIUM-4: no mass actuation (all / empty / wildcard / list) --------------------

@pytest.mark.parametrize("entity", [
    "all", "", "   ", "light.*", "*", "none",
    "light.kitchen,light.hall", "light.kitchen, switch.fan",
    "kitchen",                       # missing the domain.object shape
])
def test_mass_or_malformed_entity_id_is_refused(entity):
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "turn_on", entity,
                          control_enabled=True, allowed_services=ALLOW)
    assert out["status"] == "invalid_entity"
    assert spy.calls == []


def test_entity_id_gate_runs_before_allowlist_and_sensitive():
    # A malformed entity is rejected up front (control on) regardless of the service.
    spy = _SpyClient()
    out = call_ha_service(spy, "light", "turn_on", "all",
                          control_enabled=True, allowed_services=ALLOW)
    assert out["status"] == "invalid_entity"


# --- LOW-5: HAClient.call_service validates domain/service before building the URL ---

@pytest.mark.parametrize("domain,service", [
    ("light/../lock", "turn_on"),
    ("light", "turn_on/../unlock"),
    ("light ", "turn on"),
    ("Light", "Turn_On"),            # uppercase not allowed at the transport layer
    ("", "turn_on"),
])
def test_call_service_rejects_malformed_domain_or_service(domain, service):
    called = {"n": 0}

    def fake_post(url, headers, body):
        called["n"] += 1
        return b"[]"

    with pytest.raises(WavrHAError):
        HAClient("http://ha", "t", post=fake_post).call_service(domain, service,
                                                                {"entity_id": "light.k"})
    assert called["n"] == 0           # never reached the transport
