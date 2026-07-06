# Companion self-registers for network presence — Design (mobile side)

**Date:** 2026-07-06
**Status:** Approved (Augusto, 2026-07-06)
**Scope:** Wavr Mobile companion (`dev.wavr.mobile`, worktree `C:\IA\wavr-phase1`, branch
`mobile/phase-1`). Mobile side ONLY — the `/api/presence/*` endpoints are the Core
terminal's job (see "Core contract — coordination"). All mobile work lands in the single
mobile-specific file `mobile/src/wavr-mobile-shim.js`; **zero `index.html` edits** (shim
invariant, same as the consent toggle + sensor pill).

## Goal

When a paired companion is on the same Wi-Fi as its Core, the Core should report
"[owner] is home" through its **network** presence source — WITHOUT the user hunting a MAC
address. The phone registers itself.

**Why the Core resolves the MAC, not the phone:** Android 10+ returns a constant fake
Wi-Fi MAC (`02:00:00:00:00:00`) to apps, so the companion CANNOT read or report its own
MAC. Instead: on any authenticated LAN request the Core sees the companion's **source IP**
and resolves IP→MAC from its own ARP table. So the mobile side only needs to (a) be on the
LAN and (b) tell the Core "register me as presence of [label]".

## Surface & states

A new **`presence` pill** injected into `index.html`'s header `.status-pills` on EVERY
paired device (beside the consent pill, via the same `injectConsentPill`-style hook; no-op
if the row is absent). Tapping it opens a dedicated overlay panel (styled like `showNode`)
containing:

- a **name/label** text field (the person this phone counts as),
- an **on/off toggle**,
- a live **status** line,
- an **unregister** action.

Pill visual states (dot colour + short text), painted from the synchronous boot cache:

| State | Meaning | Copy |
|---|---|---|
| `off` | not registered | "count presence" |
| `pending` | POST in flight / retrying | "registering…" |
| `on` | Core confirmed the binding | "home as \<name\>" |
| `error` | Core can't do network presence | "no network presence" |
| `disabled` | consent is RED (see below) | greyed, not tappable |

## Persistence

Keystore-backed secure storage key `wavr.presence` = JSON `{ enabled: bool, label: string }`,
read SYNCHRONOUSLY at boot (like the token/consent caches) so the pill paints the correct
state with no flash. The label is not a secret but is **never logged**, consistent with the
file's discipline (token/fp/consent are never logged either).

New in-memory caches populated during the `ready` gate: `_presenceEnabled`, `_presenceLabel`,
plus the label default rule: pre-fill the field from `_presenceLabel` if present, else blank
with a placeholder (e.g. "e.g., Augusto"). Free text, per-device.

## Flows

