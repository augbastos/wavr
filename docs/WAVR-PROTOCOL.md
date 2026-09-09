# Wavr Protocol v1

- **Status:** Accepted. Describes the protocol as implemented on `main`.
- **Version:** 1
- **Scope:** the interoperability contract between Wavr components — Core, Client,
  Node, and peer Core — over the LAN.

This document is a specification, not a tutorial. For step-by-step operator
instructions see `docs/NODE-ONBOARDING.md` and `docs/deploy/multi-device.md`. For the
security *rationale* behind a given rule, see the referenced ADR — this document
states the rule; the ADR states why.

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in RFC 2119.

## Terminology

- **Core** — the authoritative Wavr runtime for a Space: fusion, storage, the HTTP
  API, policy. Implemented by `backend/wavr/app.py` and everything it wires together.
- **Client** — a device that renders the Wavr UI (the bundled dashboard, or a paired
  companion's browser/PWA). A Client is a *device function*, not a credential — see §10.
- **Node** — a headless sensor the operator flashes (first target: ESP32 +
  HLK-LD2450) that pushes presence telemetry to exactly one Core over HTTPS. A Node
  is not a companion device and holds no dashboard, no role, and no Space
  membership of its own. Its wire contract is `firmware/NODE_PROTOCOL.md`; this
  document defers to it and does not restate it (§7).
- **Peer Core** — a second Wavr Core the operator has explicitly paired with this
  one, for cross-instance fusion/config (Phase 1+). Not a federation partner —
  see §13.
- **Space** — the first-class container one physical environment (a home, a shop)
  is modeled as. See §10.

---

## §1. Scope

This document specifies:

- how Wavr instances **discover** each other on a LAN (§3),
- the **transport and security** envelope every message above rides on (§4),
- the **Capability Manifest** a device uses to describe what it can physically do
  (§5),
- how a Client, peer Core, or companion **establishes identity** with a Core (§6),
- a pointer to the **Node wire contract** (§7),
- the **sensing data model** (`SensingEvent`, `RoomState`) exchanged between a
  source/Node and a Core, and streamed from a Core to a Client (§8),
- how multiple Cores in one Space agree on **who is authoritative** (§9),
- the **Space model** — the three independent axes a person, a device, and a
  credential are described by (§10),
- common **error semantics** (§11),
- a summary of **security requirements** (§12),
- what this protocol **deliberately does not do** (§13),
- a **conformance** statement (§14).

This document does not specify the HTTP resource shapes of routes that are pure
Core-local administration with no second implementation to interoperate with
(e.g. `/api/settings`, `/api/discoveries`) — those are documented in code
(`backend/wavr/api_space.py`) and are not part of the cross-component contract
this spec governs. A route is in scope here if a *different* piece of software
(firmware, a second Core, a companion client) has to implement it correctly for
Wavr to work.

## §2. Versioning and negotiation

Wavr Protocol version **1** is the version described by this document. It is not
yet negotiated in the general case, because there is only one version. Two
version markers already exist in the wire formats this document governs, and
they are the seam a version 2 would use:

- **mDNS TXT `v`.** Every `_wavr._tcp.local.` advertisement carries a `v=1` TXT
  record (`backend/wavr/mdns_peers.py`, `_build_service_info`). A browsing
  instance MAY use this to refuse or specially handle a peer advertising a `v`
  it does not understand, before ever dialing it.
- **Capability Manifest `protocol_version`.** Every `CapabilityManifest` carries
  an integer `protocol_version` (default `1`, bounded to `[1, 999]` on parse —
  `backend/wavr/capabilities.py`). A Core reading a manifest from a future Node
  or Client MUST use this field to decide whether to accept, down-convert, or
  reject the manifest, rather than guessing from its shape.

**How version 2 would be negotiated**, should it become necessary: a component
advertising or sending version 2 payloads MUST also continue to satisfy every
version 1 MUST in this document when talking to a peer that has not itself
advertised version 2 support (`v` TXT record, or an equivalent field added to the
relevant payload). A Core encountering a manifest, node telemetry payload, or
peer message whose declared version it does not understand MUST fail closed —
reject or ignore the message, never guess at an unknown shape — mirroring the
existing rule that an unrecognized `CapabilityManifest.protocol_version` outside
`[1, 999]` is clamped rather than trusted. There is deliberately no
protocol-wide "negotiate down to the lowest common version" handshake: each
versioned surface (manifest, mDNS advertisement, a future node-protocol bump)
negotiates independently, because they change independently in practice.

## §3. Discovery

**Service type:** `_wavr._tcp.local.` (mDNS/DNS-SD), implemented in
`backend/wavr/mdns_peers.py`.

### 3.1 Advertisement

A Core or Desktop instance MAY self-advertise. Core (the native Kotlin launcher,
`core-launcher`) advertises this natively; Desktop (no NsdManager equivalent)
advertises via the `zeroconf` Python package (an optional `[mdns]` extra — a base
install never imports it).

TXT record fields:

| Field | Meaning | Required | Notes |
|---|---|---|---|
| `v` | Wavr Protocol version | Yes | Currently always `"1"`. |
| `path` | Base path of the advertised service | Yes | Currently always `"/"`. |
| `role` | Advertiser's role hint | Yes | e.g. `"desktop"`. Free-form; not authenticated (see §3.3). |
| `sid` | Opaque Space id, first 16 chars | No | `""` when the advertiser predates this field or has no Space yet. |
| `pv` | Wavr Protocol version the advertiser speaks | Yes | Currently always `"1"` (`WAVR_PROTOCOL_VERSION`). Distinct from `v` above, which versions the DNS-SD advertisement's own shape rather than the application protocol; both happen to be `1` today. |
| `cid` | The advertiser's own Core id, first 40 chars | No | Absent when the advertiser has no self-registered Core row yet (pre-Space). |
| `st` | The advertiser's own belief about its leadership status | No | e.g. `"primary"` or `"standby"`, truncated to 16 chars. Present together with `ep` — see §3.4. |
| `ep` | The epoch the advertiser believes applies to its own `st` | No | Present iff `st` is present. |

### 3.2 The `sid` field

`sid` lets a browsing device tell "these two Cores serve the same Space" apart
from "these are two unrelated Wavr installs on one LAN," **without** the Space's
human-chosen name ever going out over mDNS. A receiver:

- MUST bound `sid` to 16 characters and MUST strip every character that is not
  alphanumeric or `-` before using it for anything (it is attacker-controllable
  text off the LAN that can end up in an admin UI list — `mdns_peers._collect_peers`).
- MUST NOT treat an empty `sid` as an error; it means "no Space yet" or "predates
  this field," not "malformed."
- MUST NOT expect `sid` to be a complete Space id — it is deliberately truncated
  to 16 characters, a hint for matching, not a credential.

The Space's `name` field (`space_store.Space.name`) MUST NOT be broadcast over
mDNS. It is disclosed only after pairing, over the authenticated channel (§6),
never in a browsable, unauthenticated TXT record.

### 3.3 What discovery does and does not prove

Discovery is advisory. A browsed `DiscoveredPeer` (name, host, port, role,
space_id) MUST NOT be treated as authenticated or trustworthy on its own — it is
input to a human decision (which peer to pair with, which Core is "nearby"), not
a credential or a capability grant. Every subsequent step (§4, §6) re-verifies
identity independently; nothing about mDNS discovery shortcuts that.

### 3.4 Core-coordination hints (`cid`/`st`/`ep`/`pv`) are discovery only

A self-advertising Core additionally broadcasts its own
`core_id`, its own belief about its leadership status, the epoch that belief
applies to, and the Wavr Protocol version it speaks (§3.1's `cid`/`st`/`ep`/`pv`
fields). These exist so a browsing device's Discovery Inbox card can say
something meaningful ("a Core for this Space, currently primary, is on this
network") instead of a bare hostname — nothing more.

- `st`/`ep` reflect whatever the advertiser's OWN Core record currently says
  about itself. A receiver MUST NOT assume `st` appears only for a
  self-declared primary — a Core that has joined a Space as a **standby**
  member (`POST /api/setup/join-space`) has a real, non-empty status of its
  own (`"standby"`) and MAY advertise `st`/`ep` right alongside a primary's.
  The field says "this is what I currently believe about myself," not "I am
  authoritative."
- Every field in this subsection is exactly as forgeable as the rest of an
  mDNS TXT record (§3.3): anything on the segment can broadcast any
  `cid`/`st`/`ep`/`pv` it likes. An implementation **MUST NOT** let any of
  them influence Core leadership (§9) or any other authorization decision —
  they feed a Discovery Inbox card for a human, and nothing else. §9.1 states
  the corresponding rule from the leadership side: only an authenticated,
  paired-peer channel may ever move a Core's belief about who is primary.

## §4. Transport and security

### 4.1 Baseline: loopback-only

By default (`WAVR_MULTIDEVICE` unset), a Core's HTTP API **MUST** accept
requests only from loopback (`127.0.0.1`, `::1`, and the test-harness peer name
`testclient`). This is enforced by a hard-coded peer-address check
(`app._is_loopback`, ADR-0002) — never by a config value alone — so no
environment variable other than `WAVR_MULTIDEVICE` itself can widen it.

### 4.2 Opt-in: authenticated LAN access

When `WAVR_MULTIDEVICE=1` (ADR-0006), the Core's `loopback_or_authed` middleware
(`backend/wavr/app.py`) additionally accepts a request when:

1. the peer address is in the Core's own IPv4 `/24` (`auth.in_subnet`), **AND**
2. the request carries a valid, non-revoked `Authorization: Bearer <token>` for a
   device paired to this Core (§6).

Both conditions **MUST** hold; being on the same Wi-Fi is never sufficient on its
own. A caller that fails either check MUST receive `403 {"detail": "forbidden"}`
(or, for the strict-loopback default, `403 {"detail": "loopback only"}`) **before**
any token lookup — an off-subnet caller's token is never even looked up, so a
stolen token is useless off the LAN.

**IPv4 only.** `auth.in_subnet` MUST return `False` for any address that is not
IPv4 (`peer.version != 4 or local.version != 4`). A Core exposed only over IPv6
LAN addresses, or a client connecting from an IPv6-only address, will be denied
the authenticated-LAN path today — this is a fail-closed limitation, not an
oversight to route around at the application layer. IPv6 loopback (`::1`) is
still accepted for the loopback path (§4.1); it is only the *authenticated LAN
peer* path that is IPv4-only.

Certain paths are exempt from the token requirement while still being
subnet-bounded (never open to the public internet, never open off-LAN):

- Onboarding entry points that mint no token and are bounded by a short-lived,
  rate-limited code: `/api/pair`, `/api/peers/redeem`, `/api/nodes/enroll`,
  `/api/pair-request`, `/api/pair-request/status`.
- The node-initiated join request pair (§7.1), bounded differently — by
  per-source-IP rate limiting and a capped, aged-out pending list rather than a
  code: `POST /api/nodes/request` (mints no token, only a pending record) and
  `POST /api/nodes/claim` (gated by the 192-bit `request_id` capability
  `/api/nodes/request` returned, never a code).
- The Node data plane, which self-authenticates in-handler on a *Node* bearer
  token (a different credential space from Device tokens — see §7):
  `/api/nodes/telemetry`, `/api/nodes/heartbeat`, `/api/nodes/reactivate`.
- The static shell (so an unpaired companion can load the pairing screen):
  `/`, `/index.html`, `/measure.html`, `/manifest.webmanifest`, `/sw.js`,
  `/icon.svg`, `/sdk/javascript/wavr.js`, and everything under `/vendor/`,
  `/experiences/` and `/js/`.

  `/js/` is the shell's own script modules — one page split across files, not a
  second surface — and it is a PREFIX rather than an enumerated list on purpose:
  a service worker precaches the shell with `Cache.addAll`, which is
  all-or-nothing, so a single module the page loads and the exemption omits does
  not cost one script, it costs offline launch entirely. The route behind the
  prefix accepts only a bare lowercase `*.js` name resolving to a direct child of
  the shell's script directory, so widening the exemption does not widen what can
  be read. `/experiences/` and the SDK file they import are the same class of
  thing: markup and script, no data, no action.

  Every path and prefix in this bullet MUST be exempt from the `X-Wavr-Local`
  CSRF header as well as from the token, and that is not a convenience. A browser NAVIGATING to a URL — an
  address typed in, a bookmark, a QR code, a `<script src>` — sends no custom
  request header, and no page-side code has run yet to add one. Gating the page
  on a header the act of opening it cannot carry makes it impossible to open,
  which is the single thing it exists for. The header stays mandatory on every
  API and action path, which is where CSRF is actually a risk.

A Core implementation **MUST NOT** add a new unauthenticated exemption without
bounding at least as strong as one of the four shapes above: a short-lived
rate-limited one-time code, a high-entropy capability the requester was handed
plus per-source-IP rate limiting, a distinct credential space the handler itself
verifies, or a genuinely non-sensitive static asset that serves no data and
performs no action. Every existing exemption is one of those four, and each is
still bounded by `in_subnet` regardless.

### 4.3 Roles and scopes

A validated device credential resolves to a **role**: `root` (loopback), `central`,
`user`, `agent`, or `guest` (`devices.VALID_ROLES`, plus the implicit `root`).
Each role has a **default scope set** (`auth.DEFAULT_SCOPES`) drawn from:
`presence:read`, `presence:write`, `network:read`, `camera:view`, `control`,
`admin`, `mcp`. A device MAY be granted an explicit scope set narrower or wider
than its role default (`Device.scopes`); `NULL` means "derive from role."

- `root` (loopback) is **never** scope-limited.
- `central` gets the full default set.
- `user` gets presence/network/camera-view but not control/admin/mcp.
- `agent` (an MCP-only principal, Phase 2A) gets **only** `mcp`, and is further
  restricted by a *second*, independent axis — a per-tool allow-list
  (`auth.effective_tool_scopes`) — that root/central/user are not subject to at
  all (it resolves to `None` for them, meaning "not restricted by this axis").
- `guest` gets **only** `presence:write` — it may register its own presence and
  nothing else.

A route's gate MUST check role (or an equivalent `require_local`/`require_root`/
`require_central` guard) **and** — where a scope applies — the scope, as two
independent checks. A scope check MUST NOT be treated as a substitute for the
underlying role/CSRF gate it sits alongside; it is additive.

### 4.4 TLS

When `WAVR_MULTIDEVICE=1`, the Core serves HTTPS/WSS (`backend/wavr/tls.py`).
There is no public CA in this system — every Core presents a self-signed
certificate (CN `wavr`; SANs `localhost`, `127.0.0.1`, and the Core's LAN IP;
2048-bit RSA; ~397-day validity). Trust is established **trust-on-first-use,
then pinned**, in three shapes across this protocol, and an implementation MUST
NOT substitute "accept any cert" for any of them past the initial trust moment:

1. **Operator eyeball verification (pairing/peer flows).** The Core's SHA-256
   certificate fingerprint (`tls.cert_fingerprint`, uppercase colon-separated
   hex — the browser convention) is shown on the Core's own trusted screen. The
   operator compares it, by eye or via the 6-digit `verification_code`
   convenience derivation, against what the connecting client observes. A
   mismatch means stop — never proceed past it.
2. **Peer-to-peer pinning (`peer_client.py`).** Every call to a paired peer Core
   pins the fingerprint the operator confirmed at pairing time, and
   **re-verifies it on every single call, not just at pairing** — the TLS
   handshake completes with zero application bytes sent, the presented
   certificate's fingerprint is checked, and only on a match does the
   credential (bearer token) ever touch the wire. A mismatch MUST abort before
   sending the credential, MUST NOT retry with verification disabled, and
   SHOULD surface as a generic failure (never echoing the observed fingerprint,
   to avoid an oracle).
3. **Node TOFU pinning (`firmware/NODE_PROTOCOL.md`).** Specified in the Node
   protocol document, not restated here (§7).

### 4.5 WebSocket auth

A browser WebSocket handshake cannot carry an `Authorization` header. `/ws/live`
is therefore gated differently from the HTTP surface it is not covered by
Starlette's HTTP middleware for:

- **Loopback:** accepted if the peer is loopback and, when an Origin header is
  present, it matches the same-origin allowlist. When `WAVR_LOCAL_TOKEN` is set,
  the token MUST also be presented (`X-Wavr-Token` or `Authorization: Bearer`)
  even on loopback.
- **Authenticated LAN peer:** the peer MUST be in-subnet, MUST present a valid
  single-use `ticket` query parameter minted by `POST /api/ws-ticket` (§6.3), and
  the device behind that ticket MUST NOT be revoked or expired **and** MUST hold
  the `presence:read` scope. The revoked check is re-run on a wall-clock cadence
  (2 seconds) for the life of the connection, not only at handshake time, so a
  mid-stream revoke still drops the connection promptly.
- Any handshake failing these checks MUST close with WebSocket close code
  `1008` (policy violation) rather than accepting and then silently sending
  nothing.

## §5. Capability Manifest

A **Capability Manifest** (`backend/wavr/capabilities.py`) is a device's
self-description of what it can *physically do* — not what a person may do
(§10.1), not what a credential may reach (§4.3), and not what job an operator
assigned it (§10.2). It is the input to "what should this device become: Core,
Node, Client, or some combination?"

### 5.1 Honesty rule (load-bearing)

Every capability value is a **tristate**: `true` (proven present), `false`
(proven absent), or `null`/absent (unknown — a probe that raised, timed out, or
needed a dependency that was not installed). A producer of a manifest **MUST
NOT** report `false` for a capability it could not actually determine; it MUST
report `null`/omit the key instead. A consumer **MUST** render `null` as
"unknown," never coerce it to "no" — recommending against using a device as a
Core because a probe crashed would misrepresent a failure as a fact.

### 5.2 Wire shape

```json
{
  "platform": "linux",
  "arch": "aarch64",
  "os_version": "Linux 6.1.0",
  "functions_supported": ["core", "node", "client"],
  "capabilities": {
    "wifi": true, "ethernet": false, "ble": null, "bluetooth": null,
    "uwb": null, "nfc": null,
    "camera": true, "microphone": null, "gps": null, "accelerometer": null,
    "mmwave": false, "wifi_csi": null, "gpu": false, "display": true,
    "battery": false, "permanent_power": true, "usb": null,
    "docker": true, "mdns": true, "network_scan": true, "raw_socket": null
  },
  "ram_mb": 4096,
  "cpu_count": 4,
  "compute_tier": "medium",
  "model": "Raspberry Pi 5 Model B",
  "protocol_version": 1
}
```

Field constraints, enforced by `CapabilityManifest.from_dict` on any manifest
received off the wire (a manifest is device-controlled, untrusted input):

- `platform` MUST be one of `windows|linux|macos|android|ios|esp32|unknown`;
  any other value is coerced to `unknown`, never rejected outright.
- `capabilities` keys not in the fixed vocabulary (`CAPABILITY_KEYS`) MUST be
  dropped, not stored or forwarded — a device MAY NOT invent new capability
  keys; a new hardware class requires extending the vocabulary in one place,
  not per-device negotiation.
- A `capabilities` value that is not literally a JSON boolean (a string, a
  number, an array) MUST be treated as `null` (unknown) — never coerced to a
  truthy/falsy guess.
- `functions_supported` entries not in `{core, node, client}` MUST be dropped.
- `compute_tier` MUST be one of `micro|low|medium|high`; anything else is
  coerced to `low`.
- `ram_mb` and `cpu_count`, when present, MUST be clamped to `[0, 4194304]` and
  `[0, 4096]` respectively; a JSON boolean in either field MUST be treated as
  absent (Python's `bool` is an `int` subclass — a naive cast would silently
  read `true` as a RAM figure of 1).
- `protocol_version` MUST be clamped to `[1, 999]`, defaulting to `1`.
- `model` is truncated to 96 characters, `arch` to 32, `os_version` to 64 —
  free-form display text, never parsed as structured data by a receiver.

### 5.3 What a manifest does NOT grant

Submitting a manifest (`PUT /api/space/devices/{device_id}/manifest`) **MUST
NOT**, by itself, change any credential's role, scope, or authority. A device
claiming `"functions_supported": ["core"]` gains no authority to act as Core —
that requires an explicit `POST /api/space/cores/{core_id}/promote` by an
already-authorized operator (§9). A manifest is evidence for a UI recommendation,
never an authorization.

### 5.4 Zero egress

A capability *scan* (`capabilities.scan_host`, as opposed to a manifest received
from a peer) MUST read only local OS state and MUST NOT open a network
connection, resolve a hostname, or open a camera/microphone. It MUST be safe to
run before the operator has consented to anything, including before a Space
exists (`POST /api/setup/scan` is loopback-root-only, reachable pre-Space).

## §6. Identity and pairing

A Core issues **Device** credentials (`backend/wavr/devices.py`) to Clients,
peer Cores, and MCP agents. A Node (§7) is a *separate* credential space, never
a Device row. Four distinct pairing flows exist; all four MUST reduce, at their
mint site, to the same `DeviceStore.add(name, role)` call, so a device paired by
any flow is byte-identical (same token entropy, same hashing at rest) to one
paired by any other.

### 6.1 Rotating pairing code (default flow)

1. An already-authorized operator mints a short-lived code:
   `POST /api/pair-code {"role": "user"|"central"}` → `{"code", "cert_fingerprint"}`.
   Codes are 8 digits, TTL ~120s, rate-limited to 10 failed attempts per 60s
   **per source IP** (one flooding host cannot lock out another).
2. The companion redeems it over the LAN, without holding any prior credential:
   `POST /api/pair {"code", "device_name"}` → `{"device_id", "token"}`. The
   token **MUST** be returned exactly once; it is stored hashed thereafter and
   is never retrievable again.
3. A live WebSocket needs a ticket, because the token cannot ride a WS
   handshake: `POST /api/ws-ticket` (`Authorization: Bearer <token>`) →
   `{"ticket"}`, a single-use, ~30s-TTL secret, then `GET /ws/live?ticket=<ticket>`.

### 6.2 "Approve on the Core" (operator-present flow)

For a companion that has no code yet: it asks the Core to let it in, and a
**local** operator approves or denies on the Core itself
(`backend/wavr/pair_requests.py`). Three independent factors gate the mint:

1. an unguessable 192-bit `request_id` (the capability to poll status),
2. a **loopback-root** Approve (`require_local` + `require_root` — even an
   authenticated remote `central` peer MUST NOT be able to approve),
3. a per-request 6-digit `compare_code` (Bluetooth-SSP-style numeric
   comparison) the operator reads off the companion's screen and echoes back.
   Factor 2 alone is insufficient: the Core's cert fingerprint is
   byte-identical for every pending request, so factor 3 is what disambiguates
   *which* pending request is the honest device when two requests race.

`create()`/`poll()` MUST mint nothing; only `approve()`, on a correct
`confirm_code` match (`secrets.compare_digest`, constant-time), mints a token.

### 6.3 Peer-to-peer (Core-to-Core) pairing

Reshaped to mirror the companion ceremony rather than vend a code over the
network (the earlier design, which did the latter, is deleted — see
`api_peers.py`'s module docstring for the closed findings):

1. **Redeem** (`POST /api/peers/redeem {"code", "requester_name"}`) is the one
   deliberately-unauthenticated, in-subnet-bounded entry point a remote peer
   calls — safe because it only ever consumes a code minted on a *trusted
   loopback screen* (`/api/pair-code`), never one vended over the network.
2. **Forward leg + reverse bootstrap** (`POST /api/peers/confirm`,
   loopback-root only): the initiating Core redeems the target's code over a
   channel pinned to the fingerprint the operator confirmed (§4.4.2), receiving
   a token to call the peer; it then mints, in its own store, the credential
   the peer will use to call *it* back, and pushes that credential to the peer.
3. **Link-back** (`POST /api/peers/link-back`, `require_central`) is the one
   peer-reachable completion route: the caller MUST already be authenticated as
   `central`; the peer's own claimed `base_url` MUST be validated as an
   in-subnet LAN literal (SSRF guard, `_validate_peer_url`) before this Core
   ever dials it.

A peer's self-reported `device_id` from its own store MUST be ignored by the
initiator — only the initiator's own locally-derived id for that peer is used
for later revocation, so an unpair always revokes the row that actually exists.

### 6.4 Space-scoped setup pairing

`POST /api/setup/create-space`, `/join-space`, and `/adopt` (§10) are
loopback-root-only and are the mechanism by which a Core acquires a Space
identity and mints its first Owner. They are not, by themselves, a device
pairing flow — no Device token is issued here — but they gate what role a
*subsequent* device pairing resolves to (`device_role_for_person`, §10.1).

## §7. Node protocol

A Node's wire contract — enrollment, telemetry, heartbeat, reactivation, the
kill-switch state machine, and TLS TOFU pinning — is specified in full in
[`firmware/NODE_PROTOCOL.md`](../firmware/NODE_PROTOCOL.md). This document does
not duplicate it; firmware and backend MUST treat that file as the single
source of truth for the Node data plane, and MUST NOT let this document and
that one diverge.

What this document adds, at the level this spec operates:

- A Node credential is **not** a Device credential (§6). `NodeStore`
  (`backend/wavr/nodes.py`) is a separate table with its own bearer-token hash,
  its own TOFU-pinned cert fingerprint, and its own kill-switch state machine
  (`active|disabled|revoked`). The Wavr Protocol's role/scope model (§4.3) does
  not apply to a Node; a Node's only two possible actions are "push telemetry"
  and "answer a heartbeat."
- **Remote-OFF-never-ON is a protocol invariant, not an implementation detail.**
  No route in this protocol MAY offer a remote enable for a disabled Node. The
  only `disabled → active` transition is `POST /api/nodes/reactivate`, and it
  MUST be node-initiated, gated on a strictly-increasing, NVS-persisted
  `press_count` that a physical button press bumps. A server implementation
  MUST NOT add a second enable path, even an administrative one.
- **Room and modality are Core-assigned, never Node-asserted, under EITHER
  enrollment flow (§7.1).** A Node's telemetry payload MUST be attributed to
  the room and fusion modality recorded at enrollment — an operator's
  declaration on the trusted loopback screen (the code-minting flow), or an
  operator's declaration at approval time (the node-initiated flow, §7.1) —
  never to a value the Node's payload itself carries. This is the anti-spoof
  invariant that makes a compromised Node's blast radius "can lie about its own
  sensor readings," never "can relocate itself or claim a different sensor
  class." A Node's own self-description at join time (§7.1's `name_hint`/
  `sensor_hint`) is a CLAIM, not a declaration: an implementation MUST store it
  apart from the trusted fields and MUST NOT let it reach fusion or influence
  the room/modality/transport an operator ultimately assigns.
- A Node's telemetry confidence is capped by transport trust
  (`nodes.TRANSPORT_CAP`: `native` 1.0, `mqtt` 0.7) — an interop Node reached
  over a shared broker is inherently less anti-spoof-resistant than one
  presenting its own per-Node bearer token over pinned TLS, and the protocol
  scales evidence by that, not by the sensor's own claimed accuracy.

### 7.1 Node-initiated enrollment (request → approve → claim)

Alongside the operator-minted-code flow above
(`firmware/NODE_PROTOCOL.md`), a Node MAY instead speak first and let a human
approve it from an inbox — the Node-protocol analog of the companion
"Approve on the Core" flow (§6.2). A Core implementing this flow exposes three
routes (`backend/wavr/nodes.py`, `backend/wavr/api_nodes.py`):

1. **Request** (`POST /api/nodes/request`) — unauthenticated (the Node holds no
   credential yet) and bounded the way `/api/pair-request` is: in-subnet only,
   rate-limited to 5 requests per source IP per 5 minutes. It creates a PENDING
   node record carrying **no token** and returns a 192-bit `request_id` — the
   Node's own capability to poll for the outcome, returned exactly once. The
   pending list itself is bounded (aged out after 7 days; capped at 50 entries,
   oldest dropped first) so a hostile or noisy LAN cannot grow it without
   limit.
2. **Approve** (`POST /api/nodes/{id}/approve`, loopback-root) is where a human
   supplies the load-bearing fields — `name`, `sensor_type`, `room`,
   `transport` — exactly as the code-minting flow's trusted screen does. The
   request's own self-reported claims (`name_hint`/`sensor_hint`, submitted at
   step 1) MAY pre-fill a form, but MUST NOT, by themselves, populate the
   trusted columns — what lands there is what the operator submits at this
   step, never what the Node said about itself. A companion `POST
   /api/nodes/{id}/deny` (same gate) removes the pending record outright rather
   than tombstoning it, so a re-plugged or re-denied board can ask afresh.
