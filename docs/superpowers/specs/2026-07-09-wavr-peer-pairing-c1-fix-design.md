# Wavr Peer Pairing — C1 Fix: Manual Out-of-Band Code Entry (Design)

Status: DESIGN — implementation-ready. Supersedes the pairing handshake shipped on
`feat/peer-fusion` (commits `0053c7c`, `142ac04`, `08bc2e1`). Branch stays LOCAL, no push.
Fixes **C1** (unauthenticated central-credential vend), and folds in **C2** (unpair does
not revoke inbound credential), **M1** (fingerprint confirmed from a JSON body, not the
observed cert), and **I1** (credential transmitted before the pin is verified).

Builds on: ADR-0006 (authenticated LAN access), Wavr Pass role/scope model
(`auth.DEFAULT_SCOPES["central"]` = `admin`+`control`+`mcp`), the design spec
`2026-07-09-wavr-cross-instance-fusion-design.md` section 2, and the plan
`2026-07-09-wavr-peer-discovery-pairing.md`.

---

## 0. Threat model (state it before controls)

- **Asset:** a `role=central` credential on a Wavr hub. `central` = admin + control + mcp
  (`auth.py:130-133`) = full remote administration of a home-surveillance instance.
- **Trust boundary:** loopback (root, the physical operator at the machine) is trusted;
  the LAN /24 is **semi-trusted** — anything on the same subnet can reach the in-subnet-
  exempt onboarding endpoints (app.py:882) without a token.
- **Adversary (primary):** a bare in-subnet host — a curious housemate's laptop, a guest
  on the Wi-Fi, or a compromised IoT device on the same /24. It can send arbitrary HTTP
  to the exempt endpoints but **cannot see the operator's trusted screen** and **cannot
  read loopback-only responses**. Its goal: obtain a central credential.
- **Adversary (secondary):** an active LAN MitM (ARP-spoof) sitting between the two hubs
  during pairing, trying to substitute its own TLS cert and/or capture a credential in
  flight. Bidirectional on the LAN in practice.
- **Golden invariant being defended:** "your home, understood, without giving it away."
  A central-credential hole is an identity-level failure, not a bug — it must fail CLOSED.

The fix mirrors the **Mobile-to-Core pairing that already ships** in this repo (central
displays a rotating 8-digit code on its trusted screen; the operator reads it off-screen
and types it into the joining device; the cert fingerprint is compared out-of-band). Both
Core and Desktop have screens, so the exact same human ceremony works peer-to-peer.

---

## 1. Root causes (verified against source)

| ID | File:line | Defect |
|---|---|---|
| **C1** | `api_peers.py:57-76` (`/exchange`) + `:78-88` (`/redeem`), exempt at `app.py:882` | `/api/peers/exchange` is UNAUTHENTICATED, in-subnet, and **mints + returns a live `central` pairing code** (`pairing.mint_code("central")` -> response body). Any in-subnet host calls `/exchange` (gets a code), then `/redeem` (gets a central token) -> full admin, no human, no fingerprint. The `pairing.redeem` rate-limiter (`pairing.py:85-89`, FAILED guesses only) never engages because the code is **handed out**, not guessed. |
| **C2** | `api_peers.py:137-138`, `:187-188` (persist `local_device_id = redeemed["device_id"]`) + `:203-204` (unpair) | `redeemed["device_id"]` is the id in the **peer's** DeviceStore (their id for us). `device_store.revoke(peer.local_device_id)` on unpair therefore revokes a row that does not exist in OUR store -> **no-op**. The peer's inbound token survives an unpair. |
| **M1** | `api_peers.py:71-74` returns `fingerprint` in JSON; frontend `index.html:4051` shows `exch.fingerprint` | The fingerprint the human confirms is **self-reported by the peer in a JSON body**, not the fingerprint of the cert the pinned TLS connection actually presented. A MitM terminating the TLS can echo the real peer's fingerprint in the body while presenting its own cert. `tls.remote_cert_fingerprint` (`tls.py:143`) exists precisely to observe the real cert but is **wired nowhere**. |
| **I1** | `peer_client.py:64-73` | `conn.request(method, path, body=body, headers=headers)` **transmits the bearer token / pairing code to the socket** before `conn.sock.getpeercert()` is read and the pin is checked. A MitM captures the credential, then we abort — too late. |

