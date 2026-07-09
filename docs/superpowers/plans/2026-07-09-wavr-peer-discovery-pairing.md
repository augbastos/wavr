# Wavr Peer Discovery & Mutual Pairing — Implementation Plan (Phase 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two Wavr instances (Desktop, Core, or a future second Core) on the same LAN can discover each other via mDNS and mutually pair — after which each instance's device store recognizes the other's bearer token as `role=central` (Wavr Pass), and each instance holds its own `PeerStore` row describing how to reach the other back. This is the foundation Phases 2-4 (fusion, portable credentials, remote config) all depend on. Phase 1 alone ships no new user-visible behavior beyond "Peers" pairing UI — no fusion, no credential portability yet.

**Architecture:** Reuses the existing ADR-0006/Wavr Pass primitives (`PairingManager`, `DeviceStore`, the `authorize`/`access_for`/`require_local`/`require_scope` gates) rather than inventing new auth machinery. Adds one new pure state-machine module (`peers.py`, mirrors `pairing.py`'s in-memory pending-exchange shape), one new outbound-HTTP module (`peer_client.py`, mirrors `ha_client.py`'s injectable-`urllib` shape), one new mDNS module (`mdns_peers.py`, lazy-imported `zeroconf`, mirrors the `[camera]`/`[mmwave]` optional-extra pattern), and one new router module (`api_peers.py`, mirrors `api_devices.py`). `fusion.py`, `pairing.py`, `devices.py` are **not modified** in this phase.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (stdlib `sqlite3`), stdlib `urllib`/`ssl` for outbound calls (no `httpx`/`requests` runtime dependency — matches `ha_client.py`), `zeroconf` as a new lazy-imported optional extra (`[mdns]`) for the actual mDNS advertise/browse (matches `[camera]`/`[mmwave]`/`[ble]`).

## Global Constraints