3. **Claim** (`POST /api/nodes/claim`) is how the Node collects the token an
   approval minted. `request_id` MUST travel in the request body, never a query
   string, so it never lands in an access log. It is single-use: on a
   successful claim the server MUST clear the row's polling capability, so a
   replayed `request_id` is answered identically to an unknown one.

An implementation of this flow MUST additionally hold:

- **The minted token lives only in memory, never on disk**, keyed by
  `request_id`, for a bounded pickup window (`CLAIM_PICKUP_SECONDS`, 10 minutes
  in this implementation) — the same "a plaintext credential never touches
  storage" rule the rest of this protocol's pairing flows follow (§6.1's token,
  §6.2's `confirm_code`). A Core restart during that window loses the
  in-memory token.
- **A lapsed pickup MUST return the Node to PENDING, never strand it.** If the
  Node has not claimed its token before the window lapses or the Core
  restarts, `claim()` MUST put the node back in the pending queue (clearing the
  now-stale token hash) rather than leaving it permanently unable to collect a
  credential that exists only as a hash server-side. The operator sees it
  again and approves again — self-healing, and it never requires persisting
  the plaintext to survive a restart.

This flow trades the code flow's stronger anti-spoof property — the operator
commits to a specific identity *before* the device exists at all — for
convenience: a board that already has network reach and a cert fingerprint can
be reviewed and approved from an inbox instead of round-tripping a one-time
code through a captive portal. See `docs/NODE-ONBOARDING.md` for operator
guidance on which flow to use when.

