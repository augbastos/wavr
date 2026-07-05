"""Wavr MCP server: always-on READ tools + an opt-in, gated CONTROL tool (ADR-0005).

Exposes the LOCAL, derived Wavr presence state over the Model Context Protocol so a
local agent can *read* what the house currently senses. The read tools are READ-ONLY and
LOCAL: no read tool mutates state, toggles/creates a source, installs anything, or
reaches the cloud. `get_ha_entities` only *reads* the user's own Home Assistant on the
LAN (ADR-0005 READ half).

The one WRITE tool -- `call_ha_service` (ADR-0005 control half) -- is DEFAULT-OFF and
heavily gated, so the read-only default is preserved (with `WAVR_MCP_CONTROL` off it is
inert). It never drives a device directly: on passing every gate it asks the user's own
HA to run a service (delegation, ADR-0005 §1). The gate chain, in order:

    control-flag -> entity-id shape -> sensitive (service AND target) -> allowlist -> HA call

  1. control-flag: inert unless `WAVR_MCP_CONTROL` is on (returns a "control disabled"
     message; never errors the server).
  2. entity-id shape: exactly one concrete `domain.object_id` -- `all`, wildcards, and
     comma-lists are refused so a single call can't actuate a whole domain (audit MED-4).
  3. sensitive refusal (ADR-0005 §4), checked before the allowlist and against BOTH the
     service AND the target entity (audit HIGH-1): camera / media_player / lock /
     alarm_control_panel / cover / valve / siren / lawn_mower, any camera/mic-hinting
     name, and opaque indirection (scene / script / automation / group) are refused
     OUTRIGHT even if allowlisted -- consent is not wired yet. So a benign-looking
     `switch.turn_on`/`scene.turn_on` can't back-door a camera or lock. The camera
     boot-OFF invariant (ADR-0002) holds: the MCP can NEVER turn a camera on.
  4. allowlist: only explicit `domain.service` pairs in `ha_allowed_services` pass;
     anything else -> "service not allowed".

Local-only throughout: control calls stay Wavr -> HA on the LAN; nothing goes to cloud.
This constraint is deliberate and mirrors the rest of Wavr's privacy-first design.

Design, matching the camera/mmwave modules:
  * The `mcp` SDK is a LAZY optional dependency (the [mcp] extra). Importing this
    module never needs it -- the import lives inside build_mcp_server(), so the whole
    test suite runs (and `import wavr.mcp` succeeds) with `mcp` absent, exactly like
    pyserial in sources/mmwave.py and cv2 in sources/camera.py.
  * The tool LOGIC is plain, injectable functions taking a small read-only state
    provider. The MCP wiring is a thin lazy layer on top. Tests exercise the plain
    functions against a mocked FusionEngine / RoomState -- no `mcp` package needed.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

_log = logging.getLogger("wavr.mcp.control")


class StateProvider(Protocol):
    """The minimal READ-ONLY view the tools depend on. Anything shaped like this
    works -- the real FusionStateProvider below, or a fake in tests."""

    def list_rooms(self) -> list[str]: ...
    def room_state(self, room: str) -> dict | None: ...
    def house_map(self) -> dict: ...


class HAEntitiesProvider(Protocol):
    """The minimal READ-ONLY view the HA tool depends on: anything exposing HA's own
    entity list. The real `wavr.ha_client.HAClient` satisfies it, as does a fake in
    tests. `None` (not this shape) means HA is not configured -> the tool returns []."""

    def get_entities(self) -> list[dict]: ...


class HAServiceCaller(Protocol):
    """The minimal CONTROL view the write tool depends on: anything able to ask HA to run
    a service. The real `wavr.ha_client.HAClient` satisfies it, as does a fake in tests.
    `None` means HA is not configured -> the control tool degrades cleanly (no call).

    NOTE: this is a bare transport. All policy (control flag, allowlist, sensitive-domain
    refusal, consent) is enforced by `call_ha_service` BEFORE this is ever touched."""

    def call_service(self, domain: str, service: str, data: dict | None = None) -> object: ...


# Sensitive actuation domains (ADR-0005 §4). HARD-CODED here, NOT configurable: the
# allowlist cannot re-enable them -- they are refused even if somehow allowlisted, because
# turning a camera/mic on, unlocking a door, or disarming an alarm needs explicit human
# consent that is not wired yet. Upholds the camera boot-OFF invariant (ADR-0002).
#
# This is matched against BOTH the service's own domain AND the target entity's domain
# (a camera can be actuated via a `switch.` or `scene.` that fronts it -- audit HIGH-1),
# so every name here refuses no matter which door the actuation comes through.
SENSITIVE_DOMAINS = frozenset({
    "camera",              # MCP must NEVER turn a camera on
    "media_player",        # can start recording / open a mic / intercom
    "lock",                # physical security boundary (door locks)
    "alarm_control_panel",  # physical security boundary (arm/disarm)
    "cover",               # garage doors / gates -- physical access boundary
    "valve",               # water / gas valves -- physical safety
    "siren",               # alarms / sirens
    "lawn_mower",          # autonomous physical machine
})

# Indirection domains: opaque bundles that can fan out to ANYTHING (including a camera or
# lock) with a single innocuous-looking call. `scene.turn_on` / `script.turn_on` /
# `automation.trigger` / a `group` can each front a sensitive device, so the entity-target
# gate treats them as sensitive-by-default -- refused unless consent is wired (audit HIGH-1).
INDIRECTION_DOMAINS = frozenset({
    "scene", "script", "automation", "group",
})

# Defence-in-depth backstop: even in a non-sensitive domain, refuse any service OR target
# entity whose name implies a camera/mic/recording/streaming device (ADR-0005 §4 "anything
# that could enable a camera/mic") OR a physical-access/safety device modeled as a
# `switch`/`cover`-lookalike (a lock/garage/gate/valve/siren wired into HA as a bare
# `switch.` entity would otherwise sail past SENSITIVE_DOMAINS -- audit HIGH:
# lock/garage-as-switch bypass). Matched case-insensitively as substrings.
_SENSITIVE_HINTS = (
    "camera", "cam", "webcam", "microphone", "mic", "record", "snapshot",
    "stream", "livestream", "rtsp", "onvif", "intercom", "doorbell",
    "nvr", "dvr", "cctv", "surveillance", "poe", "baby_monitor", "babymonitor",
    "reolink", "unifi", "hikvision", "dahua", "arlo", "wyze", "blink", "ring",
    "nest", "eufy", "amcrest",
    "lock", "unlock", "deadbolt", "latch", "strike", "maglock",
    "door", "garage", "gate", "portao", "fechadura", "barrier",
    "valve", "siren", "alarm", "smoke", "co2",
    # A5.2 defense-in-depth: device-blocking is NEVER registered as an MCP tool (see the
    # extension-point warning in build_mcp_server) -- but refuse any HA entity whose name
    # could front an ARP-block/spoof/deauth capability anyway. Real control = never-register.
    "arp_block", "arpspoof", "arp_spoof", "deauth", "arp_poison",
)


class FusionStateProvider:
    """Adapts the live FusionEngine + loaded house map into the read-only StateProvider
    the tools consume. This is the ONLY place that touches Wavr internals, keeping the
    tool functions pure and trivially mockable."""

    def __init__(self, fusion, house_map: dict | None = None):
        self._fusion = fusion
        self._house_map = house_map or {}

    def list_rooms(self) -> list[str]:
        # FusionEngine holds the latest event per room in `_latest`; that key set is
        # the authoritative list of rooms Wavr currently has data for. There is no
        # public listing method and this module is read-only, so we read it directly
        # (defensively, so a differently-shaped fusion object can't break us).
        latest = getattr(self._fusion, "_latest", {}) or {}
        return sorted(latest.keys())

    def room_state(self, room: str) -> dict | None:
        rs = self._fusion.state(room)
        return rs.to_dict() if rs is not None else None

    def house_map(self) -> dict:
        return self._house_map


# --- Plain, injectable tool logic (READ-ONLY) --------------------------------------
# These are what the tests exercise directly. Each takes a StateProvider and returns
# plain JSON-serializable data. No side effects, no I/O beyond the provider.

def list_rooms(provider: StateProvider) -> list[dict]:
    """Room names with their current occupied flag and confidence. One row per room
    Wavr currently has sensing data for."""
    rooms: list[dict] = []
    for name in provider.list_rooms():
        rs = provider.room_state(name)
        if rs is None:
            continue
        rooms.append({
            "room": rs.get("room", name),
            "occupied": rs.get("occupied", False),
            "confidence": rs.get("confidence", 0.0),
        })
    return rooms


def get_room_context(provider: StateProvider, room: str) -> dict | None:
    """RoomState for one room, including the explainable "why": the per-modality
    `sources` and the human-readable `explanation`. None if the room is unknown.

    PRIVACY (audit CRITICAL-1): strips `vitals` (breathing/heart rate), `targets`
    (per-person x/y tracking) and `identities` (non-biometric "who is home" person
    labels — PII) before returning. MCP read tools must never expose per-person
    biometric, positional, or identity data -- only room-level occupancy, confidence,
    and the explainable sources/explanation are exposed here."""
    state = provider.room_state(room)
    if state is None:
        return None
    return {k: v for k, v in state.items()
            if k not in ("vitals", "targets", "identities")}


def get_house_map(provider: StateProvider) -> dict:
    """The house map (house.json / DEFAULT_MAP): room geometry only, no live state."""
    return provider.house_map()


def get_ha_entities(ha_client: HAEntitiesProvider | None) -> list[dict]:
    """List Home Assistant's OWN entities: `{entity_id, state, friendly_name, domain}`.

    The READ half of the "brain on Home Assistant" (ADR-0005): it exposes HA's entity
    list to an agent so it can *see* the home. READ-ONLY + LOCAL -- it never calls an HA
    service, actuates a device, or reaches the cloud (control is a future, opt-in,
    consent-gated slice, deliberately not built here).

    Gracefully DISABLED: when HA is not configured (`ha_client is None`, i.e. empty
    WAVR_HA_URL/WAVR_HA_TOKEN) it returns `[]` instead of failing. This does NOT touch
    Wavr's own RoomState / targets / vitals -- it only relays HA's entity list.
    """
    if ha_client is None:
        return []
    return ha_client.get_entities() or []


def _refused(status: str, message: str) -> dict:
    """A uniform, non-raising refusal result. The control tool NEVER errors the server;
    it returns `{"ok": False, ...}` so the agent gets a clear, machine-readable reason."""
    return {"ok": False, "status": status, "message": message}


def _is_sensitive(domain: str, service: str) -> bool:
    """True if `(domain, service)` is a sensitive actuation that must be refused for
    consent (ADR-0005 §4), regardless of the allowlist. Assumes lowercased inputs."""
    if domain in SENSITIVE_DOMAINS:
        return True
    pair = f"{domain}.{service}"
    return any(hint in pair for hint in _SENSITIVE_HINTS)


def _entity_is_sensitive(entity_id: str) -> bool:
    """True if the TARGET entity is sensitive, regardless of the service used to reach it
    (audit HIGH-1). A benign-looking `switch.turn_on` / `scene.turn_on` must not become a
    back door to a camera, lock, or an opaque scene. Refuses when the entity's own domain
    is sensitive OR an indirection bundle, or when its id hints at a camera/mic/stream.
    Assumes a lowercased `entity_id`."""
    eid = (entity_id or "").strip().lower()
    entity_domain = eid.split(".", 1)[0] if "." in eid else eid
    if entity_domain in SENSITIVE_DOMAINS or entity_domain in INDIRECTION_DOMAINS:
        return True
    return any(hint in eid for hint in _SENSITIVE_HINTS)


# A single, concrete entity id: `domain.object_id`, lowercase word-chars only. Rejects the
# empty string, the mass-actuation wildcard `all`, comma-lists, and any shape that isn't one
# real entity (audit MEDIUM-4) -- one gated call must never fan out to many devices.
# NOTE: matched with re.fullmatch (not .match + trailing `$`) -- `$` matches just before a
# trailing '\n', which would let a smuggled newline slip past a `.match()` check.
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _entity_id_is_valid(entity_id: str) -> bool:
    """True only for exactly one concrete `domain.object_id`. `all`, empty, wildcards, and
    comma-separated lists are rejected so a single call can't actuate a whole domain."""
    eid = (entity_id or "").strip().lower()
    if eid in ("", "all", "none", "*"):
        return False
    return bool(_ENTITY_ID_RE.fullmatch(eid))


