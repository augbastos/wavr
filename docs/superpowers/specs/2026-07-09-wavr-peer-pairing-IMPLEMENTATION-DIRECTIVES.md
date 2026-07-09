# Wavr Peer Pairing — Consolidated Implementation Directives (C1 fix + adversarial-sweep hardenings)

Status: authoritative implementation spec for the single fix wave on branch `feat/peer-fusion`
(local/unpushed) before any public push. Combines:
1. **The C1 fix design** — `2026-07-09-wavr-peer-pairing-c1-fix-design.md` (security-architect, fable). AUTHORITATIVE for the reshaped pairing protocol. Reshape in one line: DELETE `/api/peers/exchange` + `PeerExchangeManager`/`PendingExchange`; KEEP `/api/peers/redeem` (now only consumes a screen-displayed code, like `/api/pair`); ADD `POST /api/peers/observe` (wires the dead `remote_cert_fingerprint` → shows the OBSERVED peer cert fingerprint, fixing M1); CHANGE `/confirm` (no `exchange_id`; orchestrates forward + auto-reverse); REPLACE `/finish` with `POST /api/peers/link-back` (authenticated `require_central`, reverse leg bootstraps over the now-authenticated pinned channel). Manual out-of-band 8-digit code entry (mirrors Mobile↔Core). Folds in C2 (persist OUR-device-id-for-them, derived from the authenticated bearer token, never the peer's self-reported echo) + M1 (confirm OBSERVED fingerprint) + I1 (peer_client: connect → verify pin → THEN request).
2. **The adversarial sweep** — 13 confirmed findings (12 new). This doc reconciles them.

---

## A. Findings that EVAPORATE with the C1 reshape (verifiers must CONFIRM they are gone, not fix them)

Deleting `/api/peers/exchange` + `PeerExchangeManager` and replacing `/finish`→`/link-back` removes the unauthenticated-ingestion + stashed-attacker-data surface these depended on:

- **[2] / [10] resource-exhaustion** — unbounded `/exchange` stash → OOM. GONE (no `/exchange`, no `PeerExchangeManager._pending`). ⚠️ Still add a hard cap on `PairingManager._codes` (see B) since `/api/pair-code` still mints codes, though that path is now loopback-admin-only.
- **[3] / [5] SSRF via stashed `requester_base_url` dialed by `/finish`** — GONE (no stash, no `/finish`). ⚠️ The RESIDUAL SSRF surface moves to the new `/confirm`'s `peer_base_url` (operator-supplied) — see D.
- **[6] reverse leg trusts attacker requester fingerprint/base_url** — GONE (`/link-back` binds the reverse credential to the OBSERVED cert of the authenticated caller + the admin-confirmed value, per the C1 design).
- **[8] (C2 + revoke-redirect) attacker-chosen device_id stored as `local_device_id`, revoke hits a victim** — GONE (C2 fix: store OUR-id-for-them derived from the authenticated bearer token, not the peer's `redeemed["device_id"]` echo).
- **[12] `/finish` plants a durable PeerStore row with attacker-controlled base_url/fp/token** — GONE (reshape binds the PeerStore row to admin-confirmed + observed values).

VERIFICATION REQUIREMENT: the red-team re-verify phase must specifically re-attempt each of these against the reshaped code and confirm they no longer reproduce.

## B–E. Findings that PERSIST → MUST be explicitly implemented in this wave (NOT in the C1 design)

### B. Lock the peer control-plane to loopback-root, and stop leaking the observed fingerprint in errors — **[1] [7] [9] (important, priv-esc/info-leak)**
The peer admin routes are gated `require_local` + `require_scope("admin")`. But `require_local` admits ANY authenticated `central` role header-less, and a paired peer IS a `role=central` device whose `DEFAULT_SCOPES` include `admin`. So a REMOTE central peer (or a C1-self-provisioned one) can drive this node's control-plane with zero operator action: `/confirm` (force an outbound TLS dial to an attacker-chosen host + exfil the target's cert fingerprint via the 502 error echo), `/discovered` (LAN enum), `GET /api/peers` (leak other peers' internal IPs + cert fingerprints), `DELETE /api/peers/{id}` (sever this node's links to OTHER peers = mesh DoS). This violates the codebase's OWN `require_root` invariant (app.py `require_root`, ~line 945: an inward-LAN-attack primitive is loopback-ROOT ONLY, "even an authenticated multidevice central peer ... must NOT wield it").
**FIX:**
- Gate `GET /api/peers/discovered`, `POST /api/peers/confirm`, `GET /api/peers`, `DELETE /api/peers/{id}` (and `POST /api/peers/observe`, the new pairing-initiation route) with **`require_root`** (loopback-root only), NOT `require_local`. Only the LOCAL operator initiates/administers pairing; no remote peer ever calls these. This ALSO aligns with the C1 design (pairing is initiated by the local operator reading a code off the peer's screen).
- The ONLY peer-reachable route is the new **`/api/peers/link-back`** — keep it `require_central` (a remote already-authenticated central peer completes the reverse leg here).
- In `/confirm` (and anywhere a peer's HTTP error is surfaced), **strip the observed cert fingerprint / peer response body from the error echoed back to the caller** — return a generic "could not reach peer" 502, never the peer's cert fingerprint or raw response bytes (kill the exfil oracle).

### C. Per-source-IP failed-redeem rate limiting — **[4] [13] (dos)**
`/api/peers/redeem` (kept, unauthenticated in-subnet) shares `PairingManager`'s ONE process-global `_failed` list. An unauth in-subnet host can saturate it with junk attempts and lock out ALL legitimate pairing (device + peer) for everyone.
**FIX:** key the failed-attempt tracking **per source IP** (the handler must pass the caller's `request.client.host` into `redeem`, and `PairingManager` tracks failures per-IP), so one host's junk can't throttle a legitimate redeem from another. Keep the existing global TTL/cap semantics per-IP. Add unit tests: two IPs, one saturates, the other still redeems.

### D. SSRF guard on `/confirm`'s `peer_base_url` — **[5] [3-residual] (defense-in-depth)**
Even with `/confirm` now `require_root` (local operator only, per B), the server must not be coercible into dialing arbitrary internal hosts. Before `peer_client` dials `peer_base_url`:
**FIX:** validate `peer_base_url` — require `https://` scheme; reject loopback (127/8, ::1), link-local (169.254/16), multicast, and any non-in-subnet host (reuse the existing `in_subnet` / `is_lan_ip` helper); ideally require it to match a host actually present in the mDNS discovery result or the caller-selected peer. Reject with 400 before any socket opens.

### E. Bound + off-load blocking peer HTTP — **[11] (important, dos)**
`/confirm` (and the new `/link-back` if it makes any outbound call) invoke `peer_client` (synchronous `http.client`) directly from an `async def` handler → **blocks the event loop**; `peer_client` has only a per-socket timeout (not a total deadline) and does an **unbounded `resp.read()`**.
**FIX:** (1) run every outbound `peer_client` call via `await asyncio.to_thread(...)` so it never blocks the loop; (2) add a total wall-clock timeout to the peer call (not just per-socket); (3) cap the response body read (e.g. `resp.read(MAX_PEER_BODY)`), rejecting an oversized/hung response as a 502. Applies wherever `post_json`/`get_json` are called from a request handler.

---

## Execution notes
- Everything stays DEFAULT-OFF behind `WAVR_PEERS_ENABLED` and byte-identical when off.
- No push — local commits on `feat/peer-fusion` only; Augusto pushes after the wave is green + re-red-teamed.
- The frontend Peers panel must be updated to the manual-code-entry UX (per the C1 design): show the local instance's own code+fingerprint (via `/api/pair-code`) for the OTHER side to read, and provide the code-entry + observed-fingerprint-compare flow for initiating. Remove the auto-`/exchange` call.
- TDD: keep/extend the two-instance routed-transport integration test; it must now prove (a) unpair actually 401s the peer's inbound token, (b) a bare in-subnet host can NO LONGER obtain a central token (C1 closed), (c) `/confirm`/`/discovered`/`/api/peers`/`DELETE` reject a remote central peer (require_root), (d) `/link-back` accepts the authenticated central peer.
- After implementation: re-run the adversarial sweep (the A-list must be gone; B–E must be closed) + a fresh whole-branch review, then hand to Augusto for push.
