# Design note — Wavr as a presence brain *on top of* Home Assistant

> **Status:** validated as an idea, **not started**. This is a design note, not a plan
> with dates. Nothing below is implemented.

## The lever

Home Assistant already integrates thousands of devices — Zigbee, Z-Wave, cameras, locks,
motion and door sensors, `device_tracker`, energy. Wavr **already reads all of it** through
`ha_client.py` / `ha_import` — it is a client of the user's own HA instance, local-only.

The gap is narrow and specific: **reading** HA entities is not **fusing** them. Today Wavr's
fusion runs on its own signals (mmWave, BLE, network, camera). HA entities are imported and
displayed, but they do not contribute confidence to a room's presence.

Close that gap and Wavr supports presence from everything HA supports, without rebuilding a
single device integration. That is the whole argument.

## What to fuse, strongest signal first

| HA entity | Becomes which presence signal | Weight / note |
|---|---|---|
| `binary_sensor`, device_class **motion / occupancy / presence** | strong presence in the room | high; decays fast — motion is instantaneous, not a state |
| `device_tracker` / `person` (home/away) | presence tied to an **identity** | high for the house; identity handling applies (see invariants) |
| `binary_sensor`, **door / window** | transition and context (entered / left) | medium; an event signal, not a state one |
| `media_player` (playing) | activity implies somebody in the room | medium |
| `light` / `switch` on | weak occupancy — somebody turned it on | low; decays slowly |

Each enters `RoomState` with a **confidence weight plus decay**, reusing the same
`colorFor(pct)` / consensus maths the presence ring, the map tint and the room rail already
share. No second scale, no second colour vocabulary.

## Where to start — the de-risked first slice

**`binary_sensor` motion/occupancy → presence**, end to end, for exactly one signal type:

    HA entity → map to a Wavr room → weight in fusion → RoomState → visible on ring and rail

That proves the pattern against the real pipeline. The other rows in the table then become a
question of weights, not of architecture.

## The room bridge (HA area → Wavr room)

HA entities carry an **area**; Wavr has rooms in the `housemap`. Something has to map one to
the other. A name-to-name map in config is enough to start; anything geometric is a later
question and does not block the first slice.

## Invariants this must not break

- **Local-only, zero egress.** Only the user's own HA on the LAN, which is what
  `ha_client` already does. This adds no new destination.
- **Privacy curation applies to fusion too.** The MCP layer already strips vitals, targets
  and identities before anything leaves the Core. `device_tracker` / `person` carries
  identity by construction, so it must be held to the same rule rather than to a new one.
- **`recog.py` precedence is a separate axis.** The identity precedence chain is
  user-pin > self-describe > MUD > DHCP-fingerprint > port-hint > OUI. An HA motion sensor
  is a **presence** signal, not a claim about a device's identity, so it belongs on the
  consensus axis and not in that chain. Mixing them would let a room's occupancy quietly
  rewrite what a device *is*.
- **Read-only by default (`ha_import`).** None of this actuates anything. Control stays
  behind `call_ha_service`, gated, with ADR-0005 untouched. This is entirely read and fuse.

## Explicitly out of scope

- **Control and actuation** — that is `call_ha_service`, gated, and a different subject.
- **Exposing it over MCP** — already done. MCP proxies `RoomState`, so it inherits any
  enrichment for free.
- **Mobile display** — also a consumer of `RoomState`, and also inherits for free.

## The adversarial question, before anyone writes code

If HA entities can raise a room's presence confidence, then **anything that can write to HA
can fabricate presence in Wavr** — or, more usefully to an attacker, fabricate *absence*. A
design that fuses HA needs an answer to "what happens when the HA instance is lying", and
that answer should arrive with the first slice rather than after it.
