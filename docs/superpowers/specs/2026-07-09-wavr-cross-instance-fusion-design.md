# Wavr Cross-Instance Fusion & Portable Admin Identity (Design)

Status: DESIGN — pending self-review + Augusto's spec approval before `writing-plans`.
Builds on ADR-0006 (authenticated LAN access), ADR-0008 (MCP-over-HTTP), Wavr Pass
(`2026-07-07-wavr-pass-design.md`, role/scope model), the product taxonomy
(`2026-07-06-wavr-product-taxonomy-design.md`).

## 0. What this is

Today every Wavr instance (Desktop, Core, a future second Core) is a fully independent
backend — its own SQLite, its own `FusionEngine`/`RoomState`, its own registered sensors.
Two instances on the same LAN don't know about each other. Augusto's vision: if you own
Desktop only, or Core only, each works standalone with zero dependency on the other —
but if you own both (or several Cores), they should **complement each other automatically**
once explicitly linked: Desktop's camera and Core's network/BLE sensing feed one unified
per-room confidence, one admin identity works on any of them, and an MCP client sees "the
house" regardless of which instance it's pointed at.

This is **not** federation across the internet or across different LANs (that stays
out of scope, same boundary ADR-0006 already drew). This is same-LAN, explicitly-linked,
peer-to-peer instances — no new central server, no cloud component (except the one
deliberately opt-in exception in §4.3).

## 1. Scope

In scope:
- Peer discovery (mDNS) and mutual peer-pairing between two Wavr instances.
- Cross-instance sensor fusion via a new `RemoteSource`.
- Portable admin identity: a peer-paired instance's root/central identity is honored
  on the other side, extending Wavr Pass's existing role/scope model rather than
  replacing it.
- Credential methods for the portable identity: PIN (extends the existing Core PIN),
  biometric (local-only unlock of the portable secret), and passkey (a Wavr-native
  local keypair + QR transfer by default, with an opt-in W3C/platform-cloud passkey
  path behind an explicit external-connection warning).
- Remote configuration: an admin session on one instance can read/change settings on
  any peer it's paired with, including pushing the same change to multiple peers from
  one screen.
- MCP: no new work — fused state is visible to any MCP client because fusion happens
  inside the instance before MCP reads it.

Out of scope (explicitly deferred):
- Federation across different LANs / the internet (VPN, tunnels, distributed identity)
  — ADR-0006 already puts this in the long-horizon roadmap tier.
- N-way conflict resolution beyond "last write wins, per peer, reported per peer" (see
  §5) — a CRDT-grade merge protocol is not being built here.
- iOS passkey/platform integration specifics — covered generically, not iOS-specific
  (no Mac in this environment to validate against).

## 2. Discovery & mutual peer-pairing

**Discovery:** Desktop starts self-advertising mDNS `_wavr._tcp` the same way Core
already does (commit `3af4787`, Core-launcher/Kotlin side) — Desktop's advertise lives
in the Tauri shell or the Python backend itself (implementation detail for the plan;
functionally identical TXT record shape: `{v, path, role}`, `role=desktop` vs
`role=core`). Every instance also gains an mDNS *browser* for `_wavr._tcp` (today only
Mobile browses; Desktop/Core need to browse each other).

**Pairing is peer-to-peer, not client-to-hub.** It reuses the existing primitive
(rotating 8-digit code + manual out-of-band cert-fingerprint compare — the same UX
already shipped for Mobile↔Core) but runs it **in both directions** in one flow:

1. Admin, from either instance's "Peers" screen, picks a discovered peer.
2. Instance A generates a pair-code; admin reads it off B's screen (or vice versa —
   either side can initiate) and enters it on A. Standard fingerprint-verify happens
   (A shows B's cert fingerprint, admin confirms out-of-band) — this is the existing
   MitM-resistant flow, unchanged.
3. Once A trusts B's cert, A automatically offers ITS OWN pair-code back to B (the
   reverse leg) so B can complete the same verify against A. Both directions must
   complete before either instance treats the other as a peer — a one-sided pairing
   is not a peer, it stays pending and grants nothing.
4. On mutual success, each instance adds the other's identity to its own device store
   (the same table Wavr Pass already uses) with **role=central**, `Device.scopes` left
   `NULL` so it resolves to `DEFAULT_SCOPES["central"]` — which (confirmed in
   `auth.py`) already includes `admin`, `control`, and `mcp`, exactly what remote
   config (§4.4) and fusion (§3) need with zero extra grant logic. No new role name is
   introduced; this is Wavr Pass's existing `central` role, granted peer-to-peer
   instead of only via the loopback dashboard.

**Revocation is unilateral.** Either side can unpair without the other's consent —
this immediately drops the peer from the device store, kills any live sessions/tokens
issued under that pairing, and stops both the fusion feed (§3) and the remote-config
path (§4.4) in that direction. The other side finds out the next time it tries to use
the dead session (401) and shows the peer as unreachable/unpaired, not as an error.

**Independence is preserved by construction:** an instance with zero peers behaves
byte-identically to today. Peer discovery and pairing are additive UI/API surface;
nothing about single-instance operation changes.

## 3. Cross-instance sensor fusion

New `RemoteSource`, modeled directly on the existing `RuViewSource` pattern
(`backend/wavr/sources/ruview.py`): injectable WS-connect coroutine, infinite
reconnect-on-drop, never crashes the `SourceManager`.

- For each peer-paired instance, `SourceManager` registers one `RemoteSource` pointed
  at that peer's `/ws/live` (authenticated with the portable session from §2/§4).
