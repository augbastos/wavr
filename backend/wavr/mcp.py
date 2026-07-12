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

import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

from wavr.house_status import DEFAULT_NETWORK_WINDOW_MINUTES

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


class OccupancyHistoryProvider(Protocol):
    """The minimal READ-ONLY view `query_occupancy_history` depends on: anything shaped
    like `wavr.occupancy_log.OccupancyLog`'s read surface -- the real `OccupancyLog`
    already satisfies this structurally (passed straight through, no adapter needed),
    as does the stdio bridge's loopback-GET stand-in (`mcp_serve.LocalApiStateProvider`).
    `None` means occupancy history isn't enabled (`WAVR_OCCUPANCY_LOG=0`, no rows ever
    logged) -> `query_occupancy_history` degrades to an honest disabled shape, never a
    crash. `is_unusual`'s `occupied_now` is caller-supplied (mirrors GET
    /api/occupancy/unusual comparing the room's OWN current live reading, never a
    second source of truth)."""

    def timeline(self, room: str | None = None, *, start: str | None = None,
                 end: str | None = None, limit: int = 1000) -> list[dict]: ...
    def routine(self, room: str, *, weeks: float = 4.0) -> dict: ...
    def is_unusual(self, room: str, occupied_now: bool, *, weeks: float = 4.0) -> dict: ...


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


# Agent/cloud projection of a RoomState (audit CRITICAL-1): an EXPLICIT ALLOWLIST,
# mirroring get_alerts / get_network_inventory / get_house_map. Default-DENY so any
# future RoomState field (e.g. a new precision_*/biometric/positional field) is
# withheld from agents/cloud LLM until it is deliberately added here -- a blocklist
# would leak it by default. Strictly stricter than the human dashboard: room-level
# occupancy/confidence, the honest per-room person COUNT, the precision (resolution)
# rungs, and the explainable `sources`/`explanation`/`ts` only. NEVER `vitals`
# (breathing/heart rate), `targets` (per-person x/y tracking), or `identities`
# (non-biometric "who is home" labels -- PII).
_ROOM_CONTEXT_FIELDS = (
    "room", "occupied", "confidence",
    "person_count",
    "precision_level", "precision_pct", "precision_next",
    "sources", "explanation", "ts",
)


def get_room_context(provider: StateProvider, room: str) -> dict | None:
    """RoomState for one room, including the explainable "why": the per-modality
    `sources` and the human-readable `explanation`. None if the room is unknown.

    PRIVACY (audit CRITICAL-1): projects through an explicit allowlist
    (`_ROOM_CONTEXT_FIELDS`) rather than blocking known-bad keys, so `vitals`
    (breathing/heart rate), `targets` (per-person x/y tracking), `identities`
    (non-biometric "who is home" person labels — PII) AND any not-yet-allowlisted
    future field are withheld by default. MCP read tools must never expose per-person
    biometric, positional, or identity data -- only room-level occupancy, confidence,
    person count, precision rungs, and the explainable sources/explanation are exposed
    here."""
    state = provider.room_state(room)
    if state is None:
        return None
    return {k: state[k] for k in _ROOM_CONTEXT_FIELDS if k in state}


# verify FIX C (MEDIUM): unlike GET/PUT /api/house(/room) (the human dashboard's
# house-map editor, which stays fully rich -- untouched), the agent-facing MCP tool
# must NOT hand house.json's floor plan VERBATIM. house.json is treated as
# home-layout PII (git-ignored for the same class of reason wavr.db is): floor/room
# `name` is a free-text label the user typed (often descriptive of who/what lives
# there, e.g. "quarto-1"/"escritorio"), and `zones[].name` carries the same risk;
# `walls`/`features`/`backdrop` are floor-level detail this tool's room-geometry
# contract never promised either. An EXPLICIT allowlist (mirrors get_alerts/
# get_network_inventory, Phase-2A verify FIX 1/3): only room `id` + `polygon`
# survive, grouped by floor `id`/`level` (structural identifiers, not a label the
# user wrote) so a multi-floor house's room geometry stays attributable to a floor
# without exposing any name/label/note/free-text field.
_HOUSE_MAP_ROOM_FIELDS = ("id", "polygon")