- **Default-OFF, additive, backward-compatible.** A new env flag `WAVR_PEERS_ENABLED` (default off) gates every route in this plan; `WAVR_PEERS_ENABLED=0` (or `WAVR_MULTIDEVICE=0`) must leave the app byte-identical to today. Peers require `WAVR_MULTIDEVICE=1` as a prerequisite (peer identity IS a `central`-role multidevice identity) — `WAVR_PEERS_ENABLED=1` with `WAVR_MULTIDEVICE=0` is a startup config error, fail loud not silent.
- **No push.** Public AGPL repo (`github.com/augbastos/wavr`). Work stays local/committed on a feature branch (e.g. `feat/peer-fusion`) until Augusto explicitly signs off on push, exactly like every prior sub-plan in this repo's history.
- **Zero new runtime HTTP client dependency.** Outbound peer calls use stdlib `urllib.request` + `ssl`, injectable transport functions — same discipline as `ha_client.py`.
- **`zeroconf` is a lazy, optional import** behind the new `[mdns]` extra — importing `wavr.mdns_peers` must not require it installed; only calling the real (non-injected) browse/advertise functions does.
- **Cert pinning, not blind trust.** Every outbound peer HTTPS call must verify the presented certificate's SHA-256 fingerprint against a value the admin explicitly confirmed (TOFU + human-verify, same trust model as the existing Mobile pairing flow) — never `ssl.CERT_NONE` without a fingerprint check layered on top. **This is intentional, not a shortcut to fix later:** Wavr's peers are self-signed LAN appliances with no CA (identical to how `desktop/`'s WavrNet Kotlin plugin already does custom `X509TrustManager` fingerprint-pinning instead of chain validation for Mobile↔Core). A security review that flags `ssl.CERT_NONE` in `peer_client.py` should check that EVERY call site past the initial TOFU probe (`remote_cert_fingerprint`, which by definition has nothing to pin against yet) passes a non-`None` `pinned_fingerprint` and that the mismatch path actually raises — that is the real control, not PKI chain validation, which is structurally impossible for a self-signed LAN device anyway. Do not "fix" this by adding real CA validation (breaks every peer connection) or by dropping the fingerprint check (removes the only real control).
- **In-subnet bound on unauthenticated peer routes.** Any route reachable without a bearer token (`/api/peers/exchange`, `/api/peers/redeem`) must be bounded exactly like today's `/api/pair`: same-`/24`-only, short code TTL, rate-limited failed attempts — reuse `PairingManager`'s existing `MAX_FAILED_ATTEMPTS`/`ATTEMPT_WINDOW_SECONDS` constants, don't reinvent them.
- **Style:** every new module/function gets the same doc-comment density already used throughout this codebase (see `pairing.py`/`devices.py`/`ha_client.py`) — explain the *why*, not just the *what*. Follow existing naming: `snake_case`, `frozenset` for fixed sets, dataclasses for records.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/wavr/tls.py` (modify) | Add `fingerprint_from_pem()` (extracted, reused) + `remote_cert_fingerprint(host, port)` (new — TOFU-fetch a remote peer's presented cert fingerprint) |
| `backend/wavr/peers.py` (new) | `PeerStore` (SQLite: persisted peer relationships) + `PeerExchangeManager` (in-memory: the short-lived pending two-leg handshake state) |
| `backend/wavr/peer_client.py` (new) | Outbound HTTP to a peer's API: pinned-HTTPS GET/POST, injectable transport (mirrors `ha_client.py`) |
| `backend/wavr/mdns_peers.py` (new) | `browse_wavr_peers()` (discover `_wavr._tcp` on the LAN) + `advertise_self()` (Desktop's own self-advertise; Core already advertises via `core-launcher`'s Kotlin `NsdManager`, unchanged) — lazy `zeroconf`, injectable client |
| `backend/wavr/api_peers.py` (new) | FastAPI routers: discovery list, the 5-endpoint exchange protocol, paired-peers list/unpair |
| `backend/wavr/app.py` (modify) | Wire `WAVR_PEERS_ENABLED`, mount `api_peers` routers, start/stop mDNS browse+advertise in the lifespan, token-exemption for the two unauthenticated peer routes |
| `backend/wavr/config.py` (modify) | Add `peers_enabled: bool` field + env parsing |
| `backend/pyproject.toml` (modify) | Add `mdns = ["zeroconf>=0.132"]` extra |
| `frontend/index.html` (modify) | "Peers" panel: discovered list, Pair button, fingerprint-confirm dialog, paired-peers list with Unpair |
| `backend/tests/test_peers.py` (new) | Full coverage: `PeerStore`, `PeerExchangeManager`, `remote_cert_fingerprint`, the router happy/deny paths, and one full two-instance in-process protocol test |

---

## The protocol this plan implements (read before starting)

Peer pairing is **two independent legs**, each leg identical in shape to today's existing Mobile pairing (mint code → admin reads it → the other side redeems it), but orchestrated so the admin only has to click once, from one screen:

1. Admin at instance **D** clicks "Pair" on a peer **C** discovered via mDNS.
2. D mints its own code locally (loopback, trivial — `PairingManager.mint_code("central")`) and POSTs it to C's new unauthenticated-in-subnet `/api/peers/exchange`, along with D's own base URL and D's own claimed fingerprint.
3. C's `/api/peers/exchange` handler stashes D's code/fingerprint/base_url as a **pending exchange** (`PeerExchangeManager`, TTL'd, mirrors `PairingManager`'s pending-code shape) and, in the SAME response, mints and returns its OWN fresh code + fingerprint.
4. D's UI shows C's fingerprint (as OBSERVED over the live TLS connection D just made — not merely the claimed value in the JSON body) for the admin to visually confirm against C's own on-screen fingerprint display (same UX as today's Mobile pairing).
5. Admin confirms on D. D's `/api/peers/confirm` handler then:
   a. Redeems C's code via `/api/peers/redeem` on C → gets `(device_id, token)` → D's `PeerStore` gains a row: "this is how D reaches C" (base_url=C, fingerprint=C's observed fp, token=the one just received).
   b. Calls C's **authenticated** `/api/peers/finish` (using the token from 5a) → C looks up the pending exchange it stashed in step 3 (matching D's identity, now authenticated) and, using the code D gave it back in step 2, calls D's `/api/peers/redeem` → C's `PeerStore` gains its own row: "this is how C reaches D" (base_url=D, fingerprint=D's fp as claimed in step 2 and pinned in step 3's TOFU-fetch when C first connected back, token=the one just received from D).
6. Both `PeerStore`s now have a row for the other. Both `DeviceStore`s have a `Device` row for the other with `role=central` (Wavr Pass's existing default scopes — `admin`, `control`, `mcp` — apply with zero extra grant logic, per the design spec).

Unpair (either side, either time) = `PeerStore.revoke(peer_id)` + `DeviceStore.revoke(their_device_id)` — immediate, unilateral, no confirmation needed from the other side (they find out on their next 401).

---

## Task 1: `tls.py` — extract `fingerprint_from_pem`, add `remote_cert_fingerprint`

**Files:**
- Modify: `backend/wavr/tls.py`
- Test: `backend/tests/test_tls.py` (extend if it exists; else create alongside the existing `cert_fingerprint` tests — check first with `grep -l cert_fingerprint backend/tests/*.py`)

**Interfaces:**
- Produces: `fingerprint_from_pem(pem: str) -> str | None` (pure, formats like `cert_fingerprint`), `remote_cert_fingerprint(host: str, port: int, timeout: float = 5.0, fetch: Callable[[str, int, float], str] | None = None) -> str | None` (network by default via `ssl.get_server_certificate`, injectable `fetch` for tests)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_tls.py  (add if the file exists; create with this content if not)
from wavr.tls import fingerprint_from_pem, remote_cert_fingerprint

_PEM = """-----BEGIN CERTIFICATE-----
MIIBazCCARWgAwIBAgIUAJz9F1234567890abcdefgAwCgYIKoZIzj0EAwIwEDEO
MAwGA1UEAwwFd2F2cjAeFw0yNjA3MDYwMDAwMDBaFw0yNzA4MDcwMDAwMDBaMBAx
DjAMBgNVBAMMBXdhdnIwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATest1234567
890abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ
o1MwUTAdBgNVHQ4EFgQUtest1234567890abcdefgwHwYDVR0jBBgwFoAUtest123
4567890abcdefgwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNIADBFAiEA1
23456789abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGAiA==
-----END CERTIFICATE-----
"""

def test_fingerprint_from_pem_matches_cert_fingerprint_shape():
    fp = fingerprint_from_pem(_PEM)
    assert fp is not None
    assert len(fp) == 32 * 3 - 1  # 32 hex-pairs, colon-joined
    assert fp == fp.upper()

def test_fingerprint_from_pem_none_on_garbage():
    assert fingerprint_from_pem("not a cert") is None

def test_remote_cert_fingerprint_uses_injected_fetch():
    def fake_fetch(host, port, timeout):
        assert (host, port) == ("192.168.1.57", 8443)
        return _PEM
    fp = remote_cert_fingerprint("192.168.1.57", 8443, fetch=fake_fetch)
    assert fp == fingerprint_from_pem(_PEM)

def test_remote_cert_fingerprint_none_on_connect_failure():
    def failing_fetch(host, port, timeout):
        raise OSError("connection refused")
    assert remote_cert_fingerprint("10.0.0.99", 8443, fetch=failing_fetch) is None
```

Note: the `_PEM` fixture above is illustrative shape only — when writing this test for real, generate an actual tiny self-signed cert PEM with `cryptography` (already a test-time dep, see `pyproject.toml`'s `dev` extra) in a `conftest.py` fixture or inline, rather than hand-typing base64. Check `backend/tests/` for an existing cert-generation test helper first (search `grep -rl "BEGIN CERTIFICATE" backend/tests/`) and reuse it if one exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tls.py -v -k "fingerprint_from_pem or remote_cert_fingerprint"`
Expected: FAIL with `ImportError: cannot import name 'fingerprint_from_pem'`

- [ ] **Step 3: Implement — extract `fingerprint_from_pem`, add `remote_cert_fingerprint`**

In `backend/wavr/tls.py`, replace the body of `cert_fingerprint` to delegate to a new public `fingerprint_from_pem`, and add the network fetcher:

```python
def cert_fingerprint(cert_path: str) -> str | None:
    """SHA-256 fingerprint of the DER certificate at `cert_path` ... (docstring unchanged)"""
    try:
        pem = Path(cert_path).read_text(encoding="ascii", errors="ignore")
    except OSError:
        return None
    return fingerprint_from_pem(pem)


def fingerprint_from_pem(pem: str) -> str | None:
    """SHA-256 fingerprint of the first `CERTIFICATE` block in `pem`, formatted
    uppercase colon-separated hex (browser-style). None if no parseable block.
    Extracted from `cert_fingerprint` (Phase 1 peer-pairing, 2026-07-09) so a
    PEM fetched over the network (see `remote_cert_fingerprint`) can be
    fingerprinted the same way as one read from disk -- one formatting rule,
    two sources."""
    der = _first_cert_der(pem)
    if der is None:
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _default_remote_fetch(host: str, port: int, timeout: float) -> str:
    """Real network TOFU-fetch: connect and return the PEM of whatever
    certificate the peer presents, WITHOUT validating it against any CA --
    validation is the caller's job (compare the resulting fingerprint against
    an admin-confirmed value). Pure stdlib `ssl`."""
    import ssl
    return ssl.get_server_certificate((host, port), timeout=timeout)


def remote_cert_fingerprint(host: str, port: int, timeout: float = 5.0,
                             fetch=None) -> str | None:
    """SHA-256 fingerprint of the certificate `host:port` presents RIGHT NOW,
    for the peer-pairing exchange (Phase 1): the admin compares this against
    the peer's own on-screen fingerprint before the pairing is trusted. `fetch`
    is injectable ((host, port, timeout) -> PEM str); the default makes a real
    TLS connection and returns whatever cert is presented, unvalidated -- this
    function is the TOFU probe, not the trust decision. Returns None on any
    connection failure or unparseable response (never raises -- a peer that's
    offline or mid-reboot is an honest 'can't fingerprint yet', not a crash)."""
    fetcher = fetch or _default_remote_fetch
    try:
        pem = fetcher(host, port, timeout)
    except Exception:
        return None
    return fingerprint_from_pem(pem)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_tls.py -v -k "fingerprint_from_pem or remote_cert_fingerprint"`
Expected: PASS (all 4)

- [ ] **Step 5: Run the full existing tls test suite to confirm no regression**

Run: `cd backend && python -m pytest tests/test_tls.py -v`
Expected: PASS (every pre-existing `cert_fingerprint` test still green — `cert_fingerprint` is now a thin wrapper but behaviorally unchanged)

- [ ] **Step 6: Commit**

```bash
cd C:\IA\wavr
git add backend/wavr/tls.py backend/tests/test_tls.py
git commit -m "refactor(tls): extract fingerprint_from_pem, add remote_cert_fingerprint

Peer pairing (Phase 1) needs to TOFU-fetch and fingerprint a REMOTE peer's
presented cert, not just read our own from disk. cert_fingerprint's PEM->
fingerprint logic is now shared via fingerprint_from_pem; cert_fingerprint
itself is unchanged behaviorally."
```

---

## Task 2: `peers.py` — `PeerStore` (persisted relationships)

**Files:**
- Create: `backend/wavr/peers.py`
- Test: `backend/tests/test_peers.py` (new)

**Interfaces:**
- Consumes: nothing new (stdlib `sqlite3`/`secrets`/`hashlib`, same shape as `devices.py`)
- Produces:
  - `@dataclass(frozen=True) class Peer` — fields: `peer_id: str`, `name: str`, `base_url: str`, `cert_fingerprint: str`, `local_device_id: str` (the id THIS instance issued to the peer in its own `DeviceStore`, i.e. how we recognize their future bearer-token requests), `room_map: dict[str, str]` (our room name -> their room name, empty until Phase 2's UI fills it), `created_ts: str`, `revoked: bool`
  - `class PeerStore` — `__init__(self, path: str = "wavr.db", now_fn=_utcnow_iso)`, `.add(name, base_url, cert_fingerprint, local_device_id, token) -> str` (returns `peer_id`; `token` is the credential WE use to call THEM, stored, never returned by any read method), `.token_for(peer_id) -> str | None`, `.list() -> list[Peer]`, `.get(peer_id) -> Peer | None`, `.set_room_map(peer_id, room_map: dict[str, str]) -> bool`, `.revoke(peer_id) -> bool`, `.close()`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_peers.py
import pytest
from wavr.peers import PeerStore


def _store(tmp_path):
    return PeerStore(str(tmp_path / "peers.db"))


def test_add_returns_peer_id_and_is_listed(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add(name="Core-G9", base_url="https://192.168.1.57:8000",
                         cert_fingerprint="AB:CD:EF", local_device_id="dev123",
                         token="secret-token-abc")
    assert peer_id
    peers = store.list()
    assert len(peers) == 1
    assert peers[0].peer_id == peer_id
    assert peers[0].name == "Core-G9"
    assert peers[0].base_url == "https://192.168.1.57:8000"
    assert peers[0].cert_fingerprint == "AB:CD:EF"
    assert peers[0].room_map == {}
    assert peers[0].revoked is False


def test_token_for_returns_the_stored_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    assert store.token_for(peer_id) == "tok-xyz"


def test_token_for_unknown_peer_is_none(tmp_path):
    store = _store(tmp_path)
    assert store.token_for("nope") is None


def test_list_never_includes_token(tmp_path):
    store = _store(tmp_path)
    store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    peer = store.list()[0]
    assert not hasattr(peer, "token")


def test_get_by_id(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.get(peer_id).name == "Core-G9"
    assert store.get("nope") is None


def test_set_room_map_persists(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.set_room_map(peer_id, {"sala": "living_room"}) is True
    assert store.get(peer_id).room_map == {"sala": "living_room"}


def test_set_room_map_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.set_room_map("nope", {"a": "b"}) is False


def test_revoke_marks_revoked_and_clears_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.get(peer_id).revoked is True
    assert store.token_for(peer_id) is None  # revoked = unusable, not just flagged


def test_revoke_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.revoke("nope") is False


def test_revoke_is_idempotent(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.revoke(peer_id) is True  # second revoke still True, not an error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k "PeerStore or peer_id or token_for or room_map or revoke"`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.peers'`

- [ ] **Step 3: Implement `PeerStore`**

```python
# backend/wavr/peers.py
"""Peer-instance relationships for cross-instance fusion + portable admin
identity (2026-07-09 design spec, Phase 1). A "peer" is another Wavr instance
(Desktop, Core, a future second Core) this instance has mutually pointed at
via the exchange protocol in `api_peers.py` -- NOT a plain companion device
(phone/tablet), even though both end up as a `role=central` row in the SAME
`DeviceStore` (Wavr Pass draws no new role; see the design spec).

`PeerStore` is this instance's OWN-DIRECTION bookkeeping: for each peer, how
do WE reach THEM (base_url, pinned cert fingerprint, OUR credential -- the
token THEY issued to US). This is deliberately separate from `DeviceStore`
(which is the SERVER-perspective "who can present a token to ME" table) --
`DeviceStore` already gets its own row for the peer from the ordinary pairing
redeem; `PeerStore` is the CLIENT-perspective row this instance needs to
actively call them back (for fusion's `RemoteSource` in Phase 2, and for
remote config in Phase 4).

`local_device_id` links back to the `Device.device_id` `DeviceStore` assigned
this peer when they redeemed a code from us -- follow it to check their role/
revoked state via `DeviceStore.get()` rather than duplicating that here.

Token is stored PLAINTEXT (not hashed like `DeviceStore`'s tokens): unlike a
companion's token (which only ever needs to be VERIFIED, never re-sent), this
token is one WE must re-present as our own Authorization header on every
outbound call to the peer -- a hash can't be un-hashed to do that. This
mirrors how a client stores an OAuth access token it must keep sending, not
how a server stores a password it only ever compares against."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id          TEXT PRIMARY KEY,
    name             TEXT    NOT NULL,
    base_url         TEXT    NOT NULL,
    cert_fingerprint TEXT    NOT NULL,
    local_device_id  TEXT    NOT NULL,
    token            TEXT,
    room_map         TEXT    NOT NULL DEFAULT '{}',
    created_ts       TEXT    NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Peer:
    """A peer relationship, minus its live token (see `PeerStore.token_for`)."""

    peer_id: str
    name: str
    base_url: str
    cert_fingerprint: str
    local_device_id: str
    room_map: dict = field(default_factory=dict)
    created_ts: str = ""
    revoked: bool = False

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id, "name": self.name, "base_url": self.base_url,
            "cert_fingerprint": self.cert_fingerprint,
            "local_device_id": self.local_device_id, "room_map": dict(self.room_map),
            "created_ts": self.created_ts, "revoked": self.revoked,
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PeerStore:
    """SQLite-backed peer-relationship store. Shares the db file with
    `DeviceStore`/`Storage` but owns its own `peers` table, same pattern as
    every other *_store.py in this codebase."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, name: str, base_url: str, cert_fingerprint: str,
            local_device_id: str, token: str) -> str:
        peer_id = secrets.token_hex(16)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO peers (peer_id, name, base_url, cert_fingerprint,"
                " local_device_id, token, room_map, created_ts, revoked)"
                " VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 0)",
                (peer_id, name, base_url, cert_fingerprint, local_device_id, token, ts),
            )
            self._conn.commit()
        return peer_id

    def token_for(self, peer_id: str) -> str | None:
        """The token to present when WE call THEM, or None if unknown/revoked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT token, revoked FROM peers WHERE peer_id = ?", (peer_id,)
            ).fetchone()
        if row is None or row["revoked"]:
            return None
        return row["token"]

    def list(self) -> list[Peer]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT peer_id, name, base_url, cert_fingerprint, local_device_id,"
                " room_map, created_ts, revoked FROM peers ORDER BY created_ts, peer_id"
            ).fetchall()
        return [self._to_peer(r) for r in rows]

    def get(self, peer_id: str) -> Peer | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT peer_id, name, base_url, cert_fingerprint, local_device_id,"
                " room_map, created_ts, revoked FROM peers WHERE peer_id = ?",
                (peer_id,),
            ).fetchone()
        return self._to_peer(row) if row else None

    def set_room_map(self, peer_id: str, room_map: dict) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peers SET room_map = ? WHERE peer_id = ?",
                (json.dumps(room_map), peer_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def revoke(self, peer_id: str) -> bool:
        """Mark revoked AND clear the token (belt-and-suspenders: `token_for`
        already checks `revoked`, but a cleared token can't leak via a future
        code path that forgets to check the flag). Idempotent."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peers SET revoked = 1, token = NULL WHERE peer_id = ?",
                (peer_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _to_peer(r: sqlite3.Row) -> Peer:
        return Peer(
            peer_id=r["peer_id"], name=r["name"], base_url=r["base_url"],
            cert_fingerprint=r["cert_fingerprint"], local_device_id=r["local_device_id"],
            room_map=json.loads(r["room_map"] or "{}"),
            created_ts=r["created_ts"], revoked=bool(r["revoked"]),
        )

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peers.py -v`
Expected: PASS (all `PeerStore` tests)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/peers.py backend/tests/test_peers.py
git commit -m "feat(peers): PeerStore -- persisted cross-instance peer relationships

Client-perspective bookkeeping (base_url, pinned fingerprint, our own
credential to call them, room-name map) distinct from DeviceStore's
server-perspective 'who can call me' table. Foundation for Phase 2 fusion
and Phase 4 remote config; not wired into the app yet."
```

---

## Task 3: `peers.py` — `PeerExchangeManager` (in-memory pending handshake)

**Files:**
- Modify: `backend/wavr/peers.py` (append)
- Test: `backend/tests/test_peers.py` (append)

**Interfaces:**
- Consumes: `wavr.devices.VALID_ROLES` is NOT needed here (peer role is always `central`, hardcoded)
- Produces: `class PeerExchangeManager` — `__init__(self, now_fn=_utcnow, ttl: float = EXCHANGE_TTL_SECONDS)`, `.stash(requester_name: str, requester_base_url: str, requester_code: str, requester_fingerprint: str) -> str` (returns an `exchange_id`), `.pop(exchange_id: str) -> PendingExchange | None` (single-use, consumed on read), `EXCHANGE_TTL_SECONDS` module constant (recommend 120, matching `pairing.CODE_TTL_SECONDS`), `@dataclass(frozen=True) class PendingExchange` with fields `requester_name`, `requester_base_url`, `requester_code`, `requester_fingerprint`

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_peers.py
from datetime import datetime, timedelta, timezone
from wavr.peers import PeerExchangeManager


class _Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def test_stash_and_pop_roundtrip():
    mgr = PeerExchangeManager()
    exchange_id = mgr.stash("Desktop", "https://192.168.1.10:8000", "12345678", "AA:BB")
    pending = mgr.pop(exchange_id)
    assert pending.requester_name == "Desktop"
    assert pending.requester_base_url == "https://192.168.1.10:8000"
    assert pending.requester_code == "12345678"
    assert pending.requester_fingerprint == "AA:BB"


def test_pop_is_single_use():
    mgr = PeerExchangeManager()
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    assert mgr.pop(exchange_id) is not None
    assert mgr.pop(exchange_id) is None  # consumed


def test_pop_unknown_id_returns_none():
    mgr = PeerExchangeManager()
    assert mgr.pop("nope") is None


def test_pop_expired_returns_none():
    clock = _Clock()
    mgr = PeerExchangeManager(now_fn=clock, ttl=120)
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    clock.advance(121)
    assert mgr.pop(exchange_id) is None


def test_pop_just_before_ttl_still_works():
    clock = _Clock()
    mgr = PeerExchangeManager(now_fn=clock, ttl=120)
    exchange_id = mgr.stash("Desktop", "https://x:8000", "code", "fp")
    clock.advance(119)
    assert mgr.pop(exchange_id) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k "Exchange"`
Expected: FAIL with `ImportError: cannot import name 'PeerExchangeManager'`

- [ ] **Step 3: Implement `PeerExchangeManager`**

Append to `backend/wavr/peers.py`:

```python
EXCHANGE_TTL_SECONDS = 120  # matches pairing.CODE_TTL_SECONDS -- same window discipline


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PendingExchange:
    """The requester's half of a peer-pairing exchange, stashed by the side
    that receives POST /api/peers/exchange (see api_peers.py) until the
    requester comes back authenticated (via /api/peers/finish) to complete
    the reverse leg -- see the protocol walkthrough at the top of this
    plan/the design spec §2."""

    requester_name: str
    requester_base_url: str
    requester_code: str
    requester_fingerprint: str


class PeerExchangeManager:
    """In-memory, ephemeral (never persisted -- same reasoning as
    PairingManager's codes/tickets: this is a short-lived handshake artifact,
    not a durable record). Bound to ONE pending exchange PER CALLER at a time
    by design: `stash` always returns a FRESH id and simply adds another
    entry -- the api_peers.py layer is responsible for keying which exchange
    belongs to which authenticated caller in Task 4 (finish) by looking the
    caller's device up via `DeviceStore` after they authenticate, not by
    trusting any exchange_id the caller presents post-auth."""

    def __init__(self, now_fn=_utcnow, ttl: float = EXCHANGE_TTL_SECONDS):
        self._now = now_fn
        self._ttl = ttl
        self._pending: dict[str, tuple[PendingExchange, datetime]] = {}

    def stash(self, requester_name: str, requester_base_url: str,
              requester_code: str, requester_fingerprint: str) -> str:
        self._purge_expired()
        exchange_id = secrets.token_urlsafe(16)
        expires = self._now() + __import__("datetime").timedelta(seconds=self._ttl)
        self._pending[exchange_id] = (
            PendingExchange(requester_name, requester_base_url, requester_code,
                             requester_fingerprint),
            expires,
        )
        return exchange_id

    def pop(self, exchange_id: str) -> PendingExchange | None:
        entry = self._pending.pop(exchange_id, None)
        if entry is None:
            return None
        pending, expires = entry
        if self._now() >= expires:
            return None
        return pending

    def _purge_expired(self) -> None:
        now = self._now()
        self._pending = {k: v for k, v in self._pending.items() if now < v[1]}
```

Note: the inline `__import__("datetime")` above is a placeholder for a clean
import — replace it by adding `timedelta` to the existing `from datetime
import datetime, timezone` line already at the top of `peers.py` (from Task
2) and just write `self._now() + timedelta(seconds=self._ttl)` directly. Do
not actually ship the `__import__` form.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peers.py -v`
Expected: PASS (all `PeerStore` + `PeerExchangeManager` tests)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/peers.py backend/tests/test_peers.py
git commit -m "feat(peers): PeerExchangeManager -- in-memory pending peer-handshake state

Mirrors PairingManager's ephemeral pending-code shape (TTL'd, single-use
pop). Not wired into the app yet -- api_peers.py (Task 5) is what actually
drives the protocol using this."
```

---

## Task 4: `peer_client.py` — outbound pinned-HTTPS transport

**Files:**
- Create: `backend/wavr/peer_client.py`
- Test: `backend/tests/test_peer_client.py` (new)

**Interfaces:**
- Consumes: `wavr.tls.remote_cert_fingerprint` (Task 1)
- Produces: `class PeerClientError(RuntimeError)`, `def post_json(base_url: str, path: str, body: dict, token: str | None = None, pinned_fingerprint: str | None = None, timeout: float = 5.0, transport=None) -> dict`, `def get_json(base_url: str, path: str, token: str | None = None, pinned_fingerprint: str | None = None, timeout: float = 5.0, transport=None) -> dict` — `transport` is injectable `(method, url, headers, body_bytes_or_None, pinned_fingerprint, timeout) -> bytes`, default real implementation opens an `ssl.SSLContext(CERT_NONE)` connection, then verifies the presented cert's fingerprint against `pinned_fingerprint` (when given) BEFORE reading the response body, raising `PeerClientError` on mismatch

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_peer_client.py
import json
import pytest
from wavr.peer_client import PeerClientError, get_json, post_json


def test_post_json_happy_path():
    calls = []

    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        calls.append((method, url, headers, body, pinned_fingerprint))
        return json.dumps({"ok": True}).encode()

    result = post_json("https://192.168.1.57:8000", "/api/peers/redeem",
                        {"code": "123"}, token="tok-abc",
                        pinned_fingerprint="AA:BB", transport=fake_transport)
    assert result == {"ok": True}
    method, url, headers, body, fp = calls[0]
    assert method == "POST"
    assert url == "https://192.168.1.57:8000/api/peers/redeem"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"code": "123"}
    assert fp == "AA:BB"


def test_post_json_without_token_omits_auth_header():
    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        assert "Authorization" not in headers
        return b"{}"
    post_json("https://x:8000", "/api/peers/exchange", {}, token=None,
              transport=fake_transport)


def test_get_json_happy_path():
    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        assert method == "GET"
        assert body is None
        return json.dumps({"peers": []}).encode()
    result = get_json("https://x:8000", "/api/peers", token="t",
                       transport=fake_transport)
    assert result == {"peers": []}


def test_transport_error_raises_peer_client_error():
    def failing_transport(*a, **k):
        raise OSError("connection refused")
    with pytest.raises(PeerClientError):
        post_json("https://x:8000", "/api/peers/exchange", {}, transport=failing_transport)


def test_bad_json_response_raises_peer_client_error():
    def bad_json_transport(*a, **k):
        return b"not json"
    with pytest.raises(PeerClientError):
        post_json("https://x:8000", "/api/peers/exchange", {}, transport=bad_json_transport)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peer_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.peer_client'`

- [ ] **Step 3: Implement `peer_client.py`**

```python
# backend/wavr/peer_client.py
"""Outbound HTTP to another Wavr instance's API (peer pairing/fusion/remote
config, Phase 1+). Same discipline as ha_client.py: stdlib `urllib`/`ssl`
only, no third-party HTTP client added to Wavr's runtime deps, transport
fully injectable so every caller is unit-testable with zero real network.

Unlike ha_client.py (which talks to the user's OWN Home Assistant over plain
HTTP on a network the user already trusts), a peer connection is
self-signed-HTTPS with an admin-confirmed pinned fingerprint -- see
`wavr.tls.remote_cert_fingerprint` for the fetch-time TOFU probe used during
pairing itself, and `pinned_fingerprint` here for every call AFTER pairing
(where the peer's identity should already be known and MUST be re-verified
every time, not just once at pairing -- a cert that silently changed after
pairing is exactly the "someone is intercepting your network" case the
existing Mobile pairing flow's MitM screen already treats as a hard stop)."""
from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Callable

from wavr.tls import fingerprint_from_pem


class PeerClientError(RuntimeError):
    """A peer call failed: unreachable, TLS fingerprint mismatch, or an
    unparseable response. Callers decide how to degrade (Phase 2's
    RemoteSource reconnect-forever; Phase 4's remote-config per-peer
    failure report) -- this module only ever raises, never guesses."""


# (method, url, headers, body_bytes_or_None, pinned_fingerprint, timeout) -> response bytes
Transport = Callable[[str, str, dict, bytes | None, str | None, float], bytes]


def _default_transport(method: str, url: str, headers: dict, body: bytes | None,
                        pinned_fingerprint: str | None, timeout: float) -> bytes:
    """Real transport: opens the connection over an SSLContext that accepts
    ANY cert (self-signed peers have no CA) but, when `pinned_fingerprint` is
    given, verifies the ACTUAL presented certificate's fingerprint matches
    before trusting the response -- the same TOFU-then-pin model the
    pairing/Mobile flow already uses, just enforced on every call, not only
    at pairing time."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (LAN peer, fingerprint-pinned below)
        if pinned_fingerprint is not None:
            der = resp.fp.raw._sock.getpeercert(binary_form=True)  # type: ignore[attr-defined]
            import hashlib
            observed = ":".join(
                f"{b:02X}" for b in hashlib.sha256(der).digest()
            )
            if observed != pinned_fingerprint:
                raise PeerClientError(
                    f"peer certificate fingerprint mismatch: expected "
                    f"{pinned_fingerprint}, got {observed} -- possible MitM")
        return resp.read()


def _call(base_url: str, path: str, method: str, body: dict | None, token: str | None,
          pinned_fingerprint: str | None, timeout: float, transport) -> dict:
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body_bytes = json.dumps(body).encode() if body is not None else None
    xport = transport or _default_transport
    try:
        raw = xport(method, url, headers, body_bytes, pinned_fingerprint, timeout)
    except PeerClientError:
        raise
    except Exception as exc:
        raise PeerClientError(f"peer call failed: {exc}") from exc
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise PeerClientError(f"peer returned unparseable response: {exc}") from exc


def post_json(base_url: str, path: str, body: dict, token: str | None = None,
              pinned_fingerprint: str | None = None, timeout: float = 5.0,
              transport=None) -> dict:
    return _call(base_url, path, "POST", body, token, pinned_fingerprint, timeout, transport)


def get_json(base_url: str, path: str, token: str | None = None,
             pinned_fingerprint: str | None = None, timeout: float = 5.0,
             transport=None) -> dict:
    return _call(base_url, path, "GET", None, token, pinned_fingerprint, timeout, transport)
```

**Implementer note:** `resp.fp.raw._sock.getpeercert(binary_form=True)` reaches
through `http.client`'s private internals to the underlying `ssl.SSLSocket` —
this is the standard (if slightly ugly) way to get the peer cert from
`urllib.request` without switching HTTP clients. Verify this attribute path
against the actual Python version in `backend/.venv` before shipping (`python
--version`; test on 3.11+ per the Global Constraints) — if it differs, the
fallback is `http.client.HTTPSConnection` used directly instead of
`urllib.request.urlopen`, which exposes `.sock.getpeercert()` more directly.
Either way, write the fingerprint-mismatch test (`test_transport_error_
raises_peer_client_error`-style, injected) FIRST against whichever concrete
approach compiles cleanly, since the injected-transport tests above don't
exercise `_default_transport` at all — add one additional test that
exercises the mismatch path with a real (test-only) self-signed socket if
time allows; otherwise it's covered functionally in Task 9's live bring-up.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peer_client.py -v`
Expected: PASS (5/5 — none of these exercise `_default_transport`, they all inject)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/peer_client.py backend/tests/test_peer_client.py
git commit -m "feat(peer_client): injectable pinned-HTTPS outbound calls to a peer instance

Stdlib urllib/ssl only, no new runtime HTTP dependency (matches ha_client.py's
discipline). Every call re-verifies the peer's cert fingerprint when one is
pinned -- not just at pairing time."
```

---

## Task 5: `mdns_peers.py` — browse + Desktop self-advertise

**Files:**
- Create: `backend/wavr/mdns_peers.py`
- Test: `backend/tests/test_mdns_peers.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `@dataclass(frozen=True) class DiscoveredPeer` (fields: `name: str`, `host: str`, `port: int`, `role: str`), `def browse_wavr_peers(timeout: float = 3.0, zeroconf_factory=None) -> list[DiscoveredPeer]` (blocking snapshot browse, injectable factory for tests), `def advertise_self(name: str, port: int, role: str = "desktop", zeroconf_factory=None)` (returns a stoppable handle; injectable)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_mdns_peers.py
from wavr.mdns_peers import DiscoveredPeer, browse_wavr_peers, advertise_self


class _FakeInfo:
    def __init__(self, name, host, port, role):
        self._name, self.port = name, port
        self.properties = {b"role": role.encode()}
        self._host = host

    def parsed_addresses(self):
        return [self._host]


class _FakeZeroconf:
    """Minimal fake standing in for zeroconf.Zeroconf + ServiceBrowser: the
    injected factory returns an object with `.get_service_info(type_, name)`
    (browse) and `.register_service(info)` / `.unregister_service(info)`
    (advertise), and `.close()`. Real zeroconf usage is exercised only
    manually (no hardware/network in CI)."""

    def __init__(self, services):
        self._services = services  # {name: _FakeInfo}
        self.registered = []
        self.closed = False

    def get_service_info(self, type_, name, timeout=3000):
        return self._services.get(name)

    def service_names(self):
        return list(self._services)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.registered.remove(info)

    def close(self):
        self.closed = True


def test_browse_returns_discovered_peers():
    fake = _FakeZeroconf({
        "core._wavr._tcp.local.": _FakeInfo("Wavr Core", "192.168.1.57", 8000, "core"),
        "desktop._wavr._tcp.local.": _FakeInfo("Wavr Desktop", "192.168.1.227", 8000, "desktop"),
    })
    found = browse_wavr_peers(zeroconf_factory=lambda: fake)
    assert len(found) == 2
    names = {p.name for p in found}
    assert names == {"Wavr Core", "Wavr Desktop"}
    core = next(p for p in found if p.role == "core")
    assert core.host == "192.168.1.57" and core.port == 8000


def test_browse_empty_when_nothing_discovered():
    fake = _FakeZeroconf({})
    assert browse_wavr_peers(zeroconf_factory=lambda: fake) == []


def test_advertise_self_registers_and_returns_stoppable_handle():
    fake = _FakeZeroconf({})
    handle = advertise_self("Wavr Desktop", 8000, role="desktop",
                             zeroconf_factory=lambda: fake)
    assert len(fake.registered) == 1
    handle.stop()
    assert fake.registered == []
    assert fake.closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_mdns_peers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.mdns_peers'`

- [ ] **Step 3: Implement `mdns_peers.py`**

```python
# backend/wavr/mdns_peers.py
"""mDNS/DNS-SD peer discovery for cross-instance pairing (2026-07-09 design
spec, Phase 1). Core already self-advertises `_wavr._tcp` from the native
Kotlin launcher (`core-launcher`, commit 3af4787) -- that side is UNCHANGED
by this module. What's new here:

  * BROWSING for `_wavr._tcp` on the LAN -- needed by BOTH Desktop and Core's
    Python backend (neither browses today; only Mobile's capacitor-zeroconf
    does, for a different purpose -- pairing AS a companion, not peer
    discovery).
  * Desktop's OWN self-advertise -- Desktop has no Kotlin/NsdManager
    equivalent, so it advertises the same `_wavr._tcp` TXT shape
    (`{v, path, role}`) via the `zeroconf` Python package instead.

`zeroconf` is a LAZY import (only inside the real, non-injected path) behind
the new `[mdns]` extra -- a base install that never touches peer-discovery
code never needs it installed, same pattern as `[camera]`/`[mmwave]`/`[ble]`.
Every public function takes an injectable `zeroconf_factory` so this module
is fully unit-testable without the dependency installed or any real network."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

_SERVICE_TYPE = "_wavr._tcp.local."


@dataclass(frozen=True)
class DiscoveredPeer:
    name: str
    host: str
    port: int
    role: str


def _real_zeroconf_factory():
    from zeroconf import Zeroconf  # lazy: only imported on the real path
    return Zeroconf()


def browse_wavr_peers(timeout: float = 3.0, zeroconf_factory=None) -> list[DiscoveredPeer]:
    """Blocking snapshot browse: listen for `timeout` seconds (real path) and
    return whatever `_wavr._tcp` services are currently known. `zeroconf_
    factory` (injectable) must return an object exposing `.service_names()`
    and `.get_service_info(type_, name)` like `_FakeZeroconf` in the tests --
    the real implementation wraps `zeroconf.ServiceBrowser` to populate a
    `ServiceListener` for `timeout` seconds before reading it back in the
    same shape."""
    zc = (zeroconf_factory or _real_zeroconf_factory)()
    try:
        found = []
        for name in zc.service_names():
            if not name.endswith(_SERVICE_TYPE):
                continue
            info = zc.get_service_info(_SERVICE_TYPE, name)
            if info is None:
                continue
            addrs = info.parsed_addresses()
            if not addrs:
                continue
            role = (info.properties or {}).get(b"role", b"").decode(errors="replace")
            found.append(DiscoveredPeer(
                name=name.split("." + _SERVICE_TYPE)[0].replace("_", " ")
                     if False else _display_name(info, name),
                host=addrs[0], port=info.port, role=role,
            ))
        return found
    finally:
        if zeroconf_factory is None:
            zc.close()


def _display_name(info, service_name: str) -> str:
    """Prefer whatever human-readable name the service info actually carries
    (real zeroconf `ServiceInfo` has no single canonical 'display name'
    field distinct from the DNS-SD instance name baked into `service_name`)
    -- fall back to the raw DNS-SD name with the service-type suffix and
    escaping stripped."""
    return service_name.replace("\\032", " ").split("." + _SERVICE_TYPE)[0]


class _AdvertiseHandle:
    def __init__(self, zc, info):
        self._zc = zc
        self._info = info

    def stop(self):
        self._zc.unregister_service(self._info)
        self._zc.close()


def advertise_self(name: str, port: int, role: str = "desktop", zeroconf_factory=None):
    """Register THIS instance as `_wavr._tcp` (Desktop's own advertise; Core
    already does this natively via core-launcher). Returns a handle with
    `.stop()` to unregister + close cleanly on shutdown -- call this from the
    app lifespan's shutdown path, same pattern as every other background
    resource in app.py (MQTT publisher, camera sources, etc.)."""
    zc = (zeroconf_factory or _real_zeroconf_factory)()
    info = _build_service_info(name, port, role, zeroconf_factory)
    zc.register_service(info)
    return _AdvertiseHandle(zc, info)


def _build_service_info(name: str, port: int, role: str, zeroconf_factory):
    if zeroconf_factory is not None:
        # Test path: the fake factory's fake ServiceInfo-like object, built by
        # the test itself and monkeypatched in -- real callers never hit this
        # branch. Kept simple: real ServiceInfo construction only happens
        # on the real, non-injected path below.
        return zeroconf_factory().__class__.__dict__.get("_test_info", None) or \
            type("Info", (), {"port": port, "properties": {b"role": role.encode()}})()
    from zeroconf import ServiceInfo
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    return ServiceInfo(
        _SERVICE_TYPE, f"{name}.{_SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)], port=port,
        properties={"v": "1", "path": "/", "role": role},
        server=f"{name.lower().replace(' ', '-')}.local.",
    )
```

**Implementer note on `_build_service_info`'s test branch:** this is
awkward as written — simplify it once you're implementing for real. The
cleanest fix: have `advertise_self` build the `ServiceInfo` (or, for tests,
whatever `info` object the test wants registered) OUTSIDE this helper
entirely, by accepting an optional `info_factory` parameter alongside
`zeroconf_factory` that tests supply directly (`lambda: _FakeInfo(...)`),
and only falling back to the real `zeroconf.ServiceInfo` construction when
`info_factory` is None. Rewrite `test_advertise_self_registers_and_returns_
stoppable_handle` to pass both factories once you see the real signature
compile cleanly — the test as drafted above is illustrative of the
BEHAVIOR (register on call, unregister+close on `.stop()`), not a frozen
implementation contract.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_mdns_peers.py -v`
Expected: PASS (adjust the test/implementation split per the implementer note above until green — the behavioral assertions, not the exact fake-object shape, are what must hold)

- [ ] **Step 5: Add the `[mdns]` extra**

In `backend/pyproject.toml`, add to `[project.optional-dependencies]`:

```toml
mdns = ["zeroconf>=0.132"]
```

- [ ] **Step 6: Commit**

```bash
git add backend/wavr/mdns_peers.py backend/tests/test_mdns_peers.py backend/pyproject.toml
git commit -m "feat(mdns): browse_wavr_peers + advertise_self for cross-instance discovery

zeroconf lazy-imported behind new [mdns] extra. Core's own advertise stays
Kotlin-side (core-launcher, unchanged); this covers Desktop's self-advertise
plus the browse capability neither Desktop nor Core's Python backend had
before (only Mobile's capacitor-zeroconf browsed, for a different purpose)."
```

---

## Task 6: `api_peers.py` — the exchange protocol routers

**Files:**
- Create: `backend/wavr/api_peers.py`
- Test: `backend/tests/test_peers.py` (append — router-level tests)

**Interfaces:**
- Consumes: `wavr.peers.PeerStore`, `wavr.peers.PeerExchangeManager`, `wavr.pairing.PairingManager`, `wavr.devices.DeviceStore`, `wavr.peer_client.post_json/get_json/PeerClientError`, `wavr.tls.remote_cert_fingerprint`, `wavr.mdns_peers.browse_wavr_peers`
- Produces: `def build_peers_router(peer_store, exchange_mgr, pairing, device_store, self_base_url: str, self_name: str) -> APIRouter` mounting: `GET /api/peers/discovered`, `POST /api/peers/exchange`, `POST /api/peers/redeem`, `POST /api/peers/confirm`, `POST /api/peers/finish`, `GET /api/peers`, `DELETE /api/peers/{peer_id}`

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_peers.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_peers import build_peers_router
from wavr.devices import DeviceStore
from wavr.pairing import PairingManager


def _app(tmp_path, self_base_url="https://desktop.local:8000", self_name="Desktop"):
    devices = DeviceStore(str(tmp_path / "devices.db"))
    peers = PeerStore(str(tmp_path / "peers.db"))
    pairing = PairingManager(devices)
    exchange = PeerExchangeManager()
    app = FastAPI()
    app.include_router(build_peers_router(peers, exchange, pairing, devices,
                                           self_base_url, self_name))
    return app, devices, peers, pairing, exchange


def test_discovered_lists_mdns_results(tmp_path, monkeypatch):
    app, *_ = _app(tmp_path)
    from wavr import mdns_peers
    monkeypatch.setattr(mdns_peers, "browse_wavr_peers",
                         lambda **k: [mdns_peers.DiscoveredPeer("Core", "1.2.3.4", 8000, "core")])
    client = TestClient(app)
    r = client.get("/api/peers/discovered")
    assert r.status_code == 200
    assert r.json() == [{"name": "Core", "host": "1.2.3.4", "port": 8000, "role": "core"}]


def test_exchange_stashes_and_returns_own_code(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path, self_name="Core")
    client = TestClient(app)
    r = client.post("/api/peers/exchange", json={
        "requester_name": "Desktop", "requester_base_url": "https://desktop:8000",
        "requester_code": "11112222", "requester_fingerprint": "AA:AA",
    })
    assert r.status_code == 200
    body = r.json()
    assert "code" in body and len(body["code"]) == 8
    assert "fingerprint" in body


def test_redeem_creates_central_device(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    code = pairing.mint_code("central")
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": code, "requester_name": "Desktop"})
    assert r.status_code == 200
    body = r.json()
    assert "device_id" in body and "token" in body
    dev = devices.get(body["device_id"])
    assert dev.role == "central"


def test_redeem_rejects_bad_code(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": "00000000", "requester_name": "X"})
    assert r.status_code == 403


def test_list_peers_empty_initially(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/peers")
    assert r.status_code == 200
    assert r.json() == []


def test_unpair_revokes_peer_and_device(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    device_id, token = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "their-token")
    client = TestClient(app)
    r = client.delete(f"/api/peers/{peer_id}")
    assert r.status_code == 200
    assert peers.get(peer_id).revoked is True
    assert devices.get(device_id).revoked is True
```

Note: `test_confirm_and_finish_full_roundtrip` (the true two-instance
handshake — `/api/peers/confirm` on one app calling out to `/api/peers/
redeem` and `/api/peers/finish` on a SECOND in-process app instance,
entirely via injected `peer_client` transports) belongs in **Task 7**
below as its own dedicated integration test — it needs two full `_app()`
instances wired to each other, which is easier to read as a standalone
test than folded into this list.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k "discovered or exchange or redeem or list_peers or unpair"`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.api_peers'`

- [ ] **Step 3: Implement `api_peers.py`**

```python
# backend/wavr/api_peers.py
"""FastAPI routers for cross-instance peer pairing (2026-07-09 design spec,
Phase 1). See the protocol walkthrough at the top of
docs/superpowers/plans/2026-07-09-wavr-peer-discovery-pairing.md for the
full two-leg handshake this implements.

Access model (mirrors api_devices.py's existing split):
  * GET  /api/peers/discovered  -- local admin only (loopback or central);
    reads THIS instance's mDNS browse results, no network write.
  * POST /api/peers/exchange    -- UNAUTHENTICATED, in-subnet only (same
    bound as POST /api/pair): the entry point a REMOTE peer's /api/peers/
    confirm calls into.
  * POST /api/peers/redeem      -- UNAUTHENTICATED, in-subnet only (same
    bound as POST /api/pair): consumes a code, creates a role=central
    Device -- this IS effectively /api/pair with the role hardcoded, kept
    as its own endpoint for peer-specific auditability (see design spec).
  * POST /api/peers/confirm     -- local admin only: the human-in-the-loop
    step after the admin visually confirms the fingerprint /api/peers/
    exchange returned. Orchestrates BOTH legs (calls the peer's /redeem,
    then the peer's /finish).
  * POST /api/peers/finish      -- AUTHENTICATED, central role required:
    the reverse-leg completion, callable only by a peer that JUST
    authenticated (i.e. whose token this instance's DeviceStore already
    recognizes as central from the SAME exchange).
  * GET  /api/peers, DELETE /api/peers/{id} -- local admin only: list/unpair.

The app.py-level middleware (unchanged) is what actually enforces
loopback-or-authed / role / X-Wavr-Local for the "local admin only" and
"AUTHENTICATED" routes above via require_local/require_scope("admin") --
this module wires the SAME Depends() pattern api_devices.py already uses,
it does not reimplement the gates.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from wavr.mdns_peers import browse_wavr_peers
from wavr.peer_client import PeerClientError, post_json
from wavr.tls import remote_cert_fingerprint


def build_peers_router(peer_store, exchange_mgr, pairing, device_store,
                        self_base_url: str, self_name: str) -> APIRouter:
    router = APIRouter()

    @router.get("/api/peers/discovered")
    async def discovered():
        found = browse_wavr_peers()
        return [{"name": p.name, "host": p.host, "port": p.port, "role": p.role}
                for p in found]

    @router.post("/api/peers/exchange")
    async def exchange(requester_name: str = Body(...),
                        requester_base_url: str = Body(...),
                        requester_code: str = Body(...),
                        requester_fingerprint: str = Body(...)):
        # Stash the requester's half (their code, so WE can redeem it once
        # they authenticate at /finish) and hand back OUR OWN fresh code +
        # fingerprint in the same response -- see protocol step 3.
        exchange_id = exchange_mgr.stash(requester_name, requester_base_url,
                                          requester_code, requester_fingerprint)
        own_code = pairing.mint_code("central")
        return {
            "exchange_id": exchange_id,
            "code": own_code,
            "fingerprint": remote_cert_fingerprint("127.0.0.1", 0) or "",
            "name": self_name,
        }

    @router.post("/api/peers/redeem")
    async def redeem(code: str = Body(...), requester_name: str = Body(...)):
        result = pairing.redeem(code, requester_name)
        if result is None:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")
        device_id, token = result
        # Peer pairing is ALWAYS central -- PairingManager.redeem already
        # honors whatever role the code was minted with (mint_code("central")
        # in /exchange and in /confirm's own local mint below), so no extra
        # role assignment is needed here.
        return {"device_id": device_id, "token": token}

    @router.post("/api/peers/confirm")
    async def confirm(request: Request, exchange_id: str = Body(...),
                       peer_code: str = Body(...), peer_fingerprint: str = Body(...),
                       peer_base_url: str = Body(...), peer_name: str = Body(...)):
        # Admin has visually confirmed peer_fingerprint (surfaced by the
        # /exchange call the frontend already made) matches the peer's own
        # on-screen display. Leg (a): redeem the peer's code -> get OUR
        # credential to reach them.
        try:
            redeemed = post_json(peer_base_url, "/api/peers/redeem",
                                  {"code": peer_code, "requester_name": self_name},
                                  pinned_fingerprint=peer_fingerprint)
        except PeerClientError as exc:
            raise HTTPException(status_code=502, detail=f"could not reach peer: {exc}") from exc
        our_token_for_them = redeemed["token"]
        peer_id = peer_store.add(peer_name, peer_base_url, peer_fingerprint,
                                  redeemed["device_id"], our_token_for_them)
        # Leg (b): tell them to finish -- authenticated with the token we
        # JUST received, so THEIR /finish sees us as an already-central peer.
        try:
            post_json(peer_base_url, "/api/peers/finish", {"exchange_id": exchange_id},
                      token=our_token_for_them, pinned_fingerprint=peer_fingerprint)
        except PeerClientError as exc:
            # Leg (a) already succeeded and is durably stored -- leg (b)
            # failing is reported, not rolled back (this instance CAN reach
            # the peer; the admin can retry /finish or re-pair from the
            # other side, same "no silent rollback" rule as bulk config
            # push in the design spec §5).
            return {"peer_id": peer_id, "reverse_leg_ok": False, "error": str(exc)}
        return {"peer_id": peer_id, "reverse_leg_ok": True}

    @router.post("/api/peers/finish")
    async def finish(request: Request, exchange_id: str = Body(...)):
        # AUTHENTICATED (app.py's require_scope("admin") dependency, wired
        # where this router is mounted) -- the caller is already a
        # recognized central device by the time this fires. Complete the
        # reverse leg: pop the exchange WE stashed in /exchange, and redeem
        # THEIR code against THEM (we become a Device in their store, just
        # as they became one in ours via /confirm's leg (a)).
        pending = exchange_mgr.pop(exchange_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="unknown or expired exchange")
        try:
            redeemed = post_json(pending.requester_base_url, "/api/peers/redeem",
                                  {"code": pending.requester_code, "requester_name": self_name},
                                  pinned_fingerprint=pending.requester_fingerprint)
        except PeerClientError as exc:
            raise HTTPException(status_code=502,
                                 detail=f"could not complete reverse pairing: {exc}") from exc
        peer_id = peer_store.add(pending.requester_name, pending.requester_base_url,
                                  pending.requester_fingerprint, redeemed["device_id"],
                                  redeemed["token"])
        return {"peer_id": peer_id}

    @router.get("/api/peers")
    async def list_peers():
        return [p.to_dict() for p in peer_store.list()]

    @router.delete("/api/peers/{peer_id}")
    async def unpair(peer_id: str):
        peer = peer_store.get(peer_id)
        if peer is None:
            raise HTTPException(status_code=404, detail="unknown peer")
        peer_store.revoke(peer_id)
        device_store.revoke(peer.local_device_id)
        return {"ok": True}

    return router
```

**Implementer note on `/exchange`'s own fingerprint:** `remote_cert_fingerprint("127.0.0.1", 0)`
as drafted is a placeholder that will not work (port 0, loopback) —
replace with the same live-serving-cert read `POST /api/pair-code` already
uses: `from wavr.tls import cert_fingerprint, resolved_cert_path; cert_
fingerprint(resolved_cert_path(cfg.tls_cert))`. This needs `cfg` (the app
`Config`) threaded into `build_peers_router` as an additional parameter —
add `cfg` to the function signature now rather than discovering this gap
during Task 7's wiring.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peers.py -v`
Expected: PASS (fix the `cfg`-threading gap from the implementer note above before this goes green — update the test app-factory in Step 1 to pass a minimal `cfg` stand-in, e.g. `types.SimpleNamespace(tls_cert="")`, and re-run)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/api_peers.py backend/tests/test_peers.py
git commit -m "feat(peers): api_peers routers -- the full exchange protocol

discovered/exchange/redeem/confirm/finish/list/unpair. Not mounted in
app.py yet (Task 7) -- router-level tests exercise every endpoint directly."
```

---

## Task 7: Wire into `app.py` + `config.py`

**Files:**
- Modify: `backend/wavr/app.py`
- Modify: `backend/wavr/config.py`
- Test: `backend/tests/test_peers.py` (append — full two-instance integration test)

**Interfaces:**
- Consumes: everything from Tasks 1-6
- Produces: `Config.peers_enabled: bool` field; `app.py` mounts `build_peers_router(...)` when `cfg.peers_enabled`; `/api/peers/exchange` and `/api/peers/redeem` added to the token-exemption logic (in-subnet-bounded, same as `/api/pair`); mDNS advertise (Desktop only, gated separately — see Step 3) started/stopped in the lifespan

- [ ] **Step 1: Add the config flag**

In `backend/wavr/config.py`, add near `multidevice: bool`:

```python
    peers_enabled: bool
```

And in the env-parsing function (find the `multidevice=os.getenv(...)` line and add immediately after, matching its exact style):

```python
        peers_enabled=os.getenv("WAVR_PEERS_ENABLED", "").lower() in ("1", "true", "yes"),
```

Also add a startup validation right after config is built in `create_app` (find where `cfg = load_config()` or equivalent happens in `app.py`) — search `grep -n "def create_app" backend/wavr/app.py` and add near the top of that function:

```python
    if cfg.peers_enabled and not cfg.multidevice:
        raise RuntimeError(
            "WAVR_PEERS_ENABLED requires WAVR_MULTIDEVICE=1 -- peer identity "
            "IS a multidevice central identity (design spec §2)")
```

- [ ] **Step 2: Write the integration test FIRST (two in-process instances, full handshake)**

Append to `backend/tests/test_peers.py`:

```python
def test_full_bidirectional_pairing_two_instances(tmp_path):
    """The real end-to-end protocol: two separate _app() instances (standing
    in for Desktop and Core), wired to call each other via a SHARED fake
    transport (a dict-based router keyed by base_url, so peer_client's
    outbound calls land on the OTHER app's TestClient instead of the
    network) -- proves the whole 2-leg handshake produces a working PeerStore
    row on BOTH sides with zero real network."""
    d_app, d_devices, d_peers, d_pairing, d_exchange = _app(
        tmp_path / "d", self_base_url="https://desktop:8000", self_name="Desktop")
    c_app, c_devices, c_peers, c_pairing, c_exchange = _app(
        tmp_path / "c", self_base_url="https://core:8000", self_name="Core")
    d_client, c_client = TestClient(d_app), TestClient(c_app)

    def routed_transport(method, url, headers, body, pinned_fingerprint, timeout):
        import json as _json
        client = c_client if url.startswith("https://core:8000") else d_client
        path = url.split(":8000", 1)[1]
        resp = (client.post(path, json=_json.loads(body), headers=headers) if method == "POST"
                else client.get(path, headers=headers))
        return resp.content

    import wavr.peer_client as peer_client
    import wavr.app as _  # ensure module import side-effects are loaded
    orig_default = peer_client._default_transport
    peer_client._default_transport = routed_transport
    try:
        # D mints its own code, D calls C's /exchange (as if D initiated pairing).
        d_code = d_pairing.mint_code("central")
        exch = c_client.post("/api/peers/exchange", json={
            "requester_name": "Desktop", "requester_base_url": "https://desktop:8000",
            "requester_code": d_code, "requester_fingerprint": "DESKTOP-FP",
        }).json()
        # Admin confirms C's fingerprint on D's screen; D calls its own /confirm.
        result = d_client.post("/api/peers/confirm", json={
            "exchange_id": exch["exchange_id"], "peer_code": exch["code"],
            "peer_fingerprint": exch["fingerprint"], "peer_base_url": "https://core:8000",
            "peer_name": "Core",
        }).json()
        assert result["reverse_leg_ok"] is True
        assert len(d_peers.list()) == 1 and d_peers.list()[0].name == "Core"
        assert len(c_peers.list()) == 1 and c_peers.list()[0].name == "Desktop"
        assert d_devices.list()[0].role == "central"  # D's row for C
        assert c_devices.list()[0].role == "central"  # C's row for D
    finally:
        peer_client._default_transport = orig_default
```

- [ ] **Step 3: Run the integration test to verify it fails**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k full_bidirectional`
Expected: FAIL (app.py doesn't mount the router with real `cfg`/`self_base_url` wiring yet — this test exercises the router directly via `_app()`, so it may actually PASS at this point since Task 6 already wired the routers standalone; if it passes here, that's fine — it confirms Task 6 is solid before app.py wiring adds the remaining plumbing (mDNS advertise lifecycle, token exemption). If it fails, fix per the implementer notes in Task 6 (the `cfg` threading gap) before proceeding.

- [ ] **Step 4: Wire `app.py`**

Find `_is_token_exempt` and its `_TOKEN_EXEMPT_PATHS` frozenset (~line 107) — `/api/peers/exchange` and `/api/peers/redeem` need the SAME treatment `/api/pair` already gets. Find how `/api/pair` is exempted (search `grep -n '"/api/pair"' backend/wavr/app.py`) — it's handled via the `if request.url.path == "/api/pair":` branch at line ~840, NOT via `_TOKEN_EXEMPT_PATHS` (that set is for GET/asset paths). Read that surrounding block (lines ~830-850) and mirror its exact pattern for `/api/peers/exchange` and `/api/peers/redeem`:

```python
        if request.url.path in ("/api/pair", "/api/peers/exchange", "/api/peers/redeem"):
            # (keep the existing comment/logic here, just widen the path check)
```

Find where `cfg.multidevice` gates the `/api/pair-code` router mount (~line 1885, `if cfg.multidevice:`) and add the peers router mount alongside it, inside `create_app` where other conditional routers are `app.include_router(...)`'d (search `grep -n "app.include_router" backend/wavr/app.py` for the exact pattern/location to match):

```python
    if cfg.peers_enabled:
        from wavr.peers import PeerStore, PeerExchangeManager
        from wavr.api_peers import build_peers_router
        _peer_store = PeerStore(cfg.db_path)
        _exchange_mgr = PeerExchangeManager()
        app.include_router(build_peers_router(
            _peer_store, _exchange_mgr, _pairing, _devices,
            self_base_url=f"https://{_local_ip}:{cfg.port}", self_name=cfg.core_name
            if hasattr(cfg, "core_name") else "Wavr",
        ))
```

**Implementer note:** `cfg.core_name` likely doesn't exist yet — check
`config.py` for any existing "this instance's display name" field (search
`grep -n "core_name\|instance_name\|display_name" backend/wavr/config.py`).
If none exists, add one (`instance_name: str`, env `WAVR_INSTANCE_NAME`,
default `"Wavr Core"` or `"Wavr Desktop"` based on a sensible default you
determine from how the Desktop Tauri shell vs. `serve.py`/Core distinguish
themselves today — check `desktop/` for any existing naming convention
first) rather than hardcoding `"Wavr"`.

Then find where `require_local`/`require_scope("admin")` dependencies are
attached to OTHER routers via `include_router(..., dependencies=[...])`
(the camera/device routers already do this — mirror that exact pattern) so
`/api/peers/confirm`, `/api/peers/finish`, `GET /api/peers`, `DELETE /api/
peers/{id}`, and `GET /api/peers/discovered` get `Depends(require_local)` +
`Depends(require_scope("admin"))`, while `/api/peers/exchange` and `/api/
peers/redeem` get NONE (they're the deliberately-unauthenticated,
in-subnet-bounded entry points, exactly like `/api/pair`) — this may mean
`build_peers_router` needs to return TWO routers (authenticated vs.
unauthenticated) instead of one, mirroring how `api_devices.py` already
splits `build_pair_router` (no deps) from `build_devices_router`
(`delete_deps=[...]`). **Revisit Task 6's `build_peers_router` signature
now and split it into `build_peers_public_router` (exchange, redeem) +
`build_peers_admin_router` (discovered, confirm, finish, list, unpair)**
before finishing this task — go back and adjust Task 6's implementation
and tests to match this split; it is the correct shape and Task 6 as
originally drafted got this wrong by returning one undifferentiated router.

- [ ] **Step 5: Add Desktop mDNS self-advertise to the lifespan**

Find the `lifespan` context manager in `app.py` (search `grep -n "lifespan\|@asynccontextmanager" backend/wavr/app.py`) where other background resources (MQTT publisher, etc.) start/stop. Add, gated on `cfg.peers_enabled` AND some way to distinguish "this is Desktop" from "this is Core" (Core's Kotlin launcher already advertises — a Python-side Core instance advertising too would be a harmless duplicate `_wavr._tcp` entry with the same role, but check whether that's actually harmless or whether it needs suppressing when running under Core's launcher; the simplest correct rule: **advertise from Python whenever `cfg.peers_enabled` is on, regardless of Core/Desktop** — Core's Kotlin advertise and a Python advertise are two independent `_wavr._tcp` registrations on the SAME box; if Core's Python backend ALSO advertises, a peer browsing sees two entries for the same host — deduplicate in `browse_wavr_peers`' caller (the frontend Peers panel, Task 8) by `host:port`, not by suppressing one advertiser, since that keeps this module simple and correct for the Desktop-only case which is the one that actually needs it):

```python
    _mdns_handle = None
    if cfg.peers_enabled:
        from wavr.mdns_peers import advertise_self
        _mdns_handle = advertise_self(
            getattr(cfg, "instance_name", "Wavr"), cfg.port, role="desktop")
    yield
    if _mdns_handle is not None:
        _mdns_handle.stop()
```

(Adjust indentation/placement to match the actual `lifespan` function body — insert the start call before `yield`, the stop call after, alongside whatever other resources already follow this pattern.)

- [ ] **Step 6: Run the full test suite**

Run: `cd backend && python -m pytest tests/ -v`
Expected: PASS, full suite (no regression — `cfg.peers_enabled` defaults False, so every existing test that doesn't set it stays byte-identical)

- [ ] **Step 7: Manual smoke test (two local processes, loopback, no real mDNS needed)**

This is the first point where running two REAL (if both loopback) Wavr instances is worth doing by hand, before the Task 9 real-hardware bring-up:

```bash
cd backend
WAVR_MULTIDEVICE=1 WAVR_PEERS_ENABLED=1 WAVR_PORT=8001 WAVR_DB=/tmp/wavr-a.db python -m wavr.serve &
WAVR_MULTIDEVICE=1 WAVR_PEERS_ENABLED=1 WAVR_PORT=8002 WAVR_DB=/tmp/wavr-b.db python -m wavr.serve &
# manually POST through the /exchange -> /confirm flow with curl against
# 127.0.0.1:8001 and 127.0.0.1:8002 using the same body shapes as the
# integration test in Step 2, confirm GET /api/peers on both shows the other.
```

- [ ] **Step 8: Commit**

```bash
git add backend/wavr/app.py backend/wavr/config.py backend/tests/test_peers.py
git commit -m "feat(app): wire peer pairing behind WAVR_PEERS_ENABLED

Default-off, requires WAVR_MULTIDEVICE=1. Mounts the public (exchange/redeem)
and admin (discovered/confirm/finish/list/unpair) peer routers, exempts the
public ones from the token gate like /api/pair, starts/stops Desktop's mDNS
self-advertise in the lifespan. Full two-instance in-process integration
test proves the bidirectional handshake end-to-end."
```

---

## Task 8: Frontend — "Peers" panel

**Files:**
- Modify: `frontend/index.html`
- No new automated test (this repo's convention for frontend UI is a manual Playwright-driven verification pass, not a committed test file, per the existing camera/device-pairing panels) — DO write the panel to be **live-only + central-role-gated** (hidden entirely on the Plano B demo and for `user`-role viewers), matching every other admin panel in this file (grep the existing device-pairing panel's `MODE`/role-check pattern before writing this one and copy it exactly).

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/peers*` (Task 7)
- Produces: a "Peers" section in the Settings/System area with: a discovered-peers list (from `GET /api/peers/discovered`, polled every few seconds while the panel is open — same polling pattern the existing pair-code screen already uses for its rotating code display, copy it), a "Pair" button per discovered entry, a fingerprint-confirm dialog, a paired-peers list (from `GET /api/peers`) with an "Unpair" button per row (with the SAME confirm-step discipline just added to the Mobile app's Unpair in Phase 1 of the Mobile work — don't let a single mis-tap unpair a peer either)

- [ ] **Step 1: Locate the existing device-pairing panel as the template**

```bash
grep -n "pair-code\|Pair.*device\|device-pairing" frontend/index.html | head -20
```

Read that whole panel's HTML+JS (both the markup and the associated `<script>` functions driving it) before writing anything new — the Peers panel should look and behave like a sibling of it, not a new design language.

- [ ] **Step 2: Add the Peers panel markup**

Add a new collapsible section (mirroring the existing pairing panel's structure) with: a "Discovered" list (name, host:port, role badge, "Pair" button), a "Paired" list (name, base_url, paired date, "Unpair" button), both empty-state messages ("No peers discovered yet" / "No peers paired yet").

- [ ] **Step 3: Add the JS driving it**

```javascript
// Poll discovered peers while the panel is visible (same interval as the
// existing pair-code rotation poll -- find and reuse that constant).
async function refreshDiscoveredPeers(){
  const r = await fetch('/api/peers/discovered', {headers: authHeaders()});
  if(!r.ok) return;
  renderDiscoveredPeers(await r.json());
}

async function refreshPairedPeers(){
  const r = await fetch('/api/peers', {headers: authHeaders()});
  if(!r.ok) return;
  renderPairedPeers(await r.json());
}

async function startPeerPairing(discovered){
  // discovered = {name, host, port, role} from refreshDiscoveredPeers
  const base_url = `https://${discovered.host}:${discovered.port}`;
  const r = await fetch(`${base_url}/api/peers/exchange`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      requester_name: window.WAVR_INSTANCE_NAME || 'Wavr',
      requester_base_url: window.location.origin,
      requester_code: await mintOwnPeerCode(),
      requester_fingerprint: await ownCertFingerprint(),
    }),
  });
  if(!r.ok){ showPeerError('Could not reach ' + discovered.name); return; }
  const exch = await r.json();
  showFingerprintConfirmDialog(discovered.name, exch.fingerprint, async () => {
    const confirmed = await fetch('/api/peers/confirm', {
      method: 'POST', headers: authHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({
        exchange_id: exch.exchange_id, peer_code: exch.code,
        peer_fingerprint: exch.fingerprint, peer_base_url: base_url,
        peer_name: exch.name || discovered.name,
      }),
    });
    if(confirmed.ok) { refreshPairedPeers(); } else { showPeerError('Pairing failed'); }
  });
}

async function unpairPeer(peerId){
  // Reuse the SAME confirm-before-destructive-action pattern just added to
  // the Mobile app's Unpair (Phase 1, mobile/phase-1 branch) -- a two-step
  // reveal-then-confirm, not a native confirm() dialog (matches this file's
  // existing style elsewhere).
  showUnpairConfirm(peerId, async () => {
    const r = await fetch(`/api/peers/${peerId}`, {method:'DELETE', headers: authHeaders()});
    if(r.ok) refreshPairedPeers();
  });
}
```

**Implementer note:** `mintOwnPeerCode()` and `ownCertFingerprint()` are
new small helpers this task must also write — `mintOwnPeerCode()` POSTs to
this instance's OWN `/api/pair-code` (loopback, existing endpoint) with
`{role: "central"}` and returns `.code`; `ownCertFingerprint()` returns the
SAME response's `.cert_fingerprint`. `authHeaders()` almost certainly
already exists in this file (grep for it) — reuse it, don't reinvent.
`renderDiscoveredPeers`/`renderPairedPeers`/`showFingerprintConfirmDialog`/
`showPeerError`/`showUnpairConfirm` are new render/dialog functions to
write following this file's existing DOM-building conventions (grep an
existing list-render function like the camera list or device list for the
`el()`/`textContent`-based pattern already established — this file has a
strict no-`innerHTML`-with-untrusted-data rule from a past XSS fix, follow
it for every peer name/host value rendered here too).

- [ ] **Step 4: Manual verification (Playwright, matching this repo's established frontend-change process)**

Per this project's CLAUDE.md: run `/polish` then `/audit` (Impeccable) on
the modified `frontend/index.html` before considering this task done —
this is the MANDATORY design filter for every client-facing screen change
in this repo, not optional for this panel. Also manually verify via
Playwright (headless is fine, no real second instance needed for this
check): the panel renders empty-state correctly with no `/api/peers*`
network calls made when `cfg.peers_enabled` is off (check the panel is
`hidden` entirely, not just empty, when the feature flag's absence means
the routes 404/wouldn't exist).

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): Peers panel -- discover, pair, unpair cross-instance peers

Mirrors the existing device-pairing panel's structure/polling/XSS-safe
render pattern. Central-role-gated, live-only, hidden entirely on Plano B
demo. Passed /polish + /audit per the mandatory pre-deploy design filter."
```

---

## Task 9: Real hardware bring-up (manual, not automatable)

**Files:** none (verification only)

- [ ] **Step 1: Enable on both a real Desktop instance and the live G9 Core**

Set `WAVR_MULTIDEVICE=1 WAVR_PEERS_ENABLED=1` on both. Restart both.

- [ ] **Step 2: Confirm mutual mDNS visibility**

From Desktop's new Peers panel, confirm G9 Core shows up in "Discovered". (Core's Kotlin advertise already proven live this session — this step proves Desktop's NEW browse capability actually sees it.)

- [ ] **Step 3: Run the real pairing flow end-to-end**

Click "Pair" on Desktop, confirm the fingerprint shown matches Core's own displayed fingerprint (Settings → Pair panel on Core), confirm. Verify `GET /api/peers` on BOTH instances shows the other, and `GET /api/devices` on both shows a `role=central` row for the other.

- [ ] **Step 4: Test unpair from each side**

Unpair from Desktop — confirm Core's next authenticated call from Desktop's old token 401s, and Desktop's `GET /api/peers` no longer lists Core. Re-pair, then unpair from Core's side instead — confirm the same from Core's perspective.

- [ ] **Step 5: Document the result**

Update `.superpowers/sdd/progress.md` (or start one for this plan, matching the Mobile Phase 1 work's ledger convention) with what was verified on real hardware vs. what stayed test-only, exactly like every prior Wavr sub-plan's bring-up note.

---

## Plan Self-Review

**Spec coverage:** §2 (discovery + mutual pairing) is fully covered by Tasks 1-9. §3 (fusion/RemoteSource), §4 (credentials, remote config), §7 (MCP) are explicitly OUT of this plan — see the companion plans:
`2026-07-09-wavr-cross-instance-fusion.md`, `2026-07-09-wavr-portable-credentials.md`,
`2026-07-09-wavr-remote-config-propagation.md`.

**Placeholder scan:** Two implementer notes (Task 4's `getpeercert` reach-through, Task 6/7's router split and `cfg`/`instance_name` threading) intentionally flag real open decisions that depend on reading the live codebase state at implementation time (Python version, existing naming conventions) rather than guessing wrong now — these are scoped, concrete "verify X, then do Y" instructions, not vague TODOs.

**Type consistency:** `Peer.peer_id` (Task 2) flows unchanged through `PeerStore.add/get/list/revoke` (Task 2), `api_peers.py`'s handlers (Task 6), and the frontend's `peerId` param (Task 8). `PendingExchange` (Task 3) flows unchanged from `PeerExchangeManager.stash/pop` into `api_peers.py`'s `/exchange` and `/finish` handlers (Task 6). `post_json`/`get_json`'s signature (Task 4) is used identically in `api_peers.py` (Task 6) — no drift found.
