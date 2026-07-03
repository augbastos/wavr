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

    control-flag  ->  allowlist  ->  sensitive-domain (consent) refusal  ->  HA call

  1. control-flag: inert unless `WAVR_MCP_CONTROL` is on (returns a "control disabled"
     message; never errors the server).
  2. allowlist: only explicit `domain.service` pairs in `ha_allowed_services` pass;
     anything else -> "service not allowed".
  3. sensitive-domain refusal (ADR-0005 §4): camera / media_player / lock /
     alarm_control_panel (and any service that could enable a camera/mic) are refused
     OUTRIGHT even if somehow allowlisted -- consent for these is not wired yet. The
     camera boot-OFF invariant (ADR-0002) holds: the MCP can NEVER turn a camera on.

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

from typing import Protocol


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
SENSITIVE_DOMAINS = frozenset({
    "camera",              # MCP must NEVER turn a camera on
    "media_player",        # can start recording / open a mic / intercom
    "lock",                # physical security boundary (door locks)
    "alarm_control_panel",  # physical security boundary (arm/disarm)
})

# Defence-in-depth backstop: even in a non-sensitive domain, refuse any service whose
# name implies enabling a camera/mic or recording (ADR-0005 §4 "anything that could
# enable a camera/mic"). Compared case-insensitively against `domain.service`.
_SENSITIVE_HINTS = ("camera", "microphone", "record", "snapshot")


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
    """Full RoomState for one room, including the explainable "why": the per-modality
    `sources` and the human-readable `explanation`. None if the room is unknown."""
    return provider.room_state(room)


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


def call_ha_service(ha_client: HAServiceCaller | None, domain: str, service: str,
                    entity_id: str, *, control_enabled: bool,
                    allowed_services) -> dict:
    """CONTROL/WRITE tool logic (ADR-0005 control half): ask HA to run one service on one
    entity -- but ONLY after every gate passes. Wavr never drives the device; HA executes.

    Gate chain (each returns a non-raising `{"ok": False, ...}` refusal; no HA call is
    made until the very end):

      1. control-flag  -- inert unless `control_enabled` (WAVR_MCP_CONTROL). Returns a
         "control disabled" message; does NOT error the server (ADR-0005 §2).
      2. allowlist     -- `domain.service` must be in `allowed_services`, else
         "service not allowed" (ADR-0005 §5). No "arbitrary HA action"; pairs only.
      3. sensitive     -- camera / media_player / lock / alarm_control_panel (and any
         camera/mic-enabling service) are refused OUTRIGHT even if allowlisted; consent
         is not wired yet (ADR-0005 §4). Camera boot-OFF invariant (ADR-0002) holds.
      4. HA configured -- if `ha_client is None` (HA unconfigured) degrade cleanly.

    Only on passing ALL gates does it delegate to `ha_client.call_service(domain, service,
    {"entity_id": entity_id})`. Inputs are normalized (stripped + lowercased) first, which
    also closes any case-based bypass of the sensitive-domain gate (e.g. `Camera.turn_on`).
    """
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    pair = f"{domain}.{service}"

    # Gate 1 -- control flag (default OFF). Inert, never an error.
    if not control_enabled:
        return _refused(
            "control_disabled",
            "MCP control is disabled (WAVR_MCP_CONTROL off); no action taken.")

    # Gate 2 -- allowlist. Only explicit, pre-approved (domain, service) pairs.
    allowed = {s.strip().lower() for s in (allowed_services or ())}
    if pair not in allowed:
        return _refused("not_allowed", f"service not allowed: {pair}")

    # Gate 3 -- sensitive-domain consent backstop. Refused even if allowlisted.
    if _is_sensitive(domain, service):
        return _refused(
            "consent_required",
            f"'{pair}' targets a sensitive domain that needs explicit human consent "
            f"(ADR-0005 §4) not yet wired; refused. Camera/mic can never be enabled here.")

    # Graceful degrade -- HA not configured, so no call is possible.
    if ha_client is None:
        return _refused(
            "ha_unconfigured",
            "Home Assistant is not configured (WAVR_HA_URL/WAVR_HA_TOKEN empty).")

    # All gates passed -> delegate the actuation to HA (ADR-0005 §1). Wavr asks; HA acts.
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

    # Capture the plain functions before the tool wrappers below shadow their names.
    _list_rooms = list_rooms
    _get_room_context = get_room_context
    _get_house_map = get_house_map
    _get_ha_entities = get_ha_entities
    _call_ha_service = call_ha_service

    server = FastMCP(name)

    @server.tool()
    def list_rooms() -> list[dict]:
        """List every room Wavr senses, with its current occupied flag and confidence."""
        return _list_rooms(provider)

    @server.tool()
    def get_room_context(room: str) -> dict | None:
        """Full state for one room, including the explainable sources + explanation."""
        return _get_room_context(provider, room)

    @server.tool()
    def get_house_map() -> dict:
        """The house map / floor plan (room geometry)."""
        return _get_house_map(provider)

    @server.tool()
    def get_ha_entities() -> list[dict]:
        """List Home Assistant entities (entity_id/state/friendly_name/domain).
        Read-only + local; empty list when HA is not configured."""
        return _get_ha_entities(ha_client)

    @server.tool()
    def call_ha_service(domain: str, service: str, entity_id: str) -> dict:
        """Ask Home Assistant to run a service on an entity (CONTROL/WRITE, ADR-0005).

        Wavr does not drive the device -- HA executes. DEFAULT-OFF: inert unless
        WAVR_MCP_CONTROL is on. Only allowlisted `domain.service` pairs are permitted, and
        sensitive domains (camera / media_player / lock / alarm_control_panel, or anything
        that could enable a camera/mic) are always refused -- consent is not wired yet, so
        a camera/mic can NEVER be enabled here. Returns `{"ok": bool, "status", ...}`;
        refusals do not error, they explain."""
        return _call_ha_service(ha_client, domain, service, entity_id,
                                control_enabled=control_enabled,
                                allowed_services=allowed_services)

    # EXTENSION POINT (future slice -- NOT implemented here): a separate network slice
    # will add a read-only `get_network_inventory()` tool listing devices seen on the
    # LAN. It plugs in right here as another `@server.tool()`, reading from an injected
    # inventory provider (extend StateProvider, or inject a second provider). It MUST
    # stay READ-ONLY/LOCAL like the tools above -- observe the network, never
    # scan/probe/deploy. Do not implement it in slice D.

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