def _minimize_room_for_agent(room: dict) -> dict:
    """One room dict (house.json v2 `_ROOM_KEYS` shape: id/name/polygon) -> the
    coarse MCP-agent projection. See `get_house_map`'s docstring for the rationale."""
    return {k: room.get(k) for k in _HOUSE_MAP_ROOM_FIELDS}


def _minimize_floor_for_agent(floor: dict) -> dict:
    """One floor dict -> id/level (structural) + its minimized rooms. Drops `name`
    (a free-text floor label) and `walls`/`features`/`zones`/`backdrop` entirely --
    see `get_house_map`'s docstring."""
    rooms = floor.get("rooms")
    rooms = rooms if isinstance(rooms, list) else []
    return {
        "id": floor.get("id"),
        "level": floor.get("level"),
        "rooms": [_minimize_room_for_agent(r) for r in rooms if isinstance(r, dict)],
    }


def get_house_map(provider: StateProvider) -> dict:
    """The house map (house.json / DEFAULT_MAP), MINIMIZED for the agent-facing MCP
    surface (verify FIX C -- MEDIUM): room id + polygon/geometry only, grouped by
    floor id/level -- NEVER a floor/room/zone `name` or any other free-text field,
    and never `walls`/`features`/`backdrop` -- see the module-level rationale above
    `_minimize_room_for_agent`. This does NOT reuse `provider.house_map()`'s verbatim
    v2 doc -- it is this tool's OWN, stricter projection (GET/PUT /api/house(/room),
    the human dashboard's house-map editor, are UNCHANGED and stay fully rich). A
    non-dict house or a missing/malformed `floors` list degrades to `{"floors": []}`,
    never a crash."""
    house = provider.house_map()
    floors = house.get("floors") if isinstance(house, dict) else None
    floors = floors if isinstance(floors, list) else []
    return {"floors": [_minimize_floor_for_agent(f) for f in floors if isinstance(f, dict)]}


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


# --- Whole-house read tools (Phase 2A / B1-B3) --------------------------------------
# Same discipline as the room tools above: plain, injectable functions; each degrades
# to a clear, honest empty/disabled shape when its data source isn't wired -- NEVER a
# raise, so a caller's missing feature (occupancy log off, no network inventory) can
# never crash the MCP server. Inputs are callables/duck-typed objects, not app.py
# internals, so these stay trivially mockable in tests (mirrors `ha_client` above).

# Phase-2A verify FIX 1 (HIGH): unlike GET /api/inventory (the human dashboard, which
# stays fully rich -- untouched), the MCP tool must NOT hand an agent per-device name
# (a friendly label, often a person's name), hostname (embeds the owner's own name),
# first_seen/last_seen (a "who is home" timing profile), or open_ports (LAN attack
# surface). `sources` is dropped for the SAME reason as hostname: recog's evidence
# trail can itself embed a self-reported hostname/friendly-name string (wavr.recog's
# "hostname" signal literally records `f"{hostname} -> {dtype}"`), so passing it
# through would leak the very PII the hostname drop is trying to close. `mac` is
# dropped too -- it is a more persistent per-device tracking identifier than `ip`
# (which is DHCP-mutable); `ip` is kept only because a tool caller needs SOME way to
# refer back to a specific device. This mirrors the discipline `get_room_context`
# already uses (strip `identities`/`vitals`/`targets`) -- an EXPLICIT allowlist, not a
# blocklist, so a future field added to `_device_view` is excluded by default rather
# than silently leaking through.
_INVENTORY_AGENT_CORE_FIELDS = ("ip", "vendor", "device_type", "type_confidence", "known")
_INVENTORY_AGENT_OPTIONAL_FIELDS = ("make", "model", "os")


