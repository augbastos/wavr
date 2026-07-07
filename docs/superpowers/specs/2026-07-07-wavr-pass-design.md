# Wavr Pass — Local Sovereign Authorization Framework (Design)

Status: DESIGN — APPROVED for Phase-1 implementation (security-architect, 2026-07-07), with
conditions in §Verdict. Builds on ADR-0006 (authenticated LAN access), ADR-0002 (RAM-only
privacy), ADR-0004/0005/0008. Phase 1 is strictly backward-compatible and additive; Phases
2-4 are design-later sketches.

## 0. What this is
Wavr Pass is **local OAuth for the physical space**, zero cloud:

| OAuth role | Wavr Pass |
|---|---|
| Authorization Server | the **Core** (loopback root central) |
| Resource Server | the **same Core** |
| Resource Owner (consents) | the **admin** (loopback root / a `central`) |
| Client | companions, integrations, MCP clients, other Cores |

The Core is BOTH AS and RS — no external IdP, no broker, no internet token endpoint. Grants
are peer-to-peer on the LAN, revocable instantly, never leave the box. NOT federation across
LANs, NOT a cloud login, NOT internet exposure.

## 1. Scope taxonomy (derived from real routes)
The existing gates already encode three tiers — `can_view` (root/central/user),
`can_change_state` (root/central), loopback-root-only (block). Wavr Pass names them:

| Scope | Capability | In `user` default? |
|---|---|---|
| `presence:read` | Read RoomState + map to render it (GET /api/state, /history, /house, WS /ws/live) | YES |
| `presence:write` | Self-register caller's OWN device (MAC server-derived) (POST/DELETE /api/presence/register-companion) | YES |
| `network:read` | LAN inventory + first/last-seen report + IP-drift suggestions | YES |
| `camera:view` | Camera CONFIG/geometry/PTZ-metadata — never a frame | YES |
| `control` | Every state-change + central-only LIVE-position reads (calib-spots/calib-sample expose feet pixel) | NO |
| `admin` | Manage other devices + PII/egress surfaces (devices/role, pair-code, core/pin, identity, connectors) | NO |
| `mcp` | MCP client against `/mcp` — defined now, enforced P4 | NO (IN `central` default) |

**Unscoped baseline (Phase 1)** — non-sensitive bool/liveness reads, no `require_scope`:
/healthz, GET /api/status, /api/system, /api/speedtest/info, /api/core/pin/status,
POST /api/core/pin/verify.

**Outside the scope system entirely** — POST/GET /api/block stay **loopback-root-only** via
`require_root`. No scope grants blocking; a LAN `central` can never wield it (preserves the
A5.2 red-team mitigation).

## 2. Attaching scopes to a token
`devices.py` — add one nullable column: `scopes TEXT` (space-delimited; NULL => derive from
role). `Device` gains `scopes: frozenset[str] | None`.

Role → default-scope map (the backward-compat lever, in `auth.py`):
- `root` → ALL (sentinel — loopback root NEVER scope-limited)
- `central` → {presence:read, presence:write, network:read, camera:view, control, admin, mcp}
- `user` → {presence:read, presence:write, network:read, camera:view}

`effective_scopes(role, explicit) = explicit if explicit is not None else DEFAULT_SCOPES[role]`.
Defaults reproduce the existing can_view/can_change_state/require_central tiers exactly.

## 3. Scope-check dependency + composition
`access_for(peer, subnet, token, store)` in `auth.py` (one verify, returns role + scopes):
loopback → ("root", None); in-subnet+valid token → (role, effective_scopes(role, device.scopes));
else → (None, None) (deny BEFORE any scope talk). `authorize()` left untouched. Middleware sets
`request.state.role` AND `request.state.scopes`. Still requires in_subnet + valid non-revoked
token before scopes appear — a stolen off-LAN token never reaches the scope layer.

`require_scope(scope)` in `app.py`: root bypasses; else 403 if scope not in request.state.scopes.

Two load-bearing rules: (1) loopback root bypasses `require_scope` (still needs X-Wavr-Local
CSRF + WAVR_LOCAL_TOKEN via unchanged `require_local`); (2) **`require_scope` is ADDED, never
SUBSTITUTED** — Phase 1 never removes an existing require_local/require_central/require_root/
require_authenticated gate. A caller passes BOTH. Fail-safe: even a mis-mapped scope cannot
widen access because the original role gate still denies.

