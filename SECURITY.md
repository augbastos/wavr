# Security

Wavr runs on a network you own and holds things worth protecting: device
credentials, TLS keys, camera sources, and a live model of who is in which room
of your home. A defect here is not a crash — it is somebody learning when your
house is empty.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting** on this repository:
*Security → Advisories → Report a vulnerability*. It opens a private thread
visible only to the maintainer, and it is the only channel that does not
publish the problem while it is still exploitable.

Please do not open a public issue for anything exploitable, and do not include
a working exploit in the first message — describe the class of problem and how
to reach it, and we can go into detail privately.

What helps most, in rough order:

- which surface (backend, dashboard, desktop shell, companion app, Core
  launcher, MCP server, firmware node);
- whether it needs LAN access, a paired credential, or neither;
- what an attacker gets, concretely;
- the smallest sequence that reproduces it.

This is a one-person project. You will get an acknowledgement, not an SLA. I
will tell you honestly whether and when I expect to fix it rather than leave
you guessing.

## What is in scope

Anything that breaks one of the invariants this product is built on:

- **Unauthenticated reach.** The base install is loopback-only; multi-device is
  opt-in. Anything that reaches state or control without the credential the
  design requires.
- **Credential handling.** A credential appearing in a log, a response body, an
  error message, or on screen. A pairing ceremony that mints or revokes without
  the gate it claims.
- **Raw sensing leaving the machine.** Camera frames or raw positions written to
  disk, published over MQTT, sent to an LLM, or reachable over the API.
- **Egress that was never switched on.** Any request off the box that is not one
  of the individually-enabled paths.
- **Transport.** Certificate pinning that can be bypassed, TLS claimed where a
  plain socket is in use.
- **The MCP surface.** Read-only tools that turn out to write; the Home
  Assistant control tool reachable without its gate.

## What is not

- Anything requiring physical access to an unlocked machine that is already
  running Wavr as the owner.
- The deliberately-simulated demo mode, which announces itself on screen and
  makes no network requests.
- Denial of service against your own Core from inside your own LAN.
- Findings from automated scanners with no reachable path described.

## What this project does not claim

Wavr has one user and has never been deployed anywhere but its author's home.
There is no security team, no bug bounty, and no formal audit. Treat it
accordingly: read the code before you put it on a network you care about.