## §8. Sensing data model

The canonical **input** shape any source (local or Node-derived) produces, and
the canonical **output** shape a Core's fusion layer publishes, are fixed
dataclasses (`backend/wavr/events.py`, `backend/wavr/roomstate.py`). A Node's
telemetry (§7, `firmware/NODE_PROTOCOL.md`) is translated into exactly this
input shape by `nodes.node_event()` before it ever reaches fusion — there is
one `SensingEvent` shape in this system, not a Node-specific variant.

### 8.1 `SensingEvent` (input to fusion)

```json
{
  "room": "living_room",
  "modality": "mmwave",
  "presence": true,
  "motion": 0.4,
  "breathing_bpm": null,
  "heart_bpm": null,
  "confidence": 0.9,
  "ts": "2026-09-03T10:00:00+00:00",
  "targets": [
    {"id": 1, "x": 1.0, "y": 0.5, "z": null, "posture": "standing",
     "velocity": 0.3, "confidence": 0.9}
  ],
  "identities": [
    {"person": "Sam", "source": "ble", "rssi": -62}
  ],
  "count": 1
}
```

- `modality` is an open vocabulary in practice (`wifi_csi|network|camera|mmwave|
  sim|pir|ble|node|...`); a Core MUST NOT reject an unrecognized modality
  string outright, but a modality with no configured fusion weight contributes
  nothing (`fusion.py`'s per-modality weight table is the actual gate on
  influence, not this schema).