def _minimize_device_for_agent(device: dict) -> dict:
    """One device dict (the `GET /api/inventory` / `_device_view` shape) -> the
    coarse MCP-agent projection. See `get_network_inventory`'s docstring for the
    field-by-field rationale. Optional fields are included only when populated,
    mirroring `_device_view`'s own additive-field convention."""
    out = {k: device.get(k) for k in _INVENTORY_AGENT_CORE_FIELDS}
    for k in _INVENTORY_AGENT_OPTIONAL_FIELDS:
        if device.get(k):
            out[k] = device[k]
    if device.get("is_gateway"):
        out["is_gateway"] = True
    return out


def get_network_inventory(inventory_fn) -> dict:
    """The current LAN device inventory, MINIMIZED for the agent-facing MCP surface
    (Phase-2A verify FIX 1 -- HIGH): coarse identity only -- `ip` (to reference a
    device), `vendor`/`device_type`/`type_confidence`/`known`, and `make`/`model`/
    `os`/`is_gateway` when populated -- plus a `count`. `mac`, `name`, `hostname`,
    `first_seen`, `last_seen`, `open_ports`, and `sources` are DROPPED; see
    `_minimize_device_for_agent`'s docstring for why each one is PII/tracking
    surface an agent must never see. This does NOT reuse `wavr.api_inventory.
    inventory_view()`'s rich output verbatim -- it is this tool's OWN, stricter
    projection (`GET /api/inventory`, the human dashboard, is UNCHANGED and stays
    fully rich). `inventory_fn` is a zero-arg callable returning the same list of
    device dicts `inventory_view` produces (this tool never triggers a scan itself,
    only reads the current cache). `None` (not wired) -> `{"devices": [], "count":
    0}`, never a crash."""
    if inventory_fn is None:
        return {"devices": [], "count": 0}
    devices = [_minimize_device_for_agent(d) for d in (inventory_fn() or [])]
    return {"devices": devices, "count": len(devices)}


# Phase-2A verify FIX 3 (LOW): unlike GET /api/alerts (the human dashboard, which
# stays fully rich -- untouched), the MCP tool must NOT hand an agent the live
# `known_present` family headcount (an intrusion alert's compare-against count),
# gateway/rogue MAC+IP fields (`gateway_ip`/`trusted_mac`/`observed_mac`/`mac`/
# `extra_server` = LAN topology), or `vendor`/`hostname`/`device_type` (the same
# per-device PII class FIX 1 drops from inventory). An EXPLICIT allowlist (mirrors
# FIX 1): only `kind`/`severity`/`room`/`ts` survive -- `room` is `None` for the
# network-layer alert kinds (rogue_device/rogue_dhcp/gateway_identity), which carry
# no room at all, so every projected alert has the SAME stable four-key shape.
_ALERT_AGENT_FIELDS = ("kind", "severity", "room", "ts")


def _minimize_alert_for_agent(alert: dict) -> dict:
    """One merged alert dict (`wavr.api_inventory.merge_alerts`'s shape) -> the
    coarse MCP-agent projection. See `get_alerts`'s docstring for the rationale."""
    return {k: alert.get(k) for k in _ALERT_AGENT_FIELDS}


def get_alerts(alerts_fn) -> dict:
    """Current active alerts/notifications, MINIMIZED for the agent-facing MCP
    surface (Phase-2A verify FIX 3 -- LOW): only `kind`/`severity`/`room`/`ts` --
    enough for an agent to know THAT something is alerting, where, and how
    severely, without the live family headcount (`known_present`) or LAN-topology
    identifiers (gateway/rogue MAC+IP, vendor, hostname). `alerts_fn` is a zero-arg
    callable returning the same merged, chronologically-sorted list `GET
    /api/alerts` returns (`wavr.api_inventory.merge_alerts`) -- this tool builds
    its OWN stricter projection from it rather than reusing it verbatim; `GET
    /api/alerts` (the human dashboard) is UNCHANGED and stays fully rich. `None`
    (not wired) -> `{"alerts": []}`, never a crash."""
    if alerts_fn is None:
        return {"alerts": []}
    alerts = list(alerts_fn() or [])
    return {"alerts": [_minimize_alert_for_agent(a) for a in alerts]}


_OCCUPANCY_HISTORY_LIMIT = 1000
_OCCUPANCY_MAX_HOURS = 24 * 365  # defensive clamp -- never an unbounded query