def call_ha_service(ha_client: HAServiceCaller | None, domain: str, service: str,
                    entity_id: str, *, control_enabled: bool,
                    allowed_services) -> dict:
    """CONTROL/WRITE tool logic (ADR-0005 control half): ask HA to run one service on one
    entity -- but ONLY after every gate passes. Wavr never drives the device; HA executes.

    Gate chain (each returns a non-raising `{"ok": False, ...}` refusal; no HA call is
    made until the very end):

      1. control-flag  -- inert unless `control_enabled` (WAVR_MCP_CONTROL). Returns a
         "control disabled" message; does NOT error the server (ADR-0005 §2).
      2. entity-id     -- must be exactly one concrete `domain.object_id`; `all`, wildcards
         and comma-lists are refused so one call can't actuate a whole domain (audit MED-4).
      3. sensitive     -- checked BEFORE the allowlist (audit LOW-6) against BOTH the
         `domain.service` AND the target entity (audit HIGH-1): camera / media_player /
         lock / alarm_control_panel / cover / valve / siren / lawn_mower, any camera/mic-
         hinting name, and opaque indirection (scene / script / automation / group) are
         refused OUTRIGHT even if allowlisted -- consent is not wired yet (ADR-0005 §4). A
         benign `switch.turn_on`/`scene.turn_on` cannot back-door a sensitive device;
         camera boot-OFF invariant (ADR-0002) holds.
      4. allowlist     -- `domain.service` must be in `allowed_services`, else
         "service not allowed" (ADR-0005 §5). No "arbitrary HA action"; pairs only.
      5. HA configured -- if `ha_client is None` (HA unconfigured) degrade cleanly.

    Only on passing ALL gates does it delegate to `ha_client.call_service(domain, service,
    {"entity_id": entity_id})`. Every refusal and the one allowed call are logged (audit
    LOW-7). Inputs are normalized (stripped + lowercased) first, which also closes any
    case-based bypass of the sensitive gate (e.g. `Camera.turn_on`).
    """
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    entity_id = (entity_id or "").strip().lower()
    pair = f"{domain}.{service}"

    def _deny(status: str, message: str) -> dict:
        # Audit LOW-7: every refusal is logged (target + reason) so an operator can see
        # what an agent tried to actuate. Logged at WARNING; no secrets in the record.
        # `%r` (not `%s`) on the user-controlled pair/entity_id -- audit LOW: `.strip()`
        # only trims the ends, so an embedded newline/control char could otherwise forge
        # fake log lines (log injection). repr() escapes them into a single safe line.
        _log.warning("control refused: %r entity=%r status=%s", pair, entity_id, status)
        return _refused(status, message)

    # Gate 1 -- control flag (default OFF). Inert, never an error.
    if not control_enabled:
        return _refused(
            "control_disabled",
            "MCP control is disabled (WAVR_MCP_CONTROL off); no action taken.")

    # Gate 2 -- entity-id shape. Exactly one concrete `domain.object_id`; no `all`, no
    # wildcard, no comma-list (audit MEDIUM-4) -- one call must not fan out to a domain.
    if not _entity_id_is_valid(entity_id):
        return _deny(
            "invalid_entity",
            f"entity_id must be a single concrete 'domain.object_id'; refused: "
            f"{entity_id!r} (no 'all', wildcard, or list).")

    # Gate 3 -- sensitive-domain consent backstop, checked BEFORE the allowlist (audit
    # LOW-6) and against BOTH the service AND the target entity (audit HIGH-1). Refused
    # even if allowlisted: a benign `switch.turn_on`/`scene.turn_on` cannot back-door a
    # camera/lock/opaque-scene. Consent is not wired yet (ADR-0005 §4).
    if _is_sensitive(domain, service) or _entity_is_sensitive(entity_id):
        return _deny(
            "consent_required",
            f"'{pair}' on '{entity_id}' touches a sensitive device that needs explicit "
            f"human consent (ADR-0005 §4) not yet wired; refused. Camera/mic/lock can "
            f"never be enabled here, not even via a switch or scene.")

    # Gate 4 -- allowlist. Only explicit, pre-approved (domain, service) pairs.
    allowed = {s.strip().lower() for s in (allowed_services or ())}
    if pair not in allowed:
        return _deny("not_allowed", f"service not allowed: {pair}")

    # Graceful degrade -- HA not configured, so no call is possible.
    if ha_client is None:
        return _refused(
            "ha_unconfigured",
            "Home Assistant is not configured (WAVR_HA_URL/WAVR_HA_TOKEN empty).")

    # All gates passed -> delegate the actuation to HA (ADR-0005 §1). Wavr asks; HA acts.
    # `%r` for the same log-injection reason as `_deny` above.
    _log.info("control call: %r entity=%r", pair, entity_id)
    result = ha_client.call_service(domain, service, {"entity_id": entity_id})
    return {"ok": True, "status": "called", "domain": domain, "service": service,
            "entity_id": entity_id, "result": result}


