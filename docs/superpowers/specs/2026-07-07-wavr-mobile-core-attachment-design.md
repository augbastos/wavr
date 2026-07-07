# Wavr Mobile ↔ Core — discovery + one-time pair + enter/exit + auto-presence — Design (mobile side)

**Date:** 2026-07-07
**Status:** Approved (Augusto, 2026-07-07)
**Supersedes:** `2026-07-06-companion-network-presence-registration-design.md` (presence is no
longer a standalone pill — it is folded into the enter/exit control below).
**Scope:** Wavr Mobile companion (`dev.wavr.mobile`, worktree `C:\IA\wavr-phase1`, branch
`mobile/phase-1`). Mobile side ONLY. The Core (appliance) already announces itself on the LAN
via mDNS and exposes every endpoint used here (confirmed by the Core terminal). All mobile
logic lands in `mobile/src/wavr-mobile-shim.js` plus vendored assets (jsQR for Phase 2) and a
native mDNS-browse dependency; **zero `index.html` edits** (shim invariant).

## Goal

Make the phone **attach to the Wavr Core** (the appliance), not to a notebook — with a
consumer-grade flow: find the Core on the LAN, pair ONCE with a one-time code, then a
persistent per-device token means every day is a single **enter/exit** toggle (Spotify-Connect
style). Entering also lights up the owner's **network presence** on the Core automatically.

**Why the Core resolves the MAC:** Android 10+ returns a constant fake Wi-Fi MAC
(`02:00:00:00:00:00`) to apps, so the phone CANNOT read/report its own MAC. On any
authenticated LAN request the Core sees the companion's **source IP** and resolves IP→MAC from
its ARP table. The app only opts in with a **label**; it never sends a MAC.

## The unified control — the consent toggle IS enter/exit

