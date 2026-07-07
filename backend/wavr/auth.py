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

# The grantable scopes (design spec §1). `mcp` is named now but only ENFORCED
# from Phase 4 (`/mcp` stays unscoped through P1-P3) -- it's already a member of
# `central`'s default set below so a future P4 enforcement point is byte-
# identical for every already-paired central.
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