- `count` (per-source person count) MUST be `null` — never a fabricated `0` —
  for any modality that cannot honestly count discrete people (presence-only
  sources: network/BLE/wifi_csi/sim/pir). Only counting-capable modalities
  (camera, mmwave) MAY set an integer.
- `Target.x`/`Target.y` MAY be `null` when a source knows posture but not
  position (e.g. a camera without a homography). `Identity.rssi` MAY be `null`
  for a source with no proximity signal (a plain network scan). A Client
  consuming `Identity.rssi` **MUST NOT** render it as room-level localization —
  a single antenna localizes to the house, not a room.
- `Identity.person` is **non-biometric**: an operator-configured label bound to
  a device address (BLE/MAC), never derived from face, voice, gait, or
  re-identification. A producer of a `SensingEvent` MUST NOT populate
  `identities` from any biometric inference.

### 8.2 `RoomState` (fusion output, and the `/ws/live` wire shape)

```json
{
  "room": "living_room",
  "occupied": true,
  "confidence": 0.87,
  "vitals": {},
  "sources": [],
  "targets": [],
  "identities": [],
  "person_count": 1,
  "explanation": "mmwave: presence, confidence 0.90",
  "ts": "2026-09-03T10:00:00+00:00",
  "precision_level": "position",
  "precision_pct": 100,
  "precision_next": null
}
```