`/ws/live` and `/mcp` deliberately unscoped in P1 (presence:read is in both defaults; the WS
ticket/revocation path is delicate → P3; adding an `mcp` scope `user` lacks would NARROW =
a break → enforce at P4 when `central` already carries `mcp`).

## 4. Migration (additive)
`DeviceStore.__init__` after CREATE TABLE: PRAGMA table_info check, then
`ALTER TABLE devices ADD COLUMN scopes TEXT` (nullable, DEFAULT NULL) if absent. No backfill.

A pre-Wavr-Pass token's first request after upgrade: bearer → access_for → verify finds the row
(token_hash unchanged), not revoked → scopes NULL → effective_scopes(role, None) = role default
→ require_scope resolves against the default → **identical** allow/deny. No re-pair, no rotation,
no restart, no user action.

## 5. Roadmap (design-later)
- **P2 Consent** — RFC 8628 Device Authorization Grant, LAN-local: device POSTs requested_scopes
  + name → PENDING → Core-panel consent screen (Approve / Approve-with-fewer / Deny) → mint
  explicit scopes. Keep old role-code path as quick-grant during transition. Route to
  surveillance-threat-modeler + offensive-security-red-teamer (self-escalation, front-running).
- **P3 Locally-signed tokens** — Core signs {sub, scopes, exp, iat, jti} with its OWN on-box key
  (Ed25519), short access-TTL + refresh. Instant revocation preserved via a jti denylist. Key
  never leaves the LAN. Dual-format verify during transition. Route to security-architect + appsec.
- **P4 MCP / aOS / other-Core as first-class clients** — the Connectors screen is the consent
  surface (single door); external clients pair via P2 requesting scopes; `mcp` gates `/mcp`.
  Connectors kill-switch composes as a monotone (reduce-only) overlay. Cross-Core LAN-local;
  cross-internet out of scope.

## 6. Files Phase 1 touches (exact blast radius) — THREE files
1. `backend/wavr/devices.py` — nullable `scopes` column + PRAGMA-guarded migration; `Device.scopes`;
   parse/serialize; `add(scopes=None)` (default preserves today's callers).
2. `backend/wavr/auth.py` — SCOPES, DEFAULT_SCOPES, effective_scopes(), has_scope(), access_for().
   `authorize()` left in place.
3. `backend/wavr/app.py` — middleware sets request.state.scopes; add require_scope; wire the map.
   Router-mounted routes get scopes via EXISTING seams (no edits to the four router files):
   inventory `include_router(..., dependencies=[Depends(require_scope("network:read"))])` + PUT
   `name_deps=[require_local, require_scope("control")]`; identity/connectors router
   `dependencies=[require_central, require_scope("admin")]` + `write_deps=[require_local,
   require_scope("control")]`; devices router `dependencies=[require_central,
   require_scope("admin")]`.

NOT touched: pairing.py, api_devices.py, api_inventory.py, api_identity.py, api_connectors.py,
frontend, serve.py, TLS/cert path.

New tests (`backend/tests`): NULL-scopes→role-default for central+user (executable BC proof);
require_scope denies a missing scope; root bypasses require_scope; migration idempotency; full
user allow/deny parity matrix.

## 7. Backward-compat proof (Phase 1)
1. Existing rows get scopes=NULL after additive ALTER TABLE; token_hash unchanged.
2. NULL → DEFAULT_SCOPES[role], chosen equal to the existing tiers.
3. Root bypasses require_scope; CSRF + WAVR_LOCAL_TOKEN unchanged.
4. Additive-only: never removes a role gate → caller passes gate AND scope; a mis-map can't widen.
5-8. user/central parity route-by-route; pairing unchanged (NULL→default); WAVR_MULTIDEVICE off →
   no DeviceStore → strict loopback, byte-identical.

## 8. Invariants preserved
Loopback-only default; subnet+token gate (before scopes); local TLS; instant revocation; ZERO
cloud egress (Core is AS+RS, on-box signing key); admin sovereignty (root never scoped; P2
consent; instant revoke; monotone kill-switch); frames never persisted (camera:view = config/
geometry only, never images — ADR-0002).

## Verdict (security-architect)
APPROVE for Phase-1 with conditions before merge: (1) the executable parity test must pass as the
backward-compat proof; (2) appsec confirms access_for fails-closed off-subnet and no control/admin
route lost its original gate; (3) /api/block verified to carry NO scope mapping.

## Next
- Phase-1 implementation → python-backend-engineer (three files + parity tests).
- Phase-1 sign-off → appsec-code-reviewer (require_scope composition, migration idempotency,
  off-subnet fail-closed, block-stays-root-only).