---

## 2. The reshaped protocol (one manual code + authenticated reverse bootstrap)

Naming: **A = initiator** (operator drives A's loopback dashboard). **B = target**
(operator reads B's trusted screen). Goal: end with A and B mutually trusting each other
as `central` peers.

### Recommended shape (and why, vs. two manual codes)

**One manual code, typed on the initiator; the reverse direction bootstraps automatically
over the now-authenticated, fingerprint-pinned channel.**

The single human ceremony proves two things at once: the operator holds **local admin on
A** (they are driving A's `require_local` dashboard) **and** they can **see B's screen**
(they typed B's on-screen code and confirmed B's on-screen fingerprint). That is the full
authorization needed for mutual trust — a second code typed on B would re-prove the same
human, adding friction without adding a distinct trust proof. Because everything after the
forward leg rides A's channel to B — which is pinned to the **one** fingerprint the human
confirmed out-of-band — the reverse credential can be minted by A and pushed to B safely
(see section 6 red-team argument). Two-codes-both-ends (the symmetric alternative) is
**more** human friction AND more machinery (each side's `redeem` would have to correlate a
locally minted device id to a peer row), so it loses on both axes. Recommended: **single
code.**

### Endpoints after reshape

Public router (`build_peers_public_router`, unauthenticated in-subnet):
- **REMOVE `POST /api/peers/exchange` entirely.** This is the C1 root — nothing may
  network-vend a central code.
- **KEEP `POST /api/peers/redeem {code, requester_name} -> {device_id, token}`**,
  byte-identical to today (`api_peers.py:78-88`). It is now safe for the same reason
  `/api/pair` is safe: it only ever consumes a code that was **displayed on a trusted
  screen** by loopback-admin `/api/pair-code`, never one vended over the network.

Admin router (`build_peers_admin_router`, `admin_deps` = `require_local` +
`require_scope("admin")`):
- **KEEP `GET /api/peers/discovered`** (mDNS browse) — unchanged.
- **NEW `POST /api/peers/observe {peer_base_url} -> {fingerprint}`** — wires the dead
  `tls.remote_cert_fingerprint(host, port)` so A's backend observes B's **live** cert and
  the UI displays THAT (M1). Loopback-admin only.
- **CHANGE `POST /api/peers/confirm`** — new body `{peer_base_url, peer_name, peer_code,
  peer_fingerprint}` (no `exchange_id`). Runs the forward + reverse legs (section 3),
  pinning `peer_fingerprint` on every outbound call.
- **REPLACE `POST /api/peers/finish` with `POST /api/peers/link-back {token, base_url,
  fingerprint, name}`**, still gated by `finish_deps = require_central`. This is the
  reverse-leg completion, called by A (already authenticated as central on B) to hand B
  the credential B will use to call A (section 3).
- **KEEP `GET /api/peers`** and **`DELETE /api/peers/{id}`** — unpair logic in code is
  unchanged, but now **correct** because `local_device_id` finally holds our-id-for-them
  (C2, section 4).

Eliminated machinery: `PeerExchangeManager` + `PendingExchange` (`peers.py:165-233`) are
**deleted** — the reverse leg no longer stashes the initiator's half and waits; it is
driven synchronously by A. This also removes the "`exchange_id` as an unbound capability"
hand-wave (`api_peers.py:161-170`). Fewer moving parts, smaller attack surface.

---

## 3. Step-by-step flow (with the exact store writes)

Precondition (human, out-of-band):
- On **B**'s dashboard -> Peers panel -> "Show this hub's pairing code": B calls its own
  existing `POST /api/pair-code {role:"central"}` (loopback-admin, `app.py:1949-1964`) and
  **displays on B's trusted screen** `code_B` (8-digit) and `fp_B` (B's cert fingerprint).
  Nothing is sent to any network caller.
- Operator walks to **A**'s dashboard -> Peers -> picks discovered peer B.

Step 1 — **observe** (M1): A calls its own `POST /api/peers/observe {peer_base_url:B}` ->
backend runs `remote_cert_fingerprint(B_host, B_port)` -> `fp_B_observed`. UI shows
`fp_B_observed` and asks the operator to compare it against `fp_B` on B's screen. Operator
confirms and types `code_B`.

Step 2 — **forward leg** (A redeems B's code over a pinned channel): A's `confirm` handler
calls, via the I1-fixed `peer_client` (connect -> verify pin -> request):

```
redeemed = post_json(B_base_url, "/api/peers/redeem",
                     {"code": code_B, "requester_name": A_name},
                     pinned_fingerprint=fp_B_observed)   # peer_fingerprint from the body
```

- **On B:** `pairing.redeem(code_B, A_name)` -> mints a **central** `Device` in B's store,
  `b_did_for_a`, with token `a_token_for_b`. Returns `{device_id: b_did_for_a,
  token: a_token_for_b}`. (No PeerStore write on B yet.)
- **On A:** now holds `a_token_for_b` (A's credential to call B). A writes its PeerStore
  row for B: `peer_id_A = peer_store.add(name=B_name, base_url=B_base_url,
  cert_fingerprint=fp_B_observed, local_device_id=<set in step 3>, token=a_token_for_b)`.

Step 3 — **reverse bootstrap** (A mints B's inbound credential LOCALLY, pushes it over the
authenticated+pinned channel):
- **On A:** `a_did_for_b, b_token_for_a = device_store.add(B_name, "central")` — a central
  device in **A's own** store. `a_did_for_b` is **A's id for B** -> store it as A's
  PeerStore `local_device_id` for B (the row from step 2). This is the id A must revoke to
  kill B's inbound access (C2).
- A calls:

```
post_json(B_base_url, "/api/peers/link-back",
          {"token": b_token_for_a, "base_url": A_base_url,
           "fingerprint": fp_A_self, "name": A_name},
          token=a_token_for_b,           # authenticates A to B as central
          pinned_fingerprint=fp_B_observed)
```

- **On B (`link-back`, `require_central`):** B re-verifies the bearer token
  (`device_store.verify(a_token_for_b)`) -> `b_did_for_a` — the cryptographically-proven id
  of the caller's device on B (NOT a value A self-reported). B writes its PeerStore row for
  A: `peer_store.add(name=A_name, base_url=A_base_url, cert_fingerprint=fp_A_self,
  local_device_id=b_did_for_a, token=b_token_for_a)`.
  - `local_device_id = b_did_for_a` is **B's id for A** -> the id B must revoke to kill A's
    inbound access (C2). Derived from the token, so A cannot forge it to sabotage
    revocation.
- On reverse-leg failure (B unreachable at link-back): A revokes the just-minted
  `a_did_for_b` (hygiene — B never received the matching token, so it is dead weight) and
  returns `{peer_id, reverse_leg_ok: false}`; the operator retries the pair. **No silent
  rollback** — same rule the spec already applies to bulk config push.

### End state (both stores, both directions)

| | A.DeviceStore | A.PeerStore[B] | B.DeviceStore | B.PeerStore[A] |
|---|---|---|---|---|
| row | `a_did_for_b` (central) — **B calls A with** `b_token_for_a` | base_url=B, fp=`fp_B_observed`, token=`a_token_for_b`, **local_device_id=`a_did_for_b`** | `b_did_for_a` (central) — **A calls B with** `a_token_for_b` | base_url=A, fp=`fp_A_self`, token=`b_token_for_a`, **local_device_id=`b_did_for_a`** |

Every `local_device_id` now names the device **in the local store that the peer presents a
token for** — exactly what `DeviceStore.revoke` needs.

---

## 4. C2 — unpair revokes the peer's INBOUND credential

`DELETE /api/peers/{id}` body is unchanged (`api_peers.py:196-205`) but now correct:
- **A unpairs B:** `peer_store.revoke(peer_id_A)` (clears A's outbound token to B) +
  `device_store.revoke(a_did_for_b)`. Because A's `local_device_id` is now `a_did_for_b`
  (A's own row), B's inbound token `b_token_for_a` fails its next `verify` -> B is locked
  out of A. B learns on its next call (401), shown as unreachable/unpaired.
- **B unpairs A:** symmetric — `device_store.revoke(b_did_for_a)` kills `a_token_for_b`.

The fix is entirely in **what id gets persisted** (section 3 steps 2-3), not in the unpair
handler.

---

## 5. M1 and I1 integration

- **M1 (observed, not self-reported, fingerprint):** the fingerprint the human confirms is
  produced by A's backend via `remote_cert_fingerprint` on the live socket (`/observe`),
  and the **same value is used as the pin** on every forward/reverse call in `confirm`. The
  peer never gets to assert its own fingerprint in a trusted field. `/exchange`'s
  JSON-body `fingerprint` is gone. Optional hardening: `confirm` re-observes and rejects
  with 409 if the live fingerprint no longer equals the human-confirmed `peer_fingerprint`
  (defeats a cert swap between observe and confirm); the pin already fails-closed on
  mismatch, so this is belt-and-suspenders.
- **I1 (pin BEFORE credential leaves the socket):** rewrite `peer_client._default_transport`
  to `conn.connect()` (force the TLS handshake with **no** application data sent) ->
  `der = conn.sock.getpeercert(binary_form=True)` -> verify `format_fingerprint(der) ==
  pinned_fingerprint`, raising `PeerClientError` on mismatch -> **only then**
  `conn.request(...)` -> `conn.getresponse()`. The bearer token / code is never written to a
  MitM's socket. Recommended additional fail-closed: when a call is a pairing/credential
  call, require `pinned_fingerprint is not None` (raise if a caller forgets to pin).

---

## 6. Why C1 is now closed (the argument a red-teamer will attack)

Claim: **a bare in-subnet host cannot obtain a `central` credential without a human reading
a code off a trusted screen.**

1. The only unauthenticated in-subnet endpoints that can yield a central credential are
   `POST /api/pair` and `POST /api/peers/redeem`. Both require a **valid live code**.
2. Codes are minted ONLY by `POST /api/pair-code`, which is `require_local` +
   `require_scope("admin")` (`app.py:1950-1951`) and **displays the code on the loopback
   dashboard** — its response is reachable only from loopback, never returned to a network
   caller. `/api/peers/exchange`, the one endpoint that returned a code to the network, is
   **deleted**.
3. Therefore an in-subnet attacker can only redeem if it **knows** a live code, and the
   only ways to know one are (a) read it off the operator's trusted screen — that is the
   authorized human, not the attacker — or (b) **guess** it.
4. Guessing is bounded and now load-bearing: 8-digit space (10^8), 120 s TTL
   (`pairing.CODE_TTL_SECONDS`), and `pairing.redeem` rejects once
   `MAX_FAILED_ATTEMPTS = 10` failures accrue in `ATTEMPT_WINDOW_SECONDS = 60`
   (`pairing.py:86-89`), counted on a single **global** `self._failed` list (not keyed on
   any attacker-controlled field, so it cannot be reset by rotating name/IP). Under C1 the
   rate-limiter was irrelevant because the code was handed out; now it is the real control,
   and the probability of hitting a live code within its window is approx (attempts
   allowed) / 10^8 -> infeasible.
5. The MitM variant is closed by M1+I1: to sit in the middle A must present a cert whose
   observed fingerprint (shown by A's backend) matches `fp_B` on B's screen — it cannot
   (different self-signed cert -> different fingerprint -> operator aborts), and even a
   momentary interception never receives the code/token because I1 verifies the pin before
   any application data is sent.

Transitive anchoring of the reverse leg: the reverse credential (`b_token_for_a`) and
`fp_A_self` travel A->B inside `link-back`, over the **same** channel A pinned to
`fp_B_observed`. Since step 5 proves no MitM survives that pin, that channel is genuinely
to B, so B receives the true values. B then pins `fp_A_self` for its calls to A; a MitM on
B->A would present a different cert -> mismatch -> B aborts. Both directions are thus
anchored to the **single** out-of-band fingerprint the human confirmed. `fp_A_self` being
self-reported by A is sound: A is the initiator the operator is actively driving with local
admin — a malicious initiator gains nothing by lying about its own fingerprint.

---

## 7. Concrete deltas per file

- **`backend/wavr/api_peers.py`**
  - `build_peers_public_router`: delete the `exchange` route and the `exchange_mgr`/
    `self_name` params it needed; keep `redeem` unchanged. Signature narrows to
    `(peer_store, pairing, cfg)` (peer_store kept for symmetry; `redeem` uses `pairing`).
  - `build_peers_admin_router`: add `observe` (calls `remote_cert_fingerprint`); rewrite
    `confirm` to the section 3 forward+reverse orchestration (no `exchange_id`, mints the
    local reverse device via `device_store.add`, sets `local_device_id=a_did_for_b`);
    replace `finish` with `link-back` (re-verify bearer -> `b_did_for_a`,
    `peer_store.add(..., local_device_id=b_did_for_a, token=body.token)`). Drop
    `exchange_mgr` param.
  - Every `post_json` in `confirm`/`link-back` passes `pinned_fingerprint=peer_fingerprint`.
- **`backend/wavr/peer_client.py`**: `_default_transport` -> connect -> verify pin ->
  request (I1). Optional: raise if a credential call has `pinned_fingerprint is None`.
- **`backend/wavr/peers.py`**: delete `PeerExchangeManager`, `PendingExchange`,
  `EXCHANGE_TTL_SECONDS`, `_utcnow`. `PeerStore` unchanged (its schema already allows
  everything needed; `local_device_id` semantics are now honored).
- **`backend/wavr/app.py`**
  - Middleware exemption (`:882`): drop `"/api/peers/exchange"`; keep `"/api/pair"` and
    `"/api/peers/redeem"`. `link-back` is authenticated (`require_central`), so it is NOT
    exempt.
  - Router mounts (`:996-1003`): drop `_exchange_mgr` from both factory calls; keep
    `admin_deps` and `finish_deps=[Depends(require_central)]` on the (now) `link-back`
    route. Delete the `_exchange_mgr = PeerExchangeManager()` init (`:479`).
- **`frontend/index.html`** (Peers panel, `renderPeers` ~`:3879`)
  - **Add a code INPUT** (mirror `#cpairCode`, `:1437`) and an observe->confirm sequence.
    `startPeerPairing` no longer mints A's own code (`mintOwnPeerCode`/`ownCertFingerprint`
    are deleted) and no longer calls the remote `/api/peers/exchange`.
  - New flow: click Pair -> `POST /api/peers/observe {peer_base_url}` -> show returned
    `fingerprint` in `#peerFpBox` (server-observed, M1) -> operator compares to B's screen +
    types the code -> `POST /api/peers/confirm {peer_base_url, peer_name, peer_code,
    peer_fingerprint}`. `#peerFpValue` is now fed the **observe** fingerprint, never a
    fingerprint from a cross-instance body.
  - **Add "Show this hub's pairing code"** in the Peers panel: calls the existing
    `POST /api/pair-code {role:"central"}` and displays `code` + `cert_fingerprint` for the
    OTHER hub's operator to read (this hub acting as target B). Reuses the endpoint the
    Mobile flow already uses — no new backend surface.
  - All peer strings stay `textContent`, never `innerHTML` (unchanged invariant).
- **`backend/tests/`**: delete `PeerExchangeManager`/`/exchange`/`/finish` tests; add
  `/observe`, reshaped `/confirm`, `/link-back`, the I1 pin-before-request test, and the
  two-instance in-process test that asserts (i) an in-subnet caller with no code cannot get
  a token, (ii) both `local_device_id`s are our-id-for-them, (iii) unpair from each side
  revokes the other's inbound token.

---

## 8. Constraints honored

- **Default-OFF / additive / backward-compatible:** `WAVR_PEERS_ENABLED` gate unchanged;
  with peers off, none of these routes exist (byte-identical to today). Requires
  `WAVR_MULTIDEVICE` as before (`app.py:274`).
- **Reuse-what-works:** no new auth machinery — `PairingManager.mint_code`/`redeem`,
  `DeviceStore.add`/`revoke`, `PeerStore`, the Mobile fingerprint-confirm UX, the existing
  `/api/pair-code` displayed-code endpoint, and the previously-dead
  `remote_cert_fingerprint` are all reused. Net machinery **removed**, not added
  (`PeerExchangeManager` deleted).
- **No push;** branch stays local.

---

## 9. Residual risks (flagged, not hidden)

1. **`fp_A_self` is self-reported to B** (reverse leg). Closed transitively (section 6)
   under the single confirmed fingerprint; the only way it fails is a malicious
   *initiator*, who gains nothing by lying about its own cert. Optional hardening: have B
   independently TOFU-observe A's fingerprint on its first call and surface a one-time
   confirm — deferred as unnecessary given the transitive anchoring.
2. **Forward-only pairing grants A->B before the reverse completes.** This is intended and
   human-authorized (redeeming B's on-screen code == B's admin granting A central, exactly
   the Mobile pattern). If `link-back` fails, B holds a valid `b_did_for_a` device but no
   PeerStore row until retry; B can still revoke it via the existing devices UI. Honest
   half-state, not a hole — surface `reverse_leg_ok:false` in the UI (already handled at
   `index.html:4073`).
3. **Pairing-DoS (pre-existing):** a flood of failed guesses can trip the global
   `MAX_FAILED_ATTEMPTS` limiter and briefly block a legitimate pairing. This property is
   inherited from `/api/pair` and out of scope here; note it, do not regress it. The code
   is confidentiality-safe regardless (guessing stays infeasible).
4. **TOCTOU between `/observe` and `/confirm`:** a cert swap in that window is caught by the
   pin failing closed on the credential call; the optional 409 re-observe check (section 5)
   closes the display/confirm gap fully. Recommend shipping the re-observe check.

## 10. Verdict

**Approve-with-conditions.** The reshape closes C1 completely (no endpoint network-vends a
central code; the rate-limited, screen-only code is the sole path to a token), fixes C2 by
persisting our-id-for-them at the two points it is minted locally, fixes M1 by confirming
the observed cert, and fixes I1 by verifying the pin before any credential is written to the
socket — while removing machinery rather than adding it. **Conditions to merge:** (a) ship
the I1 connect->verify->request ordering with a test that proves no bytes precede the pin
check; (b) delete `/api/peers/exchange` and its middleware exemption in the same commit;
(c) the two-instance test must assert cross-side revocation actually kills the inbound
token. Route implementation to `python-backend-engineer` (backend) +
`wavr-mobile-experience-engineer`/`frontend-web-engineer` (Peers panel), and re-run the
adversarial pass with `offensive-security-red-teamer` against section 6.