# Phase-2A verify FIX 2 (HIGH): a SEPARATE, much stricter clamp than the defensive
# backstop above. `_OCCUPANCY_MAX_HOURS` (~1yr) only guards against a genuinely
# unbounded query; it says nothing about privacy. A multi-week per-room
# occupancy + person_count timeline (plus the routine() baseline) is a "when is
# the house empty" profile -- exactly the kind of surface that must be STRICTER
# for the agent-facing MCP tool than for the human /api/occupancy/history route
# (which keeps its own 60-day retention, UNCHANGED). Enforced at the MCP TOOL
# WRAPPER (`build_mcp_server`'s `_tool_query_occupancy_history`), not inside this
# shared plain function, so a future non-MCP caller of `query_occupancy_history`
# would be unaffected by the agent-specific clamp.
_AGENT_OCCUPANCY_MAX_HOURS = 24


def query_occupancy_history(provider: StateProvider,
                            occupancy_provider: OccupancyHistoryProvider | None,
                            room: str | None = None, hours: int = 24) -> dict:
    """The Phase-1 house memory (`wavr.occupancy_log`), bundled into one call: the raw
    timeline over the trailing `hours` (mirrors `GET /api/occupancy/history`), plus --
    ONLY when `room` is given, since both are inherently per-room -- that room's hourly
    routine baseline (`GET /api/occupancy/routine`) and whether its CURRENT reading is
    unusual for this hour (`GET /api/occupancy/unusual`, via `provider.room_state`, the
    SAME live reading `list_rooms`/`get_room_context` already expose -- never a second
    source of truth for "occupied now").

    PRIVACY: identical allowlist to the three HTTP endpoints this wraps -- room,
    occupied, person_count, confidence, ts only (occupancy_log.py never stores
    geometry, vitals, or per-person identity -- see its module docstring). Respects the
    same Watch-mode suppression every other egress point does; this tool adds no new
    disclosure beyond what those three endpoints already return.

    Gracefully DISABLED (`enabled: False`, empty history, no routine/unusual) when
    `WAVR_OCCUPANCY_LOG` is off (`occupancy_provider is None`) -- never a crash."""
    if occupancy_provider is None:
        return {"enabled": False, "history": [], "routine": None, "unusual": None}
    hours = max(1, min(int(hours), _OCCUPANCY_MAX_HOURS))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).isoformat()
    history = occupancy_provider.timeline(room, start=start, limit=_OCCUPANCY_HISTORY_LIMIT)
    result: dict = {"enabled": True, "history": history, "routine": None, "unusual": None}
    if room is not None:
        result["routine"] = occupancy_provider.routine(room)
        current = provider.room_state(room)
        if current is not None:
            result["unusual"] = occupancy_provider.is_unusual(room, bool(current.get("occupied")))
    return result


# verify FIX B (MEDIUM): `wavr.house_status._network_what` embeds LIVE network
# identifiers (vendor / extra_server / gateway_ip) into `reasons[].what`'s free-text
# caption for network-layer reasons -- get_house_status sits in the CLOUD-reachable
# coarse scope (AGENT_DEFAULT_TOOL_SCOPE), so those identifiers must never reach an
# agent, cloud or local. An EXPLICIT allowlist (mirrors get_alerts/
# get_network_inventory, Phase-2A verify FIX 1/3): only `layer`/`kind`/`severity`/
# `ts` survive per reason -- the free-text `what` caption is dropped ENTIRELY (not
# only for the network layer) so a future caption change can never silently
# reintroduce a leak through this same field. The top-level `status`/`score`/`ts`
# verdict is its own allowlist too (already coarse, no raw identifier): an agent can
# still say "everything's fine" / "there's a notice-level network reason" without
# ever seeing WHICH device/gateway/DHCP server it names.
_HOUSE_STATUS_REASON_FIELDS = ("layer", "kind", "severity", "ts")


def _minimize_house_status_reason_for_agent(reason: dict) -> dict:
    """One reason dict (`wavr.house_status.compose_house_status`'s shape) -> the
    coarse MCP-agent projection. See `get_house_status`'s docstring for the
    rationale."""
    return {k: reason.get(k) for k in _HOUSE_STATUS_REASON_FIELDS}