The green/yellow/red consent control that already exists in the shim becomes the single
enter/exit + presence control (Augusto's mapping, 2026-07-07). No new separate toggle.

| Level | Connection | Presence | Contribution |
|---|---|---|---|
| 🟢 **GREEN** | ENTER — connect WSS | `POST register-companion {label}` (named) | full |
| 🟡 **YELLOW** | ENTER — connect WSS | `POST register-companion {label}` (present) | limited / minimal |
| 🔴 **RED** | EXIT — close WSS | `DELETE register-companion` (presence gone) | none |

- Existing gestures unchanged: **tap** cycles green→yellow→red→green; **hold 2s** → red (fast
  exit / GDPR easy-withdrawal). The token is ALWAYS kept — re-entering is one tap, no code.
- **Deliberate behavior change (record it):** today RED on a viewer does not cut viewing; now
  **RED = a real EXIT** — it closes the WSS, so the device stops viewing too. That is the
  Spotify "disconnect". Consistent with "sair".
- Boot: the stored level drives auto-attach. Last GREEN/YELLOW + token → auto-connect +
  register on launch (persistent attachment). Last RED → stay out.

### Connect/disconnect lever (KEY implementation risk)

`index.html`'s `CompanionProvider` owns the live WS, constructed from the token it reads
SYNCHRONOUSLY via `WAVR_MOBILE.tokenGet()`. The shim drives connection WITHOUT editing
index.html by reusing the existing token-visibility mechanism plus socket control:

- **RED (exit):** set an internal `_attached = false`; close any open sockets
  (`WavrNet.closeSocket`); make `netWebSocket()` return an immediately-closed socket so
  index.html's reconnect loop keeps getting dead sockets and stays down; `tokenGet()` returns
  null so a reload lands on the inert NullProvider. Fire `DELETE register-companion`.
- **GREEN/YELLOW (enter):** `_attached = true`; `tokenGet()` returns the token; nudge a
  reconnect (index.html retries on its ~2s timer; `location.reload()` for immediacy, mirroring
  the `detectRole` capability-flip reload). Fire `POST register-companion {label}`.

This lever is the riskiest integration point and MUST be verified on-device against the actual
provider reconnect/`NullProvider` behavior. The robust fallback (token-hide + reload) uses only
existing shim mechanisms — no index.html edit.

## Discovery

1. **mDNS (`_wavr._tcp`)** via a native browse dependency (`capacitor-zeroconf` / Android NSD),
   invoked from the shim (null-guarded like `WavrSensor` — absent plugin degrades to manual).
   Each hit: `name` (e.g. "Wavr Core"), resolved `host`+`port` (8000), TXT `{v=1, role=core,
   path=/?core}`. Filter `role=core`.
2. **"Choose your Core" screen:** an overlay listing discovered Cores (name + host:port). Tap a
   Core → pairing. Buttons: **Scan QR** (Phase 2) and **Enter IP manually** (existing
   `showSetup`).
3. Discovery is best-effort; manual entry and QR are always available.

## Pairing (one-time code → persistent token)

Three converging paths, all ending at `POST /api/pair`:

- **A — QR (recommended, Phase 2):** scan `wavr://pair?v=1&h=<host>&p=<port>&fp=<sha256-hex>&
  c=<code>` → set `_base`, probe the cert, **verify probed fp === QR fp** (the automatic MitM
  gate — replaces typed last-6), pin, `POST /api/pair {code, device_name}`. Zero typing.
- **B — mDNS pick (no camera, Phase 1):** tap a discovered Core → set `_base` from host:port →
  existing `showVerify` (probe + type last-6 from the admin's minting screen) → existing
  8-digit code entry (`#companionPair`) → `POST /api/pair`.
- **C — manual IP (fallback):** existing `showSetup` → `showVerify` → code.

On success store `{host, port, pinnedFp, token, coreName, label}` in Keystore-backed secure
storage. The token is persistent; the code is first-time only. `coreName` comes from the mDNS
TXT / QR / a manual default. `device_name` on `/api/pair` = e.g. `"Wavr App · <model>"`.

## Auto-presence

Bound to the enter/exit levels above:

- **Enter (green/yellow):** `POST /api/presence/register-companion {label}` over the pinned
  transport (Bearer the device token) — the Core resolves source-IP→MAC and lights the label
  home. Response `{mac_registered, label, mac_prefix}`.
- **Fail-closed:** `mac_registered:false` (Core has no ARP/root) → show "this Core can't do
  network presence"; do not claim presence. Never a 500-driven crash — the Core returns the
  boolean.
- **Exit (red):** `DELETE /api/presence/register-companion` → presence disappears.
- **Re-assert** (green/yellow) — the randomized MAC is per-SSID stable but rotates; the IP
  changes on DHCP renew — re-`POST register` on: app foreground (`visibilitychange`), WS
  (re)connect, and a low-frequency periodic (**30 min**) while entered. Coalesced/guarded
  (mirrors `detectRole`'s `_roleInFlight`); never in background.
- **Off Wi-Fi:** nothing to do — the Core's ARP entry expires → reports the label **away**.

## Label & status

- **Label** ("your name on this device") captured during pairing; free text; persisted.
- **Status chip:** a tappable line "Connected to [coreName] as presence of [name]" (green/
  yellow) / "Out" (red). Tapping it opens a **details overlay**: edit label, show coreName,
  and **unpair** (the destructive token-wipe, distinct from red/exit which keeps the token).
- The consent control keeps tap=cycle / hold=exit for the level; the status chip is the
  separate affordance for details (hold is already withdrawal, so details do not overload it).

## QR display (both, phased)

- **Bootstrap — Core's own dashboard** (coordinate frontend/Core): the Core renders the QR from
  `{host, port, cert_fingerprint, rotating code}`, refreshing as the code rotates, using the
  vendored MIT QR encoder. Works for the FIRST device without any other paired device.
- **Layer 2 — admin device "add device" QR** (mobile admin): an already-paired admin device
  fetches a fresh pair code from the Core (pinned, admin token) + already holds host/port/fp →
  renders the QR in-app. Needs a vendored QR ENCODER in the app + the Core allowing
  admin-authenticated pair-code issuance over LAN (contract item below).

## Constraints

Android 10+ cannot read its own MAC (`02:00:...`) → the Core resolves by source IP; the app
NEVER reports a MAC. If the Core cannot resolve (no ARP/root) → the fail-closed message above.

## Core contract (all endpoints already exist per the Core terminal)

- `POST /api/pair-code` (admin mints → `{code, cert_fingerprint}`).
- `POST /api/pair {code, device_name}` (redeem → persistent per-device token).
- `POST /api/presence/register-companion {label}` → `{mac_registered, label, mac_prefix}`;
  `DELETE /api/presence/register-companion` (withdraw).
- `GET /ws/live?ticket=…` (via `POST /api/ws-ticket`).
- mDNS service `_wavr._tcp` port 8000, TXT `{v=1, role=core, path=/?core}`.

**Coordination still needed:** (a) confirm `register-companion` returns the `mac_registered`
boolean (fail-closed depends on it, not an exception); (b) for Layer-2 QR — admin-authenticated
pair-code issuance over the LAN channel (not loopback-only).

## Native dependencies (coordinate with capacitor-shell-engineer)

- **Phase 1:** `capacitor-zeroconf` (mDNS browse). Null-guarded so its absence degrades to
  manual/QR, never a crash.
- **Phase 2:** `CAMERA` permission (AndroidManifest + runtime) + `getUserMedia` in the WebView
  (androidScheme `https` → secure context). QR decode is web-pure (vendored **jsQR**, MIT) —
  no ML Kit, no Play-Services model fetch → zero-egress trivially; frames live in an in-memory
  canvas only, never stored/transmitted (ADR-0002). Ships only after an egress sign-off from
  privacy-compliance-license-auditor.

## Phasing

- **Phase 1 (MVP, fastest value):** mDNS discovery + manual pair (paths B/C) + consent-as-enter/
  exit + auto-presence + re-assert. Attaches the tablet + S25 to the Core with the Spotify-style
  toggle using the Core's CURRENT build — no camera work. Resolves the tablet stuck at setup.
- **Phase 2:** QR scan (path A) + the Core rendering the bootstrap QR → removes the remaining
  first-pair friction (last-6 + code typing).
- **Phase 3:** admin "add device" QR (Layer 2).
- mDNS-less manual entry stays a permanent fallback; QR stays the reliable consumer path.

## Testing (mobile side)

- Discovery lists Cores from `_wavr._tcp`; absent plugin → manual still works.
- Pair via mDNS-pick (B) and manual (C) → persistent token, survives re-boot (Keystore).
- GREEN → WS connects + `register-companion` fired; status shows coreName + label.
- YELLOW → connected + registered (limited); GREEN↔YELLOW does not thrash the socket.
- RED → WS closes (stops viewing) + `DELETE register-companion`; token kept; re-GREEN one-tap
  re-attaches with no code.
- `mac_registered:false` → fail-closed message, no false "present" claim.
- Re-assert on foreground/reconnect/30-min does not duplicate or spin.
- Boot with stored GREEN auto-attaches; stored RED stays out.
- Token + label never reach `console.*`.
- Phase 2: QR auto-pair happy-path; **fp mismatch → hard-fail** (MitM screen), never silent.

## Invariants

- **Zero `index.html` edits** — all chrome + the connect/disconnect lever go through the shim
  (token-visibility + socket control + vendored assets).
- **Local-only** — the sole peer is the attached Core, over the pinned transport with the token.
- **Fail-closed** — never show presence/attachment the Core didn't confirm; RED truly exits.
- **Never log** the token or the label.

## Out of scope

- Core-side ARP resolution, mDNS advertising, endpoint code (Core terminal).
- Room-level presence — network presence is whole-home ("[owner] is home"); a moving phone is
  not a room anchor.
- Background/always-on attachment (a viewer has no background service).
