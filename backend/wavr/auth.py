"""Pure authorization logic for multi-device client auth (ADR-0006).

No FastAPI, no I/O beyond the injected DeviceStore — just plain functions so the
load-bearing access decision is fully unit-testable in isolation. The app wiring
(app.py) calls `authorize` from the request middleware and maps its result to
allow/deny + role.

The rule (only reached when `WAVR_MULTIDEVICE` is on; otherwise app.py stays
strictly loopback-only, unchanged):

  * loopback peer                      -> "root"  (the DB-owning root central)
  * in-subnet peer + valid token       -> the device's role ("central" / "user")
  * everything else                    -> None    (deny / 403)

On-Wi-Fi alone is never enough: a valid, non-revoked per-device token is required,
AND the peer must be in the host's local /24.
"""
from __future__ import annotations

import ipaddress

# Mirrors app.py's loopback set (kept local so this module has no app.py import).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header, or None if
    the header is absent/malformed. Case-insensitive on the scheme."""
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def in_subnet(peer_host: str, local_ip: str) -> bool:
    """True iff `peer_host` is in the same IPv4 /24 as `local_ip`. Returns False on
    any unparseable/non-IPv4 input (fail-closed) — the loopback root path is handled
    separately in `authorize`, so this is only ever asked about LAN peers."""
    try:
        peer = ipaddress.ip_address(peer_host)
        local = ipaddress.ip_address(local_ip)
    except (ValueError, TypeError):
        return False
    if peer.version != 4 or local.version != 4:
        return False
    net = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    return peer in net


def authorize(peer_host, host_subnet, bearer_token, device_store) -> str | None:
    """Decide the caller's role, or None to deny.

    * `peer_host`    — the request's peer IP (request.client.host).
    * `host_subnet`  — the central's own LAN IP; its /24 defines "same Wi-Fi".
    * `bearer_token` — the raw token from the Authorization header (already parsed).
    * `device_store` — a DeviceStore for token verification.

    Returns "root" for loopback, the device role for a valid in-subnet token, else
    None. An out-of-subnet peer is rejected *before* any token lookup, so a stolen
    token is useless off the LAN and never even touches the store.
    """
    if peer_host in _LOOPBACK_HOSTS:
        return "root"
    if not bearer_token:
        return None
    if not in_subnet(peer_host, host_subnet):
        return None
    device = device_store.verify(bearer_token)
    if device is None:
        return None
    return device.role


# Role hierarchy helpers used by the per-route gate in app.py.
def can_change_state(role: str | None) -> bool:
    """State-changing routes (sources/cameras/config/pairing) require central+."""
    return role in ("root", "central")


def can_view(role: str | None) -> bool:
    """Read-only GETs + /ws/live are open to any authenticated role."""
    return role in ("root", "central", "user")


# --------------------------------------------------------------------------- #
# Wavr Pass (Phase 1) -- scope taxonomy + resolution. Pure/no I/O, same rule as
# the rest of this module: app.py's require_scope() dependency (the only thing
# that ever raises an HTTPException) is a thin wrapper around `has_scope` below.
# --------------------------------------------------------------------------- #

# The grantable scopes (design spec §1). `mcp` was named in Phase 1 but stayed
# unenforced through P1-P3 (`/mcp` itself carried no scope check at all -- any
# authenticated in-subnet role, including `user`, reached every read tool).
# Phase-2A verify FIX 5 ENFORCES it for the first time, at `wavr.mcp_http`'s
# `_McpHttpGuard` (Gate 1.5): `mcp` was already a member of `central`'s default
# set below (and `agent`'s, further down) so this enforcement point is byte-
# identical for every already-paired central/agent; only `user` (whose default
# never carried `mcp`) is newly denied.
SCOPES = frozenset({
    "presence:read", "presence:write", "network:read", "camera:view",
    "control", "admin", "mcp",
})


class _AllScopes:
    """Sentinel scope-set for the loopback root: contains every scope, always.
    In practice `require_scope` (app.py) bypasses role "root" before it ever
    looks at a scope set, and `access_for` below returns `("root", None)` for
    the loopback path (there is no Device row to read an explicit grant from)
    -- so this sentinel is never actually consulted at request time. It exists
    so `DEFAULT_SCOPES["root"]` documents the "root = ALL, never scope-limited"
    rule from the design spec and so `effective_scopes("root", None)` is
    unit-testable as literally containing every scope."""

    def __contains__(self, _scope: object) -> bool:
        return True

    def __repr__(self) -> str:
        return "ALL_SCOPES"


ALL_SCOPES = _AllScopes()

# Role -> default scope set: the backward-compatibility lever. A device row
# with `scopes IS NULL` (every device paired before Wavr Pass, and every device
# paired since without an explicit grant) resolves to this. Chosen to reproduce
# today's can_view / can_change_state / require_central tiers EXACTLY -- see
# effective_scopes() and the design spec §2/§7.
DEFAULT_SCOPES: dict[str, frozenset[str] | "_AllScopes"] = {
    "root": ALL_SCOPES,
    "central": frozenset({
        "presence:read", "presence:write", "network:read", "camera:view",
        "control", "admin", "mcp",
    }),
    "user": frozenset({
        "presence:read", "presence:write", "network:read", "camera:view",
    }),
    # Phase 2A / B4: the AGENT principal type gets NOTHING but the `mcp` scope --
    # now ROUTE-ENFORCED (Phase-2A verify FIX 5, wavr.mcp_http's Gate 1.5), so
    # this is what actually lets an agent reach /mcp at all. An agent has no
    # presence/network/camera/control/admin route access at all --
    # every require_scope("...")-gated route (and require_authenticated's can_view/
    # require_local's can_change_state fallback, since "agent" is absent from both
    # role tuples in this module) denies it, so its ONLY reachable surface is /mcp
    # itself, further bounded there by its per-tool allow-list (effective_tool_
    # scopes below) -- "a bounded capability set, not the whole API" at BOTH layers.
    "agent": frozenset({"mcp"}),
}


def effective_scopes(role: str | None, explicit: frozenset[str] | None):
    """The scopes actually in force for `role`. `explicit` is a device's own
    stored `Device.scopes`: when it is not None (a P2 consent grant, possibly
    an intentionally narrow/empty one) it wins outright; otherwise (NULL --
    the Phase 1 default for every device) the caller gets `DEFAULT_SCOPES[role]`.
    An unrecognised role (should never happen -- every caller here already
    passed a VALID_ROLES member, "root", or None) fails closed to an empty set."""
    if explicit is not None:
        return explicit
    return DEFAULT_SCOPES.get(role, frozenset())


def has_scope(scopes, scope: str) -> bool:
    """True iff `scope` is granted. `scopes=None` (no role resolved at all --
    should never reach a scope check, since `access_for` denies first) fails
    CLOSED to False; `ALL_SCOPES` (root) always contains everything; a plain
    frozenset is a normal membership test."""
    if scopes is None:
        return False
    return scope in scopes


def access_for(peer_host, host_subnet, bearer_token, device_store):
    """One verify -> (role, scopes). Same access decision as `authorize` (left
    untouched above, and still used by its own existing callers/tests) but also
    resolves the caller's effective scopes in the SAME pass, so app.py's
    middleware needs only one call to set both `request.state.role` and
    `request.state.scopes`:

      * loopback peer                -> ("root", None) -- root is NEVER scope-
        limited; `require_scope` (app.py) bypasses it before consulting `scopes`
        at all, so this `None` is never dereferenced as a real scope set.
      * in-subnet peer + valid token -> (role, effective_scopes(role, device.scopes))
        -- a pre-Wavr-Pass token has `device.scopes is None`, so this resolves
        to `DEFAULT_SCOPES[role]`: identical to today's can_view/can_change_state
        tiers (design spec §7 backward-compat proof).
      * everything else              -> (None, None) -- deny BEFORE any scope
        talk: an off-subnet peer or an unknown/revoked token never reaches
        `device_store.verify` (off-subnet) or never gets a Device back (bad
        token), so it can't leak which scopes a valid token *would* have had.
    """
    if peer_host in _LOOPBACK_HOSTS:
        return "root", None
    if not bearer_token:
        return None, None
    if not in_subnet(peer_host, host_subnet):
        return None, None
    device = device_store.verify(bearer_token)
    if device is None:
        return None, None
    return device.role, effective_scopes(device.role, device.scopes)


# --------------------------------------------------------------------------- #
# Wavr Pass (Phase 2A / B4) -- the AGENT principal type + per-tool MCP scopes.
# A SEPARATE, finer-grained axis from SCOPES/DEFAULT_SCOPES above: those gate HTTP
# ROUTES (has_scope/require_scope); this axis gates individual MCP TOOL NAMES for a
# caller's `tools/call` over `/mcp` (enforced in mcp_http.py's `_McpHttpGuard`).
# Deliberately a different vocabulary (tool names like "call_ha_service", not route
# scopes like "control") so the two systems are never confused for each other, and
# deliberately additive: this axis restricts ONLY the new 'agent' role -- every
# pre-existing role (root/central/user) resolves it to None ("not restricted by
# this axis at all"), so their behaviour calling /mcp is untouched by this feature.
# --------------------------------------------------------------------------- #

# Every MCP tool name a caller could ever be granted -- mirrors the @server.tool()
# names registered in wavr.mcp.build_mcp_server. Kept here (not imported from
# wavr.mcp) so auth.py stays free of the [mcp] extra's import chain, the same
# discipline mcp.py itself uses for its own lazy SDK import.
MCP_TOOL_NAMES = frozenset({
    "list_rooms", "get_room_context", "get_house_map", "get_ha_entities",
    "get_network_inventory", "get_alerts", "query_occupancy_history",
    "get_house_status", "call_ha_service",
})

# Named tool-scope bundles an admin grants at pairing/promotion time (design brief
# B4): READ-ONLY is every read tool, EXCLUDING the one write tool -- actuation is
# opt-in, never in the default agent grant. ACTUATOR extends READ-ONLY with
# call_ha_service, for an operator who explicitly wants an agent able to act
# through Home Assistant -- that call is STILL separately gated by
# WAVR_MCP_CONTROL + the HA service allowlist + the sensitive-domain refusal
# inside call_ha_service itself (mcp.py); this bundle only decides whether the
# CALL is reachable AT ALL for this caller, one gate among several.
#
# AGENT_READ_TOOL_SCOPE stays available as the name an admin grants EXPLICITLY
# (via Device.tool_scopes) to widen an agent to every read tool -- it is no
# longer the DEFAULT (see AGENT_DEFAULT_TOOL_SCOPE / DEFAULT_AGENT_TOOL_SCOPES
# below, Phase-2A verify FIX 4).
AGENT_READ_TOOL_SCOPE = MCP_TOOL_NAMES - {"call_ha_service"}
AGENT_ACTUATOR_TOOL_SCOPE = MCP_TOOL_NAMES

# Phase-2A verify FIX 4 (MEDIUM, least-privilege default): the DEFAULT agent grant
# is COARSE, current-state-only -- current occupancy + the explainable room context
# + the composed house-status verdict. It deliberately EXCLUDES every read tool
# that leaks a household PII/tracking crown-jewel, even after the mcp.py-side
# minimization (FIX 1/2/3) narrows each one's OWN field set:
#   * query_occupancy_history -- a multi-week per-room timeline = "when is the
#     house empty" (even clamped to 24h by FIX 2, still a live-presence signal a
#     default cloud-relayed agent should not get for free).
#   * get_network_inventory   -- a LAN device census (even minimized by FIX 1).
#   * get_alerts               -- alert metadata (even minimized by FIX 3).
#   * get_ha_entities          -- HA's OWN entity `friendly_name`, which often
#     names a person or a specific device (see app.py's mcp-read Connectors
#     disclosure: "HA entity list incl. entity names (which may name people/
#     devices)") -- never minimized at all, so it stays out of the default too.
#   * get_house_map            -- Phase-2B verify re-threat (MEDIUM): even
#     mcp.py's own minimization (FIX C) only drops the floor/room/zone `name`
#     label -- it still ships room `id` (which ENCODES the room name in every
#     real house.json, e.g. "cozinha"/"quarto-1") plus the room's polygon
#     GEOMETRY, i.e. the annotated floor plan. A cloud Q&A assistant does not
#     need the floor plan to answer an occupancy question -- current occupancy
#     via list_rooms/get_room_context/get_house_status is enough -- so this
#     joins the crown-jewel set above, opt-in only.
# Each of these five requires an EXPLICIT admin Device.tool_scopes grant to
# reach -- exactly the same opt-in discipline call_ha_service already needs
# (AGENT_ACTUATOR_TOOL_SCOPE). This matches the decision that a default
# cloud-relayable agent gets current-occupancy only, not the history/inventory/
# alerts/HA-entities/floor-plan crown jewels; an operator who wants more grants
# AGENT_READ_TOOL_SCOPE (every read tool) or a hand-picked subset explicitly.
AGENT_DEFAULT_TOOL_SCOPE = frozenset({
    "list_rooms", "get_room_context", "get_house_status",
})

# Role -> default MCP tool-name allow-list (the tool-axis analog of DEFAULT_SCOPES).
# Only 'agent' is restricted by this axis at all -- root/central/user calling /mcp
# get every tool the TRANSPORT exposes (today: expose_control=False over HTTP, so
# call_ha_service is absent for them regardless of any grant -- unchanged by this
# feature) with no additional per-tool check, exactly as before. The sane default
# for a newly agent-scoped device is the COARSE set above (Phase-2A verify FIX 4):
# an operator must take a SEPARATE, explicit step (an explicit `Device.tool_scopes`
# grant) to widen an agent to AGENT_READ_TOOL_SCOPE or AGENT_ACTUATOR_TOOL_SCOPE.
DEFAULT_AGENT_TOOL_SCOPES: dict[str, frozenset[str]] = {
    "agent": AGENT_DEFAULT_TOOL_SCOPE,
}


def effective_tool_scopes(role: str | None,
                          explicit: frozenset[str] | None) -> frozenset[str] | None:
    """The MCP tool-name allow-list in force for `role`, mirroring effective_scopes's
    NULL-vs-explicit rule but for the tool-name axis. `explicit` is a device's own
    stored `Device.tool_scopes`: non-None (even empty -- deny-all) wins outright;
    NULL derives from `DEFAULT_AGENT_TOOL_SCOPES[role]`. Returns **None** for every
    role this axis doesn't apply to (i.e. anything other than 'agent') so a caller
    can tell "not restricted by this axis at all" (None) apart from "restricted to
    nothing" (an explicit empty frozenset) -- mcp_http.py's gate treats these two
    cases differently (None skips the per-tool check entirely; empty refuses every
    tool call)."""
    if role != "agent":
        return None
    if explicit is not None:
        return explicit
    return DEFAULT_AGENT_TOOL_SCOPES.get(role)


def tool_call_allowed(tool_scopes: frozenset[str] | None, tool_name: str) -> bool:
    """True iff `tool_name` may be called under `tool_scopes`. `tool_scopes is None`
    means "this axis doesn't restrict the caller" (root/central/user, and any
    future role this axis is never extended to) -> always True, i.e. unchanged
    pre-existing behaviour. A frozenset (even empty) is a REAL allow-list ->
    ordinary membership test, fails CLOSED for anything not explicitly listed."""
    if tool_scopes is None:
        return True
    return tool_name in tool_scopes


def access_for_scoped(peer_host, host_subnet, bearer_token, device_store):
    """`access_for`'s three-way sibling: resolves the caller's MCP tool-name
    allow-list (Phase 2A / B4) in the SAME one-verify pass, so app.py's middleware
    needs only one `device_store.verify()` to populate `request.state.role`,
    `.scopes`, AND `.tool_scopes`. Returns `(role, scopes, tool_scopes)` -- the
    first two elements are computed IDENTICALLY to `access_for` for the same
    inputs (byte-for-byte: same loopback/off-subnet/unknown-token/revoked-token
    short circuits, same `effective_scopes` call); only the third element is new.
    `tool_scopes` is **None** for every role except 'agent' (see
    `effective_tool_scopes`), so root/central/user get `(role, scopes, None)` --
    identical to how this axis simply didn't exist for them, the additive-only
    proof for this feature.

    `access_for` ITSELF IS UNCHANGED and remains the two-tuple its own tests
    exercise (test_wavr_pass_scopes.py) -- this mirrors the exact precedent
    `access_for` set over `authorize()`: a new function supersedes the old one at
    app.py's ONE call site while the superseded function stays put, untouched,
    for its own callers/tests."""
    if peer_host in _LOOPBACK_HOSTS:
        return "root", None, None
    if not bearer_token:
        return None, None, None
    if not in_subnet(peer_host, host_subnet):
        return None, None, None
    device = device_store.verify(bearer_token)
    if device is None:
        return None, None, None
    role = device.role
    scopes = effective_scopes(role, device.scopes)
    tool_scopes = effective_tool_scopes(role, device.tool_scopes)
    return role, scopes, tool_scopes
