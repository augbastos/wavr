"""Read-only Wavr MCP server.

Exposes the LOCAL, derived Wavr presence state over the Model Context Protocol so a
local agent can *read* what the house currently senses -- nothing more. Every tool
here is READ-ONLY and LOCAL: no tool mutates state, toggles/creates a source,
installs anything, controls a device, or reaches the cloud. The one tool that talks
to another box -- `get_ha_entities` -- only *reads* the user's own Home Assistant on
the LAN (ADR-0005 READ half; control is a future, opt-in, consent-gated slice that is
NOT built here). That constraint is deliberate and mirrors the rest of Wavr's
privacy-first design (see the note on build_mcp_server below).

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


# --- Thin, LAZY MCP wiring ---------------------------------------------------------

def build_mcp_server(provider: StateProvider, name: str = "wavr",
                     ha_client: HAEntitiesProvider | None = None):
    """Build the MCP server exposing the read-only tools above, bound to `provider`.

    The MCP SDK is imported HERE, lazily: importing `wavr.mcp` never needs the [mcp]
    extra -- only actually standing up the server does. Install it with
    `pip install .[mcp]`.

    READ-ONLY BY CONSTRUCTION: only read tools are registered. Do NOT add a tool here
    that mutates state, toggles/creates a source, controls a device, installs anything,
    or reaches off the local box -- this is a local read interface, period.

    `ha_client` is an optional injected read-only Home Assistant client (ADR-0005 READ
    half). When None (HA not configured), `get_ha_entities` degrades to an empty list.
    The HA client is LOCAL-ONLY (own HA on the LAN) and never actuates anything.
    """
    from mcp.server.fastmcp import FastMCP   # lazy: optional [mcp] extra

    # Capture the plain functions before the tool wrappers below shadow their names.
    _list_rooms = list_rooms
    _get_room_context = get_room_context
    _get_house_map = get_house_map
    _get_ha_entities = get_ha_entities

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

    # EXTENSION POINT (future slice -- NOT implemented here): a separate network slice
    # will add a read-only `get_network_inventory()` tool listing devices seen on the
    # LAN. It plugs in right here as another `@server.tool()`, reading from an injected
    # inventory provider (extend StateProvider, or inject a second provider). It MUST
    # stay READ-ONLY/LOCAL like the tools above -- observe the network, never
    # scan/probe/deploy. Do not implement it in slice D.

    return server


def make_server_from_app_state(fusion, house_map: dict | None = None, name: str = "wavr",
                               ha_client: HAEntitiesProvider | None = None):
    """Convenience wiring for the app: wrap a live FusionEngine + house map and build
    the server. Kept tiny and lazy so `import wavr.mcp` stays dependency-free. Pass a
    read-only `ha_client` (e.g. `wavr.ha_client.client_from_config(cfg)`, which is None
    when HA is unconfigured) to expose HA's entity list via `get_ha_entities`."""
    return build_mcp_server(FusionStateProvider(fusion, house_map), name=name,
                            ha_client=ha_client)
