# Connecting to the Wavr MCP server

Wavr exposes its LOCAL, derived presence state over the Model Context Protocol (MCP)
so any MCP host — Claude Code, or a future Agentic OS — can *read what the house
currently senses*.

## Two transports, on purpose

Wavr speaks MCP over **two** transports with **deliberately different capability**:

| Transport | Reach | Tools | Auth |
|-----------|-------|-------|------|
| **stdio** | Local, same box only | **Full, gated** (4 read + `call_ha_service`, DEFAULT-OFF) | Host spawns the process; loopback-only by construction |
| **HTTP** (`/mcp`) | LAN, paired peers | **Read-only** (the 4 read tools; `call_ha_service` is absent, not just disabled) | Paired token (`Authorization: Bearer`) + pinned self-signed cert |

The honest one-liner: **stdio = local, full (gated) control; HTTP = LAN-paired, read-only.**
Control (`call_ha_service`) is reachable ONLY over stdio and ONLY when explicitly enabled —
it is not registered at all on the HTTP transport (ADR-0008 Slice 1). Remote actuation over
the network is a separate, deferred opt-in (spec `docs/mcp-http-transport-spec.md` §#2).

Both transports read the SAME live state from the already-running Wavr app — neither starts
a second sensing process, and neither ever reaches off the local box except to the user's own
Home Assistant on the LAN.

---

## Transport 1 — stdio (local, full gated toolset)

`python -m wavr.mcp_serve` (entrypoint: `backend/wavr/mcp_serve.py`) is a **stdio** MCP server.
It reads the DERIVED room state from the **already-running Wavr app** over loopback HTTP
(`http://127.0.0.1:<WAVR_PORT>`, same box) and reads/optionally-controls the user's **own**
Home Assistant on the LAN. The host spawns the process and talks over its pipes; nothing binds
a public interface.

### Prerequisites

1. The Wavr app is running on `:8000` (or your `WAVR_PORT`). If it is down, tool calls surface
   a clear transport error rather than fabricating an empty house.
2. The `[mcp]` extra is installed (`mcp>=1.27`). It is a **lazy** dependency: `import wavr.mcp`
   never needs it — only running the server does.
   Install: `C:\IA\wavr\.venv\Scripts\python -m pip install -e "backend[mcp]"`

### Connect Claude Code (editable install / local dev)

The `wavr-mcp` console script is registered by the editable install, so no `-m` or `PYTHONPATH`
juggling is needed:

```
claude mcp add wavr -- wavr-mcp
```

Or without an editable install, spawn the module directly (PYTHONPATH lets it resolve from any cwd):

```
claude mcp add wavr --env PYTHONPATH=C:\IA\wavr\backend -- C:\IA\wavr\.venv\Scripts\python.exe -m wavr.mcp_serve
```

Then in a Claude Code session, `/mcp` lists the `wavr` server and its tools. Ask e.g.
"which rooms does Wavr sense as occupied?" and it will call `list_rooms`.

To control Home Assistant devices (default OFF), also set `--env WAVR_MCP_CONTROL=1` (and
configure `WAVR_HA_URL` / `WAVR_HA_TOKEN` + `WAVR_HA_ALLOWED_SERVICES`). See **Guarantees** below —
sensitive domains are refused regardless.

### Connect with uvx / pipx (no clone, no editable install)

The console script + stdio bridge live on the public repo's `master`, so an MCP host can spawn
`wavr-mcp` straight from git with **no local checkout** — uv builds it in an ephemeral env:

```
uvx --from "wavr[mcp] @ git+https://github.com/augbastos/wavr.git#subdirectory=backend" wavr-mcp
```

As a persistent tool via pipx:

```
pipx install "wavr[mcp] @ git+https://github.com/augbastos/wavr.git#subdirectory=backend"
wavr-mcp        # now on PATH
```

Wire it into Claude Code:

```
claude mcp add wavr -- uvx --from "wavr[mcp] @ git+https://github.com/augbastos/wavr.git#subdirectory=backend" wavr-mcp
```

Caveats — this is still the **loopback, same-box** bridge:
- It reads the running app on `127.0.0.1:<WAVR_PORT>`, so it must run **on the same machine** as
  the Wavr app, which must be up. It reaches nothing off the box (except the user's LAN HA).
- `uvx` pulls the default branch (`master`). Pin a ref with `@<branch-or-tag>` if you need one.
- A bare `uvx wavr-mcp` (PyPI) is **not** wired — Wavr is not published to PyPI yet. Use the
  `--from git+…` form above.

### Optional same-box target override

`WAVR_MCP_TARGET` may point the loopback bridge at a different **same-box** port/scheme (e.g.
`http://127.0.0.1:9000`). It is **fail-closed to loopback**: a non-loopback host is refused
outright, so the same-box token can never be sent off-box.

---

## Transport 2 — HTTP (LAN-paired, read-only)

> **Availability:** the in-app `/mcp` HTTP mount (ADR-0008 Slice 1) currently lives on the
> `feat/mcp-http-transport` branch — it is **not on `master` yet**. The steps below work once
> that branch is running; the stdio path above works against `master` today.

The HTTP transport mounts a read-only MCP endpoint at `/mcp` **inside** the main Wavr app (same
uvicorn, same self-signed TLS, same auth middleware). It is **default-OFF** and only serves when
LAN mode is on AND the `mcp-http` connector is enabled.

### Prerequisites

1. **LAN mode on:** start the app with `WAVR_MULTIDEVICE=1`, which serves HTTPS/WSS on your LAN
   interface with a self-signed cert (auto-generated at `~/.wavr/cert.pem`):
   ```
   set WAVR_MULTIDEVICE=1
   C:\IA\wavr\.venv\Scripts\python.exe -m wavr.serve
   ```
   Without `WAVR_MULTIDEVICE`, the app binds `127.0.0.1` only and `/mcp` is not mounted (404).
2. The `[mcp]` and `[tls]` extras installed (`pip install -e "backend[mcp,tls]"`).

### Step 1 — enable the connector (default-OFF kill-switch)

`/mcp` returns **503 until you enable it**. As an operator (loopback root / a `central` peer):

```
POST /api/connectors/mcp-http/enable   { "enabled": true }
```

This is a live, per-request kill-switch — flip it back to `false` to cut the transport with no
restart. Presence + state of the toggle are disclosed honestly in `GET /api/status` (features)
and `GET /api/connectors`.

### Step 2 — pair the client (identity + MitM-checked cert)

1. **Operator** mints a one-time pairing code (gated to loopback/central), which also returns the
   cert fingerprint for out-of-band verification:
   ```
   POST /api/pair-code   { "role": "user" }
   -> { "code": "<one-time, ~2-min>", "cert_fingerprint": "<sha256>" }
   ```
2. **Client** redeems the code (reachable by an in-subnet peer without a token — that is the point
   of pairing, bounded by the one-time code):
   ```
   POST /api/pair   { "code": "<code>", "device_name": "my-agent" }
   -> { "device_id": "...", "token": "<256-bit, shown once>" }
   ```
   Store the `token` — it is the `Authorization: Bearer` credential and is never shown again
   (it is hashed at rest, revocable).
3. **Verify the cert out-of-band:** the fingerprint the client sees on the TLS cert MUST equal the
   `cert_fingerprint` from step 1. A pairing-time MitM presents a different self-signed cert →
   different fingerprint → mismatch → stop. Trust that exact cert (`~/.wavr/cert.pem`) in your MCP
   host (e.g. `NODE_EXTRA_CA_CERTS=~/.wavr/cert.pem` for a Node-based host); do **not** disable
   TLS verification.

### Step 3 — add the HTTP server to your MCP host

```
claude mcp add wavr-lan https://<wavr-lan-ip>:<WAVR_PORT>/mcp \
  --transport http \
  --header "Authorization: Bearer <token>"
```

The client must be **on the same /24 subnet** — out-of-subnet peers are 403'd before the token is
even looked up. `/mcp` also enforces an Origin allowlist (DNS-rebind defence) and per-peer
rate-limiting. Over HTTP you get the **4 read tools only**; `call_ha_service` is absent from
`list_tools`.

---

## Tools

| Tool | Kind | stdio | HTTP | Description |
|------|------|:---:|:---:|-------------|
| `list_rooms` | read | ✓ | ✓ | Every room Wavr senses, with `occupied` + `confidence`. |
| `get_room_context` | read | ✓ | ✓ | Full state for one room incl. explainable `sources` + `explanation`. **Strips `vitals`, `targets`, `identities`** — no per-person biometric/positional data. |
| `get_house_map` | read | ✓ | ✓ | The house map / floor plan (room geometry only). |
| `get_ha_entities` | read | ✓ | ✓ | Home Assistant's own entities (`entity_id`/`state`/`friendly_name`/`domain`). `[]` when HA is unconfigured. **Note:** HA entity names may name people/devices (e.g. `person.*`, `device_tracker.*`). |
| `call_ha_service` | control | ✓ (gated, DEFAULT-OFF) | ✗ (never registered) | Ask HA to run one service on one entity. |

## Guarantees

- **Read-only by construction (both transports).** The four read tools never mutate state, toggle/
  create a source, install anything, or reach off the local box. `call_ha_service` is the only
  mutating tool and is stdio-only + default-OFF.
- **Local-only.** All traffic is loopback (to the running app) or LAN (to the user's own HA, or —
  over the HTTP transport — from a paired LAN peer). No cloud, no new external egress.
- **Privacy survives the bridge.** `/api/state` carries full RoomState (incl. vitals/targets) over
  loopback into the local bridge only; `get_room_context` strips `vitals`/`targets`/`identities` at
  the tool layer before anything reaches the agent, and `list_rooms` only surfaces
  room/occupied/confidence.
- **Control is DEFAULT-OFF and stdio-only.** `call_ha_service` is inert unless `WAVR_MCP_CONTROL`
  is on, and is never exposed over HTTP. Even enabled (stdio), it passes a gate chain:
  control-flag → single concrete `domain.object_id` (no `all`/wildcard/list) → sensitive-domain
  refusal (camera / media_player / lock / alarm_control_panel / cover / valve / siren / lawn_mower,
  any camera/mic-hinting name, and opaque scene/script/automation/group are refused OUTRIGHT even if
  allowlisted — consent is not wired yet) → allowlist (`WAVR_HA_ALLOWED_SERVICES`) → HA delegation.
  The camera boot-OFF invariant (ADR-0002) holds: the MCP can never turn a camera on.
- **Device blocking is PERMANENTLY excluded from MCP.** ARP-block / spoof / deauth is an active
  LAN-attack primitive and is never registered as an MCP tool. It ships only behind a human-clicked,
  triple-gated dashboard action (`POST /api/block`).
- **HTTP is a default-OFF, revocable, LAN-only surface.** It only serves under `WAVR_MULTIDEVICE`
  with the `mcp-http` connector enabled; every request is gated by paired-token auth + subnet check
  + Origin + rate-limit; the kill-switch cuts it live.

## Notes

- **Data is live only while the app is running.** The MCP server shares the running app's in-memory
  state; it holds no state of its own. Start Wavr first.
- Full design rationale: ADR-0005 (control boundary), ADR-0006 (authenticated LAN access), and
  `docs/mcp-http-transport-spec.md` (ADR-0008, the HTTP transport).