def _minimize_house_status_for_agent(status: dict) -> dict:
    reasons = status.get("reasons")
    reasons = reasons if isinstance(reasons, list) else []
    return {
        "status": status.get("status"),
        "score": status.get("score"),
        "reasons": [_minimize_house_status_reason_for_agent(r)
                   for r in reasons if isinstance(r, dict)],
        "ts": status.get("ts"),
    }


async def get_house_status(house_status_fn, window_minutes: float = DEFAULT_NETWORK_WINDOW_MINUTES) -> dict:
    """The unified "is everything OK at home" verdict, MINIMIZED for the
    agent-facing MCP surface (verify FIX B -- MEDIUM) from the EXACT composition
    `GET /api/house-status` returns (`wavr.house_status.compose_house_status`, fusing
    network + physical signals that already exist elsewhere -- no new detection
    here): `status`/`score`/`ts` are kept (already coarse), but each `reasons[]`
    entry is reduced to `layer`/`kind`/`severity`/`ts` -- the free-text `what`
    caption is dropped entirely, because `_network_what` embeds live network
    identifiers (vendor / extra_server / gateway_ip) for network-layer reasons and
    this tool is reachable from the CLOUD-scoped default tool set. This is this
    tool's OWN, stricter projection; `GET /api/house-status` (the human dashboard)
    is UNCHANGED and stays fully rich. `house_status_fn` is a
    `callable(window_minutes) -> dict | Awaitable[dict]`: either a sync bridge (the
    stdio loopback GET) or an async in-process composer (app.py, which fans out
    concurrent per-room reads) satisfies it -- awaited only if it actually returns
    an awaitable, so both work with no adapter. `None` (not wired), or a malformed
    (non-dict) result, degrades to an honest `{"status": "unknown", ...}` verdict,
    never a crash."""
    if house_status_fn is None:
        return {"status": "unknown", "score": 0, "reasons": [], "ts": None}
    result = house_status_fn(window_minutes)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        return {"status": "unknown", "score": 0, "reasons": [], "ts": None}
    return _minimize_house_status_for_agent(result)


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
                     allowed_services=frozenset(), *,
                     expose_control: bool = True,
                     stateless_http: bool = False,
                     json_response: bool = False,
                     transport_security=None,
                     network_inventory_fn=None,
                     alerts_fn=None,
                     occupancy_provider: OccupancyHistoryProvider | None = None,
                     house_status_fn=None):
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

    Whole-house read tools (Phase 2A / B1-B3) -- each optional, each degrades to a
    clear empty/disabled shape (never a crash) when its source isn't wired:
      * `network_inventory_fn` -- zero-arg callable -> the current LAN inventory (same
        shape as `GET /api/inventory`; the tool never scans, only reads).
      * `alerts_fn` -- zero-arg callable -> the current merged alert list (same shape
        as `GET /api/alerts`).
      * `occupancy_provider` -- an `OccupancyHistoryProvider` (the real
        `wavr.occupancy_log.OccupancyLog` already satisfies it) -> the house's
        occupancy memory (`query_occupancy_history`, wrapping `/api/occupancy/
        {history,routine,unusual}`). `None` when `WAVR_OCCUPANCY_LOG` is off.
      * `house_status_fn` -- `callable(window_minutes) -> dict | Awaitable[dict]` ->
        the composed "is everything OK at home" verdict (same shape as
        `GET /api/house-status`).

    Control gating (ADR-0005), passed in from config so this stays free of env lookups:
      * `control_enabled` (default False -> `WAVR_MCP_CONTROL` off): the control tool is
        registered but inert -- it returns a "control disabled" message. So with control
        off the read-only default is preserved: nothing can actuate.
      * `allowed_services`: the set of permitted `domain.service` pairs (ADR-0005 §5).
        Sensitive domains are refused in code regardless (ADR-0005 §4).

    Transport shaping (ADR-0008, Slice 1 -- the in-app streamable-HTTP mount):
      * `expose_control` (default True): when False, `call_ha_service` is NOT registered
        at all -- ABSENT from `list_tools`, not merely inert. The HTTP transport passes
        False so it is READ-ONLY by construction (mcp.py's control gate is process-global,
        not per-caller, so control must not be reachable over the network). The stdio
        bridge keeps the default (full gated toolset), unchanged.
      * `stateless_http` / `json_response` / `transport_security`: forwarded straight to
        FastMCP for the streamable-HTTP transport. Defaults reproduce the plain stdio
        server byte-for-byte (no kwargs passed to FastMCP), so the stdio path is untouched.
    """
    from mcp.server.fastmcp import FastMCP   # lazy: optional [mcp] extra

    # Only pass the streamable-HTTP kwargs when explicitly set, so the default (stdio)
    # construction stays `FastMCP(name)` -- byte-identical to before this slice.
    _fastmcp_kwargs: dict = {}
    if stateless_http:
        _fastmcp_kwargs["stateless_http"] = True
    if json_response:
        _fastmcp_kwargs["json_response"] = True
    if transport_security is not None:
        _fastmcp_kwargs["transport_security"] = transport_security
    server = FastMCP(name, **_fastmcp_kwargs)

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
        """The house map / floor plan, MINIMIZED for the agent-facing MCP surface
        (verify FIX C): room id + polygon only, grouped by floor id/level -- NEVER
        a floor/room/zone name/label/note or walls/features/backdrop -- see
        `get_house_map`'s docstring."""
        return get_house_map(provider)

    @server.tool(name="get_ha_entities")
    async def _tool_get_ha_entities() -> list[dict]:
        """List Home Assistant entities (entity_id/state/friendly_name/domain).
        Read-only + local; empty list when HA is not configured.

        Async + off-loop (ADR-0008 red-team MED): get_ha_entities makes a SYNCHRONOUS HA
        HTTP GET (urllib, ~5s timeout). Over the in-app /mcp mount the tool runs on the
        MAIN event loop, so a slow/blackholed HA would otherwise stall the whole server
        (dashboard WS + every peer). Offload the blocking call to a worker thread so one
        slow HA read can never freeze the loop. Harmless on the stdio bridge (separate
        process); anyio ships with the [mcp] SDK, imported lazily like FastMCP above."""
        import anyio
        return await anyio.to_thread.run_sync(get_ha_entities, ha_client)

    @server.tool(name="get_network_inventory")
    def _tool_get_network_inventory() -> dict:
        """List every device Wavr currently sees on the LAN, MINIMIZED for the
        agent-facing MCP surface (Phase-2A verify FIX 1): ip/vendor/device_type/
        type_confidence/known + make/model/os/is_gateway when populated, plus a
        count. NEVER mac/name/hostname/first_seen/last_seen/open_ports/sources --
        see `get_network_inventory`'s docstring. Read-only: reads the
        already-scanned inventory, never triggers a new scan. Empty devices list
        when network inventory isn't wired/enabled."""
        return get_network_inventory(network_inventory_fn)

    @server.tool(name="get_alerts")
    def _tool_get_alerts() -> dict:
        """Current active alerts/notifications, MINIMIZED for the agent-facing MCP
        surface (Phase-2A verify FIX 3): kind/severity/room/ts only -- NEVER the
        live known_present headcount or gateway/rogue MAC+IP/vendor/hostname --
        see `get_alerts`'s docstring. Read-only. Empty alerts list when not wired."""
        return get_alerts(alerts_fn)

    @server.tool(name="query_occupancy_history")
    def _tool_query_occupancy_history(room: str | None = None, hours: int = 24) -> dict:
        """The house's occupancy memory: raw history over the trailing `hours`
        (optionally filtered to one `room`), plus -- when `room` is given -- that
        room's hourly routine baseline and whether its current reading is unusual for
        this hour. Room-level occupancy/person_count/confidence only, never geometry
        or identity. Disabled shape (`enabled: False`) when occupancy history isn't
        enabled (WAVR_OCCUPANCY_LOG=0).

        Phase-2A verify FIX 2 (HIGH): `hours` is clamped to
        `_AGENT_OCCUPANCY_MAX_HOURS` (24h) HERE, at the MCP tool wrapper -- a
        multi-week per-room timeline is a "when is the house empty" profile, so
        the agent-facing surface is bounded far tighter than the ~1yr defensive
        backstop inside the shared `query_occupancy_history` function (which
        itself wraps the 60-day-retention `/api/occupancy/*` routes, UNCHANGED)."""
        hours = min(int(hours), _AGENT_OCCUPANCY_MAX_HOURS)
        return query_occupancy_history(provider, occupancy_provider, room=room, hours=hours)

    @server.tool(name="get_house_status")
    async def _tool_get_house_status(window_minutes: float = DEFAULT_NETWORK_WINDOW_MINUTES) -> dict:
        """The composed "is everything OK at home" verdict, fusing the network layer
        (rogue-device/rogue-DHCP/gateway-identity) with the physical layer
        (intrusion/fall-suspected/occupancy-anomaly) that already exist elsewhere --
        no new detection here, just the same ranked status/score GET /api/house-status
        returns. MINIMIZED for the agent-facing MCP surface (verify FIX B): each
        `reasons[]` entry is layer/kind/severity/ts only -- NEVER the free-text `what`
        caption, which can embed live network identifiers (vendor/gateway/DHCP-server)
        -- see `get_house_status`'s docstring. `window_minutes` overrides the
        network-alert recency window."""
        return await get_house_status(house_status_fn, window_minutes)

    # CONTROL/WRITE tool -- registered ONLY when expose_control is True. The HTTP transport
    # passes expose_control=False so this tool is ABSENT from list_tools (not merely inert):
    # mcp.py's control gate is process-global, not per-caller, so control must never be
    # reachable over the network (ADR-0008). The stdio bridge keeps it (default True).
    if expose_control:
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

    # PERMANENT EXCLUSION (A5.2): device blocking / ARP spoofing / deauth is an ACTIVE
    # LAN-ATTACK primitive and is PERMANENTLY OUT OF MCP SCOPE. Never add a block/arp/
    # spoof/deauth @server.tool() here or anywhere -- MCP is read-only-by-construction and
    # no agent may trigger an attack. It ships only behind a human-clicked, triple-gated
    # dashboard action (POST /api/block). Do not weaken this.

    return server