- `RemoteSource` receives the peer's live `RoomState` stream and re-emits each room's
  constituent evidence as local `SensingEvent`s, tagged with their **original**
  modality (a camera reading from Desktop arrives at Core tagged `camera`, not some
  new `remote` modality) — so `fusion.py`'s existing per-modality trust weights
  (`DEFAULT_WEIGHTS`) apply unchanged. **Zero changes to `fusion.py`.**
- Room-name reconciliation: peers may have drawn their house maps independently with
  different room names for the same physical space. V1 requires the admin to
  explicitly map "Desktop's `sala`" → "Core's `living_room`" once during pairing
  (a small confirm step, not automatic name-guessing) — auto-matching identical or
  fuzzy-matched names is a possible follow-up, not required for v1.
- A dead/unreachable peer's contributed events simply age out via the existing
  freshness/staleness decay (`WAVR_SOURCE_FRESHNESS_S`/`_STALE_S`) — no special-case
  error state, consistent with how a dead camera or dead RuView container already
  behaves today.

## 4. Portable admin identity & credentials

### 4.1 What's portable vs. what's inherently local

The **credential material** (a secret or keypair) can be portable; the **unlock
mechanism** for a device's local factor cannot. Three methods:

| Method | Portable? | Mechanism |
|---|---|---|
| PIN | Yes, if admin opts in | Same PIN hash (pbkdf2, existing `pin_store.py`) pushed to peers via §4.4 when set as "shared" |
| Biometric | No, always local | Unlocks THIS device's local copy of the portable secret (Keystore/Secure Enclave/TPM) — fingerprint/face never leaves the device, never portable itself |
| Passkey (local, default) | Yes | Wavr-native keypair, not W3C-standard WebAuthn (LAN IPs/mDNS names don't satisfy browser RP-ID rules) — same challenge-response shape, portable by scanning a QR the source instance generates (reuses the pairing-QR pattern already designed for Mobile) |
| Passkey (cloud, opt-in) | Yes, via platform | Real W3C WebAuthn synced through iCloud Keychain / Google Password Manager — gated behind an explicit toggle with an "this adds an external connection" warning, same disclosure pattern as the narrator's Gemini option |

### 4.2 Local vs. shared, per credential, admin's choice

When an admin sets any credential, they choose **local** (this instance only) or
**shared** (propagate to already-peer-paired instances). This is a per-credential
choice, not a global mode — an admin can have a shared PIN but a local-only biometric
enrollment (which is the only sane biometric state anyway, per §4.1).

### 4.3 The cloud-passkey exception

Wavr's standing invariant is zero cloud egress except explicit, individually-gated
opt-ins (narrator/Gemini, speedtest). Cloud-synced passkeys join that list under the
same discipline: off by default, on only via explicit toggle, warned as an external
connection at the moment of enabling — never silently.

### 4.4 Remote configuration & bulk propagation

Because §2 already grants a peer's identity `central`+`admin` scope, "configure a peer
from here" is **not a new distributed protocol** — it's the portable identity applied
to settings endpoints that already exist (PIN, cameras, house map, connectors, etc.).
The admin console adds:
- A "Peers" panel listing paired instances and their reachability.
- On any settings screen, an optional "apply to: [this instance / peer list with
  checkboxes]" — fires the same authenticated PATCH/POST once per selected peer.
- Per-peer result reporting (§5) — no silent retry queue, no illusion of atomicity
  across peers.

## 5. Failure handling

| Failure | Behavior |
|---|---|
| Peer drops mid-fusion | `RemoteSource` reconnects forever (RuView pattern); its contributed evidence decays via existing freshness/staleness, no error banner, no crash |
| One-sided pairing (only A confirmed B) | Neither side grants trust; pairing stays pending until both legs complete — fail-closed |
| Bulk config push, one peer offline | Per-peer result shown (succeeded / unreachable); admin retries manually later; no background retry queue (avoids silent drift) |
| Unpair | Unilateral, immediate, kills fusion + remote-config + any live peer sessions from that side |
| Room-name mismatch at fusion time (unmapped room) | Peer's events for that room are dropped with a one-time admin-visible notice ("Core is reporting a room Desktop doesn't recognize — map it in Peers") rather than silently fused into the wrong room or silently discarded forever |

## 6. Testing

- `RemoteSource`: mock-testable exactly like `RuViewSource` (injectable connect
  coroutine) — no hardware, no real peer needed.
- Mutual pairing: extends the existing `pairing.py` test suite to cover the two-leg
  handshake and the fail-closed one-sided case.
- Remote config propagation: unit-testable as "peer A offline, peer B online, push
  fires, per-peer result matches reality" using the same injectable-transport style
  already used throughout the codebase.
- Passkey (local): keypair generation + challenge-response is unit-testable without
  hardware; QR transfer content is testable as data (not the camera/scan UX itself).
- Passkey (cloud) and the actual two-real-device bring-up (Desktop + Core on the same
  physical LAN) remain manual, hardware-gated steps — consistent with how every other
  Wavr milestone in this project has shipped (mock-tested code, manual final bring-up).

## 7. What this unlocks for free

Once §2 (peer-pairing) and §3 (fusion) exist, "MCP sees the whole house no matter which
instance you point it at" requires **no MCP-side code at all** — an MCP client attached
to Desktop's stdio/HTTP transport already reads Desktop's `RoomState`, and that
`RoomState` already includes Core's fused-in evidence once they're paired. The same is
true in reverse. This was the original ask that started this design and falls out of
the architecture rather than needing its own implementation.