- `precision_level` is an **authoritative enum**:
  `none|house|room|count|position`. `precision_pct` is an **ordinal** rung fill
  (`0/25/50/75/100`) derived from `precision_level` — it **MUST NOT** be
  interpolated or presented as a statistical confidence; it is a discrete
  capability rung, and `confidence` (a genuine 0–1 probability) is the separate
  field for "how sure," never conflated with "how detailed."
- `person_count` follows the same honesty rule as `SensingEvent.count`: `null`
  means unknown, never a fabricated `0`.
- `/ws/live` streams a `RoomState`-shaped JSON object per fused update, run
  through one further, optional privacy projection: when **Watch mode** is on
  (`backend/wavr/watch.py`), `targets`, `identities`, and `vitals` are replaced
  with their empty forms, and `watch: true` plus a room-level `unrecognized`
  boolean are added. A Client **MUST** treat the presence of `"watch": true` as
  "this payload has had per-person geometry/vitals removed by design," not as
  an error or an incomplete frame.
- Per-person `x`/`y` targets and vitals are **live-only** on this channel — they
  are never persisted to SQLite or forwarded to MQTT (ADR-0002 invariant 4);
  an implementation MUST preserve that boundary for any new egress it adds.

## §9. Core coordination (leadership)

A Space MAY have multiple Cores; exactly one is authoritative at any instant.
The mechanism (`backend/wavr/core_registry.py`) is a **monotonic epoch**, not a
consensus protocol:

> The primary is the Core named by the highest epoch anyone has seen.

- `epoch` lives on the **Space** (`space_store.bump_epoch()`), not on a Core,
  and only ever increases. Promotion = bump the epoch, then stamp the new
  primary at that value (`CoreRegistry.promote`).
- A Core that believes it is primary at epoch *N* and observes a peer claiming
  a higher epoch **MUST** stand down immediately (`observe_peer` → `VERDICT_YIELD`)
  — no timeout, no clock comparison, no quorum round.
- `CoreRegistry.promote` **MUST** reject a `new_epoch` that is not strictly
  greater than the highest epoch it has ever recorded — replaying an old
  promotion is exactly the split-brain condition this design exists to
  prevent.
- **Genuine split-brain (two claimants at the same epoch) MUST be surfaced, not
  silently absorbed.** The tie-break is `min(core_id)` — arbitrary, but total
  and symmetric, so both sides independently compute the same answer — **and**
  it MUST still report `VERDICT_CONTESTED` so a human learns about it. A Core
  implementation MUST NOT converge on a tie-break without also raising this
  signal.
- An **offline primary** MUST be reported as `status: "primary", "stale": true`
  — never silently rewritten to `offline`. Losing contact with the primary is
  not the same event as the primary handing over, and collapsing the two is
  exactly the ambiguity an operator needs disambiguated.
- **There is no automatic failover.** `promotion_candidate()` computes who
  *should* take over (preferring a mains-powered Core over a portable one) but
  nothing promotes on its own — an implementation MUST NOT auto-promote without
  an explicit, separately-designed opt-in policy layered on top; the primitive
  is deliberately already epoch-fenced so that addition changes only the
  trigger, never this section's invariants.

### 9.1 Two-tier trust: an authenticated peer poll feeds `observe_peer`; mDNS never does

**`observe_peer()` has a caller.** This subsection is
the whole design, stated precisely, because the two tiers are easy to blur:

1. **The authenticated tier — the only one that may move leadership.** A Core
   polls each of its PAIRED peers (`_observe_peer_cores`,
   `backend/wavr/app.py`) over the exact channel §4.4.2 already specifies for
   every peer call: certificate-pinned, bearer-tokened. It calls `GET
   /api/peers/core-status` — gated `require_central`, so only a peer that
   completed the pairing handshake can reach it — which answers with the
   peer's own `space_id`/`core_id`/`epoch`/`status`/`protocol_version` and
   nothing about the house. A response is discarded outright if its
   `space_id` does not match this Core's own Space (two unrelated Wavr
   installs sharing a LAN is ordinary, not a conflict). Only a response that
   passes both gates — authenticated channel, matching Space — is ever handed
   to `observe_peer()` (§9), which MAY yield this Core's primacy or raise a
   contested-epoch signal as a direct result.
2. **The discovery tier — advisory only, never wired to leadership.** An mDNS
   advertisement (§3.4) carries the same SHAPE of information —
   `core_id`/`status`/`epoch` — but is unauthenticated by construction:
   anything on the segment can broadcast it. An implementation MUST treat an
   mDNS-discovered Core exactly as §3.3 already requires for discovery in
   general: input to a Discovery Inbox card asking a human whether to pair,
   and nothing else. `_observe_unpaired_cores` (`backend/wavr/app.py`) is the
   reference shape of this — it reads mDNS, checks the Space match, and raises
   an inbox item; it MUST NOT and does not call `observe_peer()` or touch
   `CoreRegistry` in any way.