def make_server_from_app_state(fusion, house_map: dict | None = None, name: str = "wavr",
                               ha_client: HAEntitiesProvider | None = None,
                               control_enabled: bool = False,
                               allowed_services=frozenset(), *,
                               network_inventory_fn=None,
                               alerts_fn=None,
                               occupancy_provider: OccupancyHistoryProvider | None = None,
                               house_status_fn=None):
    """Convenience wiring for the app: wrap a live FusionEngine + house map and build
    the server. Kept tiny and lazy so `import wavr.mcp` stays dependency-free. Pass an
    `ha_client` (e.g. `wavr.ha_client.client_from_config(cfg)`, which is None when HA is
    unconfigured) to expose HA's entity list via `get_ha_entities`.

    Control is DEFAULT-OFF: pass `control_enabled=cfg.mcp_control` and
    `allowed_services=cfg.ha_allowed_services` to opt the gated `call_ha_service` write
    tool in (ADR-0005). With the defaults, the server stays read-only exactly as before.

    The whole-house read tools (Phase 2A / B1-B3) are pass-through -- see
    `build_mcp_server`'s docstring for what each of `network_inventory_fn` /
    `alerts_fn` / `occupancy_provider` / `house_status_fn` expects. All default to
    None (each tool degrades to its honest empty/disabled shape), so an existing
    caller of this function is unaffected until it opts in."""
    return build_mcp_server(FusionStateProvider(fusion, house_map), name=name,
                            ha_client=ha_client, control_enabled=control_enabled,
                            allowed_services=allowed_services,
                            network_inventory_fn=network_inventory_fn, alerts_fn=alerts_fn,
                            occupancy_provider=occupancy_provider,
                            house_status_fn=house_status_fn)
