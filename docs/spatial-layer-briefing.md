# Wavr — the spatial layer: implementation briefing

Written before touching code, from a full read of the tree at `feat/spatial-layer`
(base `0e97d93`, tests green).

## 1. What exists

Wavr is **much further along than a "presence dashboard"**. 64k lines of Python
across ~90 modules, a 17k-line single-file frontend, a Tauri v2 desktop shell,
an undocumented Android kiosk launcher, complete-but-never-compiled ESP32
firmware, and a live Cloudflare Pages endpoint (`wavr-diag`).

Already real and load-bearing:

- **Sensing + fusion.** `FusionEngine`, `SensingEvent`/`Target`/`RoomState`,
  `SourceManager`, six source classes, a precision ladder, honest count/unknown
  semantics. This is the crown jewel and is not touched by this work.
- **LAN mode.** `WAVR_MULTIDEVICE` already relaxes loopback-only into
  "in-subnet **and** valid bearer token", with self-signed TLS, operator-eyeball
  cert-fingerprint verification and a per-device token store.
- **Device auth.** `DeviceStore` roles `central|user|agent|guest`, "Wavr Pass"
  named scopes, per-tool MCP allow-lists, revocation, guest expiry, tri-color
  consent.
- **Pairing.** Four distinct flows: 8-digit rotating code + QR, approve-on-Core
  with Bluetooth-SSP-style numeric comparison, peer link-back, node enrollment.
- **Nodes.** A real Node runtime contract: enrollment codes, per-node tokens,
  TOFU cert pinning, remote-OFF-never-ON kill switch, telemetry anti-replay.
- **Discovery.** ARP, mDNS, SSDP, NetBIOS, SNMP, DHCP fingerprinting, ONVIF
  probing, OUI taxonomy, a 203-device catalog, and `net_doctor`'s honest cause
  discrimination.

## 2. What can be reused as-is

Everything above. The new architecture is a **layer on top**, not a rewrite.
Specifically:

- `DeviceStore` stays the credential table. Device *auth role* is a real,
  well-tested concept — it just isn't the same thing as a person's role or a
  device's job, and the new model stops conflating them.
- `NodeStore` already is the Node registry. It gains a capability manifest.
- `PeerStore` already is Core-to-Core plumbing. It gains a Space and an epoch.
- `netinventory` already produces the raw material the Discovery Inbox needs.

## 3. What must change

| Gap | Why it blocks the goal |
|---|---|
| **No Space** | Everything is implicitly "this house". Nothing can be named, joined, or transferred. |
| **Person ≠ device** | `role=central` means both "this credential may administer" and "this box is a hub". You cannot express "Alex is Owner, and *these four* of his devices are his". |
| **No capability manifest** | Nothing can answer "what should this device become?", so every setup is manual. |
| **No device function axis** | Core / Node / Client are conventions, not data. A machine cannot declare it is all three. |
| **Config is 100% env vars** | This is the single biggest blocker. §45's acceptance test fails *by construction*: turning on LAN mode, naming the instance, enabling nodes — all require editing `.env`. |
| **No first-run flow** | The app boots straight into the dashboard. There is no "create a Space". |
| **No Discovery Inbox** | Discovery output goes to a network table, not to an actionable queue. |
| **No install surface** | No website, no installer, no CI artifact, no release asset. Install = clone + pip. |
| **Stale docs** | `NODE-ONBOARDING.md`'s status table is wrong on all four rows; `mcp-connect.md` says a shipped feature is on a branch; ADR-0002 invariants 1 and 7 have been outgrown without amendment; three ADRs cite a deleted ROADMAP. |

## 4. Migrations required

Additive only, same idiom the codebase already uses (PRAGMA-guarded
`ALTER TABLE`, `CREATE TABLE IF NOT EXISTS`). No existing table is dropped or
rewritten, so an existing `wavr.db` keeps working:

- new tables: `space`, `people`, `person_devices`, `device_functions`,
  `device_capabilities`, `cores`, `discoveries`, `settings`
- `devices` gains nothing — the legacy `role` column stays authoritative for
  auth, and the new person/function axes hang off `device_id`.
- a **legacy adoption pass**: on first boot with an existing db and no Space,
  synthesize a Space, mint an Owner, and map every existing `central` device to
  that Owner with function `client`. Nothing is lost and nothing is guessed.

## 5. Risks

1. **Privacy invariants are load-bearing and easy to erode by accident.**
   Onboarding wants to make LAN mode one click; ADR-0002 made it deliberately
   hard. Resolution: the setting is UI-reachable but behind an explicit,
   plain-language consent step that states what changes — "discover
   aggressively, activate conservatively" (§15), never a silent default.
2. **A settings store that overlays env can become an auth bypass.** Resolution:
   the store is *additive and bounded* — a fixed allow-list of keys, none of
   which can weaken a security gate that env could not already weaken, and env
   always wins over the store so an operator's `.env` is never overridden.
3. **Scope.** This prompt is a multi-quarter program. The honest failure mode is
   a broad, shallow, untested change to a mature public repo. Resolution below.
4. **GitHub Actions quota** is at 7.2% with a projection that overruns the
   month. A Windows/Android build matrix on every push would burn it.
   Resolution: release artifacts build on **tag only**.
5. **Android-as-real-Core** (§9) needs an Android toolchain, a device, and
   signing material. It cannot be verified here and must not be claimed.

## 6. Implementation order

1. Capability manifest + host scan + recommendation *(pure, no deps)*
2. Space / People / DeviceFunctions / Cores stores + migrations
3. Settings store (kills the `.env` requirement)
4. Discovery Inbox
5. HTTP API for all of the above
6. Wire into `app.py` behind the existing gates
7. Tests at every layer
8. First-run wizard in the frontend
9. Installers (Windows / Linux / Pi / Docker)
10. Installation website
11. `WAVR-PROTOCOL.md` + docs truth pass + README
12. CI release pipeline (tag-gated)

## 7. Explicitly out of scope for this pass

Stated up front so nothing is silently claimed later:

- **Android Core runtime** (embedding Python on Android). Groundwork only.
- **Compiling the ESP32 firmware** — no toolchain on this machine.
- **Code signing / Play Store** — owner-only credentials.
- **Deploying the website** — it is built and deploy-ready; publishing a public
  surface needs the owner's explicit go-ahead.
- **Physical multi-device verification** — the five-device journey can be
  *exercised* against the API, not *performed* on the real hardware.
