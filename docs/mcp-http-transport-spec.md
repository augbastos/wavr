# Wavr MCP over streamable-HTTP — Design Spec (Slice 1: secure read transport)

> Status: **approved for Slice 1** (2026-07-06, owner). Design by security-architect,
> grounded in the actual Wavr auth/pairing/MCP code. Becomes ADR-0008 on merge.
> Extends [ADR-0005 MCP control boundary] + [ADR-0006 authenticated LAN access].

## Goal

Make the Wavr MCP a **universal, cross-platform integration**: any MCP client (another
machine on the home LAN, a phone, a local-LLM/agentic-OS host) can add Wavr and get live
home presence — while today it is stdio-only. Reference-quality, secure by design.

## Golden invariant (do not slip)

Today Wavr is "secure by construction, no open port" (stdio-only; app binds 127.0.0.1
unless `WAVR_MULTIDEVICE` is on). This capability **deliberately opens an inbound network
listener**. It is only defensible if ALL THREE hold: (a) **LAN-only, never internet**;
(b) reuses the existing **paired-token + cert-pin** gate; (c) **default-OFF** behind the
Connectors surface. If any slips, it becomes "a network attacker controls the home."

## Architecture (Slice 1)

- **Mount FastMCP's `streamable_http_app()` INSIDE the main FastAPI app** at `/mcp`
  (constructed with `stateless_http=True`), served by the existing `serve.py` uvicorn +
  self-signed TLS. It therefore INHERITS `loopback_or_authed` (`app.py`), `TrustedHostMiddleware`,
  the TLS cert, and `DeviceStore`. **Do NOT run a second FastMCP uvicorn on its own port** —
  that would bypass the middleware = "inventing auth" = rejected.
- **Provider:** in-process `FusionStateProvider` (live engine), not the loopback bridge.
- **stdio path stays exactly as-is** (`mcp_serve.py`) — local, full gated toolset.
- **READ-ONLY over HTTP:** the HTTP mount registers ONLY the 4 read tools
  (`list_rooms`, `get_room_context`, `get_house_map`, `get_ha_entities`). `call_ha_service`
  is **NOT registered** on the HTTP transport (not merely disabled — absent from `list_tools`).
  Reason: `mcp.py`'s control gate is process-global, not per-caller — there is no "read-only
  client" vs "control client" distinction today, so exposing control over HTTP would grant it
  to every paired token equally. `get_room_context` already strips vitals/targets/identities.
- **Default-OFF Connectors toggle:** new inbound built-in `mcp-http`,
  `enforcement="registry-overlay"` (real per-request kill-switch, no restart). Mount serves
  ONLY when `multidevice` ON (TLS present) AND `mcp-http` enabled.
- **Stateless (RC 2026-07-28):** Wavr auth is already per-request stateless (`authorize()`
  re-verifies the token every call); `stateless_http=True` matches the RC core.

## Security gate (every `/mcp` request)

- **Cert-pin (transport):** client pins the self-signed cert SHA-256 (same as mobile;
  `/api/pair-code` returns `cert_fingerprint`, verified out-of-band).
- **Pairing (identity):** `Authorization: Bearer <paired-token>` minted by `PairingManager`
  → `DeviceStore` (hashed, revocable). Out-of-subnet peers are **403'd before token lookup**
  (`auth.py`). No token → 403 at middleware, before FastMCP dispatch.

## Must-build (Slice 1)

1. **SPIKE FIRST (de-risk):** prove a mounted `streamable_http_app()` sub-app actually passes
   through the app's `@app.middleware("http")` — an UNPAIRED request to `/mcp` MUST get 403.
   If FastMCP's sub-mount bypasses Starlette middleware, adjust the design before building.
   (SDK is `mcp 1.27.1` — has `streamable_http_app()`/`stateless_http`; bump `pyproject.toml`
   `[mcp]` pin from `>=1.2` to `>=1.27` to be explicit.)
2. In-app mount at `/mcp` (`app.py`/`serve.py`), read-only tool set (omit `call_ha_service`).
3. Confirm `/mcp` is NOT in the unauth static/pair allowlist → always hits `authorize()`.
4. **Origin-header validation** on `/mcp` (MCP streamable-HTTP spec DNS-rebind requirement;
   `TrustedHostMiddleware` covers Host, not Origin).
5. New `mcp-http` connector (builtin, inbound, registry-overlay), default-OFF.
6. `stateless_http=True`; HTTP-endpoint rate-limiting (stdio had none, was local-by-construction).
7. **Negative-path tests:** unpaired → 403; out-of-subnet + valid token → 403; revoked token →
   fail; `call_ha_service` ABSENT from HTTP `list_tools`; loopback stdio still exposes full set.

## Acceptance gate (before merge — public AGPL repo, new inbound surface)

- `offensive-security-red-teamer`: unpaired / out-of-subnet / wrong-cert drills all fail-closed;
  `call_ha_service` unreachable over HTTP.
- `appsec-code-reviewer`: middleware coverage of `/mcp`, tool-set omission, no secret committed.
- `privacy-compliance-license-auditor`: new inbound listener disclosed in `/api/status.features`;
  no cert/key/token/`wavr.db*` committed (they live in `~/.wavr/`).

## Packaging (Slice 2, after the secure server is complete)

- `uvx wavr-mcp` / `pipx` launches the **stdio** bridge (full gated toolset) — best-tested
  `claude mcp add` path, unchanged.
- Networked client adds HTTP explicitly: `claude mcp add --transport http https://<ip>:<port>/mcp`
  + pinned-cert trust + paired-token `Authorization: Bearer`.
- Docs state honestly: **stdio = local, full (gated) control; HTTP = LAN-paired, read-only.**

---

## DEFERRED — future sub-projects (owner: "guardar p/ quando for viável, analisar caso a caso")

### #2 — House CONTROL over the network (remote actuation)
Let a remote paired client call `call_ha_service` (actuate the home), not just read.
**Why deferred / what it needs:** `mcp.py`'s control gate is process-global with no per-caller
authz — must build **per-device role enforcement** (only a `central`-role token actuates; `user`
tokens never), a **fresh security re-audit per ADR-0005 §Consequences**, and UI surfacing. It is a
SEPARATE opt-in (a second flag, never folded into the read toggle). Higher risk — do after Slice 1.

### #3 — True INTERNET reach (any platform anywhere, not just the home LAN)
A web agentic OS / LLM on a different network connects to the home Wavr.
**Why deferred / what it needs:** today `authorize()` rejects out-of-subnet by design. Internet
exposure needs a **relay/tunnel + mutual auth + explicit UI-surfaced internet opt-in** — a much
larger risk surface (the whole point of Wavr is local-first, zero cloud egress). Design as its own
sub-project with its own threat model before any build.

## Residual risks accepted for Slice 1
- A stolen/abused paired token ON the LAN can READ the house (occupancy, house map, HA entity
  names — NOT vitals/targets/identities). Mitigation = revoke + short pairing windows + kill-switch.
- Flat-LAN /24 trust; TOFU pinning depends on the operator comparing the fingerprint at first pair.
- HTTP is read-only by design — an operator may expect remote actuation and be surprised. Documented.