# --- Thin, LAZY MCP wiring ---------------------------------------------------------

def build_mcp_server(provider: StateProvider, name: str = "wavr",
                     ha_client: HAEntitiesProvider | None = None,
                     control_enabled: bool = False,
                     allowed_services=frozenset()):
    """Build the MCP server: the always-on read tools + the opt-in control tool.

    The MCP SDK is imported HERE, lazily: importing `wavr.mcp` never needs the [mcp]
    extra -- only actually standing up the server does. Install it with
    `pip install .[mcp]`.

    READ TOOLS ARE READ-ONLY BY CONSTRUCTION: do NOT add another read tool here that
    mutates state, toggles/creates a source, installs anything, or reaches off the local
    box. The ONE write tool, `call_ha_service`, is gated (see below) and DEFAULT-OFF.

    `ha_client` is an optional injected Home Assistant client. When None (HA not
    configured), `get_ha_entities` degrades to `[]` and `call_ha_service` degrades to a
    clean refusal. It is LOCAL-ONLY (own HA on the LAN).

    Control gating (ADR-0005), passed in from config so this stays free of env lookups:
      * `control_enabled` (default False -> `WAVR_MCP_CONTROL` off): the control tool is
        registered but inert -- it returns a "control disabled" message. So with control
        off the read-only default is preserved: nothing can actuate.
      * `allowed_services`: the set of permitted `domain.service` pairs (ADR-0005 §5).
        Sensitive domains are refused in code regardless (ADR-0005 §4).
    """
    from mcp.server.fastmcp import FastMCP   # lazy: optional [mcp] extra

    server = FastMCP(name)

    # Each tool wrapper is given a private name (`_tool_*`) so it does NOT shadow the
    # module-level plain function it delegates to -- shadowing made those names
    # function-local for this whole scope and read-before-assignment raised
    # UnboundLocalError. The MCP-visible tool name is pinned via `name=` so the wire
    # contract (tool names + param names) stays byte-identical for any host.

    @server.tool(name="list_rooms")
    def _tool_list_rooms() -> list[dict]:
        """List every room Wavr senses, with its current occupied flag and confidence."""
        return list_rooms(provider)

    @server.tool(name="get_room_context")
    def _tool_get_room_context(room: str) -> dict | None:
        """Full state for one room, including the explainable sources + explanation."""
        return get_room_context(provider, room)

    @server.tool(name="get_house_map")
    def _tool_get_house_map() -> dict:
        """The house map / floor plan (room geometry)."""
        return get_house_map(provider)

    @server.tool(name="get_ha_entities")
    def _tool_get_ha_entities() -> list[dict]:
        """List Home Assistant entities (entity_id/state/friendly_name/domain).
        Read-only + local; empty list when HA is not configured."""
        return get_ha_entities(ha_client)

    @server.tool(name="call_ha_service")
    def _tool_call_ha_service(domain: str, service: str, entity_id: str) -> dict:
        """Ask Home Assistant to run a service on an entity (CONTROL/WRITE, ADR-0005).

        Wavr does not drive the device -- HA executes. DEFAULT-OFF: inert unless
        WAVR_MCP_CONTROL is on. Only allowlisted `domain.service` pairs are permitted, and
        sensitive domains (camera / media_player / lock / alarm_control_panel, or anything
        that could enable a camera/mic) are always refused -- consent is not wired yet, so
        a camera/mic can NEVER be enabled here. Returns `{"ok": bool, "status", ...}`;
        refusals do not error, they explain."""
        return call_ha_service(ha_client, domain, service, entity_id,
                               control_enabled=control_enabled,
                               allowed_services=allowed_services)

    # EXTENSION POINT (future slice -- NOT implemented here): a separate network slice
    # will add a read-only `get_network_inventory()` tool listing devices seen on the
    # LAN. It plugs in right here as another `@server.tool()`, reading from an injected
    # inventory provider (extend StateProvider, or inject a second provider). It MUST
    # stay READ-ONLY/LOCAL like the tools above -- observe the network, never
    # scan/probe/deploy. Do not implement it in slice D.
    #
    # PERMANENT EXCLUSION (A5.2): device blocking / ARP spoofing / deauth is an ACTIVE
    # LAN-ATTACK primitive and is PERMANENTLY OUT OF MCP SCOPE. Never add a block/arp/
    # spoof/deauth @server.tool() here or anywhere -- MCP is read-only-by-construction and
    # no agent may trigger an attack. It ships only behind a human-clicked, triple-gated
    # dashboard action (POST /api/block). Do not weaken this.

    return server


def make_server_from_app_state(fusion, house_map: dict | None = None, name: str = "wavr",
                               ha_client: HAEntitiesProvider | None = None,
                               control_enabled: bool = False,
                               allowed_services=frozenset()):
    """Convenience wiring for the app: wrap a live FusionEngine + house map and build
    the server. Kept tiny and lazy so `import wavr.mcp` stays dependency-free. Pass an
    `ha_client` (e.g. `wavr.ha_client.client_from_config(cfg)`, which is None when HA is
    unconfigured) to expose HA's entity list via `get_ha_entities`.

    Control is DEFAULT-OFF: pass `control_enabled=cfg.mcp_control` and
    `allowed_services=cfg.ha_allowed_services` to opt the gated `call_ha_service` write
    tool in (ADR-0005). With the defaults, the server stays read-only exactly as before."""
    return build_mcp_server(FusionStateProvider(fusion, house_map), name=name,
                            ha_client=ha_client, control_enabled=control_enabled,
                            allowed_services=allowed_services)