An implementation MUST NOT collapse these two tiers, even partially — e.g. by
letting an mDNS-advertised epoch shortcut or pre-seed the authenticated poll,
or by treating "discovered, same Space, high epoch" as grounds to skip pairing
before trusting a claim. The authenticated tier's whole security argument is
that `observe_peer`'s inputs are known to come from something that proved its
identity first; anything that lets the unauthenticated tier influence the same
outcome reopens exactly the forgeable-broadcast hole §3.3 and §3.4 already
name.

## §10. Space, people, and device functions

Before this model, a single value (`Device.role == "central"`) meant two
unrelated things: "this credential may administer Wavr" and "this box is a
hub." This protocol separates that into **three independent axes**
(`backend/wavr/space_store.py`):

1. **Person role** — what a *human* may do: `owner|admin|user|guest`
   (`space_store.PERSON_ROLES`), each resolving to a capability set
   (`space_store.PERSON_CAPABILITIES` — a vocabulary distinct from `auth.SCOPES`,
   §4.3, by design). Exactly one `owner` may exist per Space; a `transfer`
   operation, not a second `add_person(role=owner)` call, is the supported way
   to change who holds it.
2. **Device function** — what job a box does for the Space: `core|node|client`
   (`space_store.DEVICE_FUNCTIONS`), freely combinable — "Core + Node + Client"
   on one laptop is the normal single-machine setup, and an implementation
   MUST NOT force a device to pick exactly one function.
3. **Device auth role** — what a *credential* may reach: `central|user|agent|
   guest` (`devices.VALID_ROLES`, §4.3). This axis is **untouched** by the
   Space model; it remains the tested security surface `auth.py` enforces.

### 10.1 The one bridge between axes, at mint time

`space_store.device_role_for_person()` is the **only**, deliberately narrow,
one-directional bridge from axis 1 to axis 3 **at the moment a device pairs**:
when one of a person's devices pairs, the person's role determines the
credential role it receives (`owner|admin → central`, `user → user`,
`guest → guest`). This mint-time mapping:

- MUST NOT be widened to let a person role grant a credential role a human
  could not already have been granted by hand before this bridge existed.
- Acts only on a NEW pairing. It MUST NOT itself reissue, replace, or reach
  back into a token a device already holds — that is a distinct mechanism,
  §10.1.1.

### 10.1.1 Per-request narrowing (the person cap)

**This axis is wired into the real auth path, not only
a UI preview.** A second, independent mechanism caps what an *already-issued*
credential may reach, resolved **fresh on every authenticated request**
(`auth.narrower_role`, `auth._apply_person_cap`, called from `access_for_scoped`
— app.py's one call site for both the ordinary HTTP role/scope gate and the
MCP tool-scope gate):

> effective role = the NARROWER of (the role the credential was issued) and
> (the role the person currently holds)

An implementation MUST hold every one of the following:

- **Demotion is immediate.** There is no revoke, no window, no "existing
  devices keep their access until you get round to it": the very next request
  from a demoted person's device MUST resolve at the lower role. This is the
  direction that matters and the reason this mechanism exists — it revises
  the "does not touch their existing devices' credentials" description this
  document previously gave §10.1's mint-time bridge, which described the
  *mint-time* direction correctly but no longer describes the full picture
  now that this per-request cap exists alongside it.
- **Promotion MUST NOT widen a live credential.** A token issued as `user`
  stays `user` however senior its holder becomes; reaching a wider role
  requires deliberately re-pairing the device. A mis-click or a hostile edit
  on the people screen therefore cannot hand elevated authority to a
  credential already out in the world.
- **A device with no person association is unaffected.** Every device paired
  before this mechanism existed, every Node (§7, a separate credential space
  entirely), and every `agent` credential resolves exactly as it did before.
  `agent` is EXEMPT from this axis outright — it is a bounded machine
  principal, not a human's device, and has no person to be capped by.
- **A dangling association MUST deny outright, never degrade to the issued
  role.** If the person a device is associated with is no longer in the Space
  (removed, and the removal's own revoke did not — or has not yet — reached
  this device), resolving that person's current role MUST return "deny," not
  "fall back to whatever the credential was issued as." The same applies to a
  storage fault encountered while resolving the person's role: fail closed,
  never open. It is better to reject a legitimate request during a database
  hiccup than to silently serve one at un-narrowed authority.

See [ADR-0011](adr/0011-person-role-authorization.md) for why narrowing-only
(and specifically why promotion must not widen) was chosen over making the
person axis authoritative in both directions.

### 10.2 Discovery honesty (person↔device association)