### Register (toggle ON, or field-confirm)
1. `POST /api/presence/register-companion { label }` via `netFetch` (pinned HTTPS, `Bearer`
   the device's own token) — mirrors `postConsent` exactly.
2. `200 { mac_registered: true, label, mac_prefix }` → state `on`; status line
   "home as \<name\> · MAC \<mac_prefix\>"; persist `{ enabled: true, label }`.
3. `200 { mac_registered: false, ... }` (Core has no ARP / no root) → **fail-closed**: state
   `error`, message "this Core can't do network presence", flip the toggle back OFF, persist
   `{ enabled: false }`. Never leave the UI claiming a registration the Core didn't make.
4. Network error / non-2xx → state `pending` + retry using the `scheduleConsentRetry`
   pattern (generation-guarded, 5s backoff). NEVER wipes the token, NEVER forces re-pair —
   token wipe stays bound to 401/403 on the read/ws/telemetry paths only (unchanged).

### Unregister (toggle OFF)
`POST /api/presence/unregister-companion {}` → the Core removes the MAC→label binding for the
requesting IP's resolved MAC. State `off`; persist `{ enabled: false }`. A failed unregister
retries (same pattern) but the LOCAL state goes off immediately (the phone stops asserting).

### Re-assert (keep the binding fresh)
The randomized Wi-Fi MAC is per-SSID stable but can rotate (network re-join / periodic
randomization); the IP changes on DHCP renewal. So while `enabled` and consent ≠ RED, re-POST
`register-companion` on:
- (a) app foreground — `visibilitychange` → visible (the hook already wired for `detectRole`),
- (b) WS (re)connect (the `netWebSocket.openSocket` success path already calls `detectRole`),
- (c) a low-frequency periodic — every **30 min** while foreground + registered.

**Never in background:** a viewer has no background service, and the Core already sees the
phone's IP on every authenticated request regardless. Re-assert is coalesced/guarded so an
extra fire is a harmless no-op (mirrors `detectRole`'s `_roleInFlight` guard).

## Consent interaction (decided)

Network-presence registration IS a contribution, so it is bound to the consent axis:

- Going **RED** (via `changeConsent`/`applyConsentLocal`) auto-fires `unregisterPresence()` at
  the SAME point that already calls `stopSensor()` for RED, and **disables** the presence pill
  while RED.
- **Yellow / green** re-enable the pill. Re-enabling does NOT auto-register — the user must
  toggle back on (an explicit re-grant of this specific contribution).
- The stored `{ enabled }` is preserved across a RED excursion only as intent; while RED the
  effective state is `disabled` and no assertion is sent.

## Edge — off the Wi-Fi

Nothing to do on the mobile side: the Core sees the ARP entry expire → reports the label
**away** naturally. The phone does not need to detect network loss for presence purposes.

## Core contract — coordination (Core terminal implements)

These are the cross-terminal contract items this mobile design depends on. The mobile side is
built against them; the Core terminal owns the server implementation.

1. **`POST /api/presence/register-companion { label }`** → `200 { mac_registered, label,
   mac_prefix }`. The Core reads the request's source IP, resolves IP→MAC via ARP, stores
   MAC→label as a known presence device, and the network source then reports `label` home
   when that MAC is on the LAN / away when it expires from the table.
2. **Failure shape (must confirm):** when the Core CANNOT resolve IP→MAC (no root / no ARP
   access), it MUST return `200 { mac_registered: false, reason }` — NOT a 500. The mobile
   fail-closed path depends on a clean boolean, not an exception.
3. **`POST /api/presence/unregister-companion {}`** (NEW — not in the original ask): removes
   the MAC→label binding for the requesting IP's resolved MAC. Needed for toggle-OFF and for
   the RED-consent auto-withdrawal.
4. **Binding persistence:** the Core keeps MAC→label until an explicit unregister or its own
   staleness policy; the mobile re-assert only refreshes it (idempotent re-register).

## Testing (mobile side)

- Register happy-path → pill `on`, label persists, survives a re-boot (Keystore).
- `mac_registered:false` → fail-closed (toggle returns OFF, correct message, nothing persisted
  as enabled).
- Consent → RED → `unregister-companion` fired once + pill disabled; back to yellow/green
  re-enables without auto-registering.
- Re-assert on foreground/reconnect/periodic does not duplicate or spin (generation-guard).
- Network error → `pending` + retry, token never wiped.
- Grep-assert the token/label never reach `console.*`.

## Invariants (carried from the shim + Wavr privacy model)

- **Zero `index.html` edits** — all chrome is shim overlay/pill injection (like consent/sensor).
- **Local-only** — the sole network peer stays the paired Core; register/unregister go over the
  pinned transport with the device token.
- **Fail-closed** — never show a registration the Core didn't confirm; RED withdraws.
- **Never log** the token or the label.

## Out of scope

- The Core-side ARP resolution, endpoint code, and network-source wiring (Core terminal).
- Any background/always-on presence assertion (viewer has no background service).
- Room-level presence — network presence is whole-home ("[owner] is home"), never a room; a
  moving phone is not a room anchor.
