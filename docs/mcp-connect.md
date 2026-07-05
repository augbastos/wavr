# Connecting to the Wavr MCP server

Wavr exposes its LOCAL, derived presence state over the Model Context Protocol (MCP)
so any MCP host — Claude Code, or a future Agentic OS — can *read what the house
currently senses*. This doc covers how to launch the server and how to connect a host.

## What it is

`python -m wavr.mcp_serve` (entrypoint: `backend/wavr/mcp_serve.py`) is a **stdio** MCP
server. It does NOT start a second sensing process. It reads the DERIVED room state from
the **already-running Wavr app** over loopback HTTP (`http://127.0.0.1:<WAVR_PORT>`, same
box) and reads/optionally-controls the user's **own** Home Assistant on the LAN.

- Transport: **stdio** (the host spawns the process and talks over its pipes).
- Nothing binds a public interface. The only outbound calls are a loopback GET to the
  running app and, if HA is configured, a LAN call to the user's own Home Assistant.

## Prerequisites

1. The Wavr app is running on `:8000` (or your `WAVR_PORT`). The MCP server reads live
   state from it; if the app is down, tool calls surface a clear transport error rather
   than fabricating an empty house.
2. The `[mcp]` extra is installed in the venv (`mcp>=1.2`). It is a **lazy** dependency:
   `import wavr.mcp` never needs it — only actually running the server does.
   Install: `C:\IA\wavr\.venv\Scripts\python -m pip install -e "backend[mcp]"`

## Launch it standalone (sanity check)

```
set PYTHONPATH=C:\IA\wavr\backend
C:\IA\wavr\.venv\Scripts\python.exe -m wavr.mcp_serve
```

It will sit on stdio waiting for an MCP host (no output is normal). Ctrl-C to stop.

## Connect Claude Code

Copy-paste (Augusto's venv python; PYTHONPATH lets the module resolve from any cwd, so no
`cwd` juggling and no editable install required):

```
claude mcp add wavr --env PYTHONPATH=C:\IA\wavr\backend -- C:\IA\wavr\.venv\Scripts\python.exe -m wavr.mcp_serve
```

Then in a Claude Code session, `/mcp` lists the `wavr` server and its five tools. Ask
e.g. "which rooms does Wavr sense as occupied?" and it will call `list_rooms`.

To control Home Assistant devices (default OFF), also set `--env WAVR_MCP_CONTROL=1`
(and configure `WAVR_HA_URL` / `WAVR_HA_TOKEN` + `WAVR_HA_ALLOWED_SERVICES`). See gating
below — sensitive domains are refused regardless.

## Connect a future Agentic OS (or any MCP host)

Any host that speaks MCP stdio connects the same way — spawn:

```
command : C:\IA\wavr\.venv\Scripts\python.exe
args    : ["-m", "wavr.mcp_serve"]
env     : { "PYTHONPATH": "C:\\IA\\wavr\\backend" }
```

If you `pip install -e "backend[mcp]"`, a console script `wavr-mcp` is also registered,
so the host can spawn `wavr-mcp` directly (still stdio, still loopback-only).

Optional override: `WAVR_MCP_TARGET` may point the loopback bridge at a different
same-box port/scheme (e.g. `http://127.0.0.1:9000`). It is **fail-closed to loopback**:
a non-loopback host is refused outright, so the same-box token can never be sent off-box.

## Tools

| Tool | Kind | Description |
|------|------|-------------|
| `list_rooms` | read | Every room Wavr senses, with `occupied` + `confidence`. |
| `get_room_context` | read | Full state for one room incl. explainable `sources` + `explanation`. **Strips `vitals` and `targets`** (audit CRITICAL-1) — no per-person biometric/positional data. |
| `get_house_map` | read | The house map / floor plan (room geometry only). |
| `get_ha_entities` | read | Home Assistant's own entities (`entity_id`/`state`/`friendly_name`/`domain`). `[]` when HA is unconfigured. |
| `call_ha_service` | control | Ask HA to run one service on one entity. **DEFAULT-OFF and heavily gated** (see below). |

## Guarantees

- **Read-only by construction.** The four read tools never mutate state, toggle/create a
  source, install anything, or reach off the local box.
- **Local-only.** All traffic is loopback (to the running app) or LAN (to the user's own
  HA). No cloud, no new external egress. The MCP process never binds a public interface;
  it speaks stdio to the host that spawned it.
- **Privacy survives the bridge.** `/api/state` carries full RoomState (incl.
  vitals/targets) over loopback into the local bridge only; `get_room_context` strips
  vitals/targets at the tool layer before anything reaches the agent, and `list_rooms`
  only surfaces room/occupied/confidence.
- **Control is DEFAULT-OFF.** `call_ha_service` is inert unless `WAVR_MCP_CONTROL` is on.
  Even enabled, it passes a gate chain: control-flag → single concrete `domain.object_id`
  (no `all`/wildcard/list) → sensitive-domain refusal (camera / media_player / lock /
  alarm_control_panel / cover / valve / siren / lawn_mower, any camera/mic-hinting name,
  and opaque scene/script/automation/group are refused OUTRIGHT even if allowlisted —
  consent is not wired yet) → allowlist (`WAVR_HA_ALLOWED_SERVICES`) → HA delegation.
  Wavr never drives a device directly; HA executes. The camera boot-OFF invariant
  (ADR-0002) holds: the MCP can never turn a camera on.
- **Device blocking is PERMANENTLY excluded from MCP (A5.2).** ARP-block / spoof / deauth
  is an active LAN-attack primitive and is never registered as an MCP tool. It ships only
  behind a human-clicked, triple-gated dashboard action (`POST /api/block`).

## Notes

- **Data is live only while `:8000` is running.** The MCP server shares the running app's
  in-memory state via a loopback GET; it holds no state of its own. Start Wavr first.
- Full design rationale: ADR-0005 (`docs/adr/`).
</content>