A device MAY be associated with a person with `origin: "confirmed"` (a human
said yes) or `origin: "inferred"` (discovery's guess). An implementation MUST
render an `"inferred"` association differently from a `"confirmed"` one
wherever it surfaces to an operator — "we saw a Samsung phone on the LAN" is
evidence, not identity, and presenting it as fact is the specific mistake this
distinction exists to prevent.

### 10.3 Legacy adoption

A pre-Space installation (has paired Devices, no Space) is adopted, not
migrated destructively: every existing `central` Device is associated with a
single synthesized Owner with function `client`; every other device is left
unassociated ("we genuinely do not know whose it is, and guessing would be
wrong"). No credential is reissued, no token is invalidated, no device role
changes. An implementation MUST preserve this non-destructive property for any
future adoption path it adds.

## §11. Error semantics

A conformant Core uses these HTTP status codes consistently across every
surface this document governs:

| Code | Meaning here | Example |
|---|---|---|
| `400` | Malformed request the caller could have avoided (missing field, wrong type) | `/api/nodes/telemetry` with a non-integer `seq` |
| `401` | No credential presented where one is required | missing bearer token on a Node data-plane route |
| `403` | Credential presented but insufficient, or the caller is out of bounds (off-subnet, off-loopback, missing CSRF header, wrong role) | an out-of-subnet peer; a `user`-role device calling a `central`-only route |
| `404` | The named resource does not exist (or, deliberately, an unknown-vs-expired pairing/device id is not distinguished from "gone") | `DELETE /api/nodes/{id}` for an unknown node |
| `409` | A replay or a stale write was rejected | a Node telemetry `seq` that is not strictly increasing |
| `423` | The resource exists but is administratively locked | telemetry from a `disabled` Node |
| `429` | Rate-limited — an abuse brake, not a security boundary in itself | `/api/nodes/reactivate` hammered beyond its window |
| `502` | An upstream (a peer Core, Home Assistant) could not be reached, or its identity could not be verified | a peer whose pinned fingerprint does not match |

Two error-handling rules apply across every surface in this document:

- **No exfiltration oracle.** A `502`/`403` returned for a peer or pairing
  failure MUST NOT echo the peer's response body, the observed (mismatched)
  fingerprint, or any detail that would let a caller distinguish *why* a
  privileged check failed beyond "it failed." (`peer_client.PeerClientError`,
  `api_peers.py`'s `§B` comments are the reference behavior.)
- **Fail-closed on a missing gate, never fail-open.** Every admin router in
  this system takes its auth dependencies as an injected argument and
  substitutes a hard `403` "not wired" stub when that argument is omitted,
  rather than defaulting to `[]` (unguarded). A conformant implementation of a
  new admin surface MUST follow this same pattern: a forgotten wiring call is a
  loud `403`, never a silent open door.

## §12. Security requirements — summary

This section collects the cross-cutting **MUST**s already stated above, for a
reviewer checking a new implementation against the spec in one pass.

An implementation of this protocol:

- **MUST** default to loopback-only and require an explicit opt-in
  (`WAVR_MULTIDEVICE`) before accepting any LAN peer (§4.1–4.2).
- **MUST** require both subnet membership *and* a valid bearer token for a LAN
  peer — neither alone is sufficient (§4.2).
- **MUST** treat a Capability Manifest's `false` as "proven absent," never as a
  stand-in for "I didn't check" (§5.1).
- **MUST NOT** let a self-reported Capability Manifest or Node telemetry field
  (room, modality, functions_supported, sensor identity) grant authority or
  override a Core-assigned, operator-declared value (§5.3, §7).
- **MUST** pin and re-verify a peer Core's TLS certificate fingerprint on
  *every* call after pairing, not only at pairing time (§4.4.2).
- **MUST** implement remote-OFF-never-ON for a Node's kill switch with no
  administrative enable path of any kind (§7).
- **MUST** reject a non-increasing Core-promotion epoch, and MUST surface
  (never silently tie-break away) a genuine same-epoch split-brain (§9).
- **MUST NOT** let an mDNS-advertised `core_id`/`status`/`epoch` (§3.4)
  influence Core leadership. Only a PAIRED peer, polled over the
  certificate-pinned, bearer-tokened channel (`GET /api/peers/core-status`),
  may feed `observe_peer` (§9.1).
- **MUST** resolve a device's effective role as the NARROWER of its issued
  role and its associated person's CURRENT role, fresh on every request —
  demotion takes effect immediately, promotion never widens a live
  credential, and a dangling person association denies rather than degrades
  (§10.1.1).
- **MUST** keep per-person `x`/`y` targets and vitals live-only — never
  persisted, never forwarded to a non-live sink — on every egress path,
  existing or new (§8.2).
- **MUST** fail closed (explicit `403`) when an admin route's auth dependency
  is not wired, rather than defaulting to open (§11).
- **MUST NOT** broadcast a Space's human-chosen name over an unauthenticated
  channel (mDNS TXT records) (§3.2).
- **SHOULD** rate-limit failed pairing/enrollment attempts **per source IP**,
  so one flooding host cannot lock out a legitimate peer (§6.1, §7).
- **MAY** add a new capability key, modality string, or device function, but
  **MUST** do so by extending the shared fixed vocabulary in one place
  (`capabilities.CAPABILITY_KEYS`, `space_store.DEVICE_FUNCTIONS`) rather than
  accepting an arbitrary self-declared string as authoritative.

## §13. What this protocol deliberately does not do

- **No cloud broker.** Every pairing flow in §6 is peer-to-peer over the LAN.
  There is no hosted matchmaking service, relay, or account system a Wavr
  instance registers with to find or reach another instance.
- **No internet federation.** A peer Core (§6.3) is paired explicitly, on the
  same LAN, by an operator who can read both Cores' certificate fingerprints.
  There is no mechanism in this protocol for two Wavr instances on different
  networks to discover or federate with each other over the internet (VPN/tunnel
  bridging is a network-layer concern outside this document's scope, not a
  protocol feature).
- **No IPv6 LAN peers today.** As stated in §4.2, `auth.in_subnet` only
  evaluates IPv4 addresses; an IPv6-only LAN cannot use the authenticated-peer
  path in this version of the protocol. This is a known, verified gap (not a
  guess), tracked as a version-2-scope item rather than worked around at the
  application layer.
- **No automatic Core failover.** §9 establishes the mechanism a future opt-in
  auto-failover would use, but this version of the protocol performs no
  promotion without an explicit human or explicitly-configured-policy trigger.
- **No cross-Space data sharing.** A Node, a Client, and a peer Core all belong
  to exactly one Space. This protocol has no notion of a device or a fused
  reading crossing a Space boundary.
- **No remote enable for a killed Node.** Restated from §7 because it is easy
  to assume otherwise: there is no administrative path, in this version or
  planned for a future one without a superseding decision, that turns a
  disabled Node back on without a physical action at the device.

## §14. Conformance

A Wavr component (Core, Node firmware, or a third-party Client) **conforms** to
Wavr Protocol v1 if:

1. It correctly implements every **MUST** and **MUST NOT** in §4 (transport and
   security), §7/`firmware/NODE_PROTOCOL.md` (if it is a Node), and §9 (if it
   participates in Core coordination).
2. It treats every field this document marks as an open/extensible vocabulary
   (modality strings, `role` in mDNS TXT records, `platform`/`compute_tier`
   fallback values) as forward-compatible — an unrecognized value degrades to
   a safe default (§2, §5.2) rather than causing a crash or a security bypass.
3. It reports `protocol_version: 1` on any Capability Manifest it emits, and
   fails closed (§2) on any manifest, node-protocol message, or mDNS
   advertisement declaring a version it does not implement.
4. Where this document defers to `firmware/NODE_PROTOCOL.md` (§7) for
   wire-level detail, it treats that file — not this one — as authoritative for
   byte-level Node framing, and keeps the two in sync rather than letting them
   drift.

A component MAY implement a subset of the *roles* this protocol describes (a
minimal Node need not understand Core coordination at all) without losing
conformance for the surfaces it does implement, provided it never claims
support for a surface (via its Capability Manifest or its own documentation)
that it has not actually implemented to the letter of the relevant MUSTs.
