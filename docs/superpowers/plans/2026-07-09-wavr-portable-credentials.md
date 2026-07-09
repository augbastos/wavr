# Wavr Portable Admin Credentials — Implementation Plan (Phase 3 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin sets ONE credential (PIN, or a passkey) that works the same way on any peer-paired Wavr instance, by their own choice of **local** (this instance only) or **shared** (propagated to peers). Biometric unlock stays local-only by nature (§4.1 of the design spec) — it unlocks the local copy of a portable secret, never travels itself.

**Architecture:** Builds entirely on Phase 1's peer relationships (`PeerStore`, `peer_client.post_json`) — "shared" simply means "also call the same set-credential endpoint on every currently-paired peer, using the admin session's existing peer-to-peer trust." No new distributed protocol. Two credential *kinds* beyond the existing PIN: a Wavr-native local keypair (Ed25519 via the already-available `cryptography` package) transferable between instances by QR, and an opt-in real WebAuthn/passkey path via the `webauthn` PyPI package for admins who accept platform-cloud sync.

**Tech Stack:** `cryptography` (already an extra, `[tls]`) for the local keypair. New `[webauthn]` extra (`webauthn>=2.0`, the well-known `py_webauthn` package — a vetted library, not hand-rolled COSE/CBOR/attestation parsing) for the opt-in cloud path.

## Global Constraints

- **Default-OFF, additive.** Every new route in this plan requires `WAVR_PEERS_ENABLED` (Phase 1) already on — portable credentials are meaningless without a peer to be portable to. A single, peer-less instance keeps using today's plain `POST /api/core/pin` unchanged.
- **Local vs. shared is a per-credential admin choice, never a global mode.** Setting a shared PIN does not force a shared passkey or vice versa (design spec §4.2).
- **The cloud WebAuthn path is opt-in with an explicit external-connection warning, same discipline as the narrator's Gemini option and speedtest** — off by default, one clear toggle, warned at the moment of enabling, never silently.
- **No push.** Local commits only, same as Phases 1-2.
- **Biometric is out of this plan's *implementation* scope for the actual native unlock UI** — Android's side already exists (`core-launcher`'s `BiometricPrompt`, commit `90f027d`); Desktop's native equivalent (Windows Hello / Touch ID via a Tauri plugin) is real work this plan does NOT attempt to write blind, since it depends on the actual Tauri version and plugin ecosystem available at implementation time, which nobody has checked yet. This plan's Task 4 defines the narrow backend contract biometric unlock needs (a local-only "reveal the stored credential" gate) and hands the native UI work off explicitly — see Task 4.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/wavr/pin_store.py` (modify) | Add a `shared: bool` column so the panel/API can show/toggle it (the PIN VALUE itself is unchanged — sharing means "also push this PIN to peers", not a schema change to how it's hashed) |
| `backend/wavr/passkey_store.py` (new) | Local Wavr-native keypair storage (Ed25519 private key, local-only, never leaves this instance except via the explicit QR-transfer flow in Task 3) |
| `backend/wavr/passkey_transfer.py` (new) | The one-time QR-transfer protocol: source instance encrypts the private key to a short-lived transfer code, target instance redeems it once |
| `backend/wavr/webauthn_store.py` (new) | Cloud WebAuthn credential storage (public key + credential ID only — the private key never touches Wavr, that's the whole point of WebAuthn) |
| `backend/wavr/api_credentials.py` (new) | Routers: PIN share-toggle + push, local-passkey register/verify/export/import, WebAuthn registration/authentication ceremony endpoints |
| `backend/wavr/app.py` (modify) | Wire the new routers, gate WebAuthn behind its own explicit env flag |
| `backend/pyproject.toml` (modify) | Add `webauthn = ["webauthn>=2.0"]` extra |
| `frontend/index.html` (modify) | Credential-method picker (PIN/biometric/passkey), local-vs-shared toggle, QR display/scan for local-passkey transfer, WebAuthn opt-in toggle with warning copy |
| `backend/tests/test_pin_sharing.py`, `test_passkey_store.py`, `test_passkey_transfer.py`, `test_webauthn_credentials.py` (new) | Per-module coverage |

---

## Task 1: PIN — local vs. shared

**Files:**
- Modify: `backend/wavr/pin_store.py`
- Modify: `backend/wavr/app.py` (extend `POST /api/core/pin`)
- Create: `backend/wavr/api_credentials.py` (the push-to-peers logic starts here, grows in later tasks)
- Test: `backend/tests/test_pin_sharing.py` (new)

**Interfaces:**
- Consumes: `wavr.peers.PeerStore.list/token_for` (Phase 1), `wavr.peer_client.post_json, PeerClientError` (Phase 1)
- Produces: `PinStore.set_pin(pin, shared: bool = False)` (extended signature, backward-compatible default), `PinStore.is_shared() -> bool`; `def push_pin_to_peers(pin: str, peer_store) -> dict[str, bool]` (peer_id → success) in `api_credentials.py`; `POST /api/core/pin` gains an optional `shared: bool = False` body field

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_pin_sharing.py
import pytest
from wavr.pin_store import PinStore


def test_set_pin_defaults_to_not_shared(tmp_path):
    store = PinStore(str(tmp_path / "p.db"))
    store.set_pin("1234")
    assert store.is_shared() is False


def test_set_pin_shared_true_persists(tmp_path):
    store = PinStore(str(tmp_path / "p.db"))
    store.set_pin("1234", shared=True)
    assert store.is_shared() is True


def test_reset_pin_without_shared_flag_defaults_false_again(tmp_path):
    store = PinStore(str(tmp_path / "p.db"))
    store.set_pin("1234", shared=True)
    store.set_pin("5678")  # re-set, shared not specified
    assert store.is_shared() is False


def test_is_shared_false_when_unset(tmp_path):
    store = PinStore(str(tmp_path / "p.db"))
    assert store.is_shared() is False
```

```python
# backend/tests/test_api_credentials.py (new file, PIN-push section)
from wavr.api_credentials import push_pin_to_peers
from wavr.peers import PeerStore


def test_push_pin_to_peers_reports_per_peer_result(tmp_path, monkeypatch):
    peers = PeerStore(str(tmp_path / "peers.db"))
    ok_id = peers.add("Core", "https://core:8000", "FP1", "dev1", "tok1")
    down_id = peers.add("Desktop2", "https://d2:8000", "FP2", "dev2", "tok2")

    import wavr.api_credentials as mod

    def fake_post_json(base_url, path, body, token=None, pinned_fingerprint=None, **k):
        if "d2" in base_url:
            raise mod.PeerClientError("unreachable")
        assert path == "/api/core/pin"
        assert body == {"pin": "9999", "shared": True}
        return {"set": True}

    monkeypatch.setattr(mod, "post_json", fake_post_json)
    result = push_pin_to_peers("9999", peers)
    assert result == {ok_id: True, down_id: False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_pin_sharing.py tests/test_api_credentials.py -v`
Expected: FAIL — `set_pin` doesn't accept `shared`, `is_shared` doesn't exist, `wavr.api_credentials` doesn't exist

- [ ] **Step 3: Extend `pin_store.py`**

```python
# in the _SCHEMA string, add a column:
_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_pin (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    salt_hex   TEXT NOT NULL,
    hash_hex   TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    updated_ts TEXT NOT NULL,
    shared     INTEGER NOT NULL DEFAULT 0
);
"""
```

Add a migration method (mirror `devices.py`'s `_migrate_scopes_column` exactly) called from `__init__` right after `executescript`:

```python
    def _migrate_shared_column(self) -> None:
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(core_pin)")}
        if "shared" not in cols:
            self._conn.execute("ALTER TABLE core_pin ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
```

Update `set_pin` and add `is_shared`:

```python
    def set_pin(self, pin: str, shared: bool = False) -> None:
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _derive(pin, salt, ITERATIONS)
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO core_pin (id, salt_hex, hash_hex, iterations, updated_ts, shared)"
                " VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   salt_hex = excluded.salt_hex,"
                "   hash_hex = excluded.hash_hex,"
                "   iterations = excluded.iterations,"
                "   updated_ts = excluded.updated_ts,"
                "   shared = excluded.shared",
                (salt.hex(), digest.hex(), ITERATIONS, ts, int(shared)),
            )
            self._conn.commit()

    def is_shared(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT shared FROM core_pin WHERE id = 1").fetchone()
        return bool(row["shared"]) if row else False
```

- [ ] **Step 4: Create `api_credentials.py` with the PIN-push function**

```python
# backend/wavr/api_credentials.py
"""Portable admin credentials (2026-07-09 design spec §4, Phase 3): pushing a
local-vs-shared credential choice out to already-paired peers. Every push
function here follows the SAME per-peer-result, no-silent-retry discipline
as the design spec's remote-config propagation (§5) -- Phase 4 generalizes
this exact pattern to arbitrary settings; this module has the first,
credential-specific instances of it."""
from __future__ import annotations

from wavr.peer_client import PeerClientError, post_json


def push_pin_to_peers(pin: str, peer_store) -> dict:
    """POST the (plaintext, over pinned-HTTPS -- never stored/logged) PIN to
    every non-revoked peer's own /api/core/pin, using OUR credential to
    reach them (peer_store.token_for). Returns {peer_id: True/False} -- the
    admin sees exactly which peers got it and which didn't; no queue, no
    silent retry (design spec §5)."""
    results = {}
    for peer in peer_store.list():
        if peer.revoked:
            continue
        token = peer_store.token_for(peer.peer_id)
        try:
            post_json(peer.base_url, "/api/core/pin", {"pin": pin, "shared": True},
                      token=token, pinned_fingerprint=peer.cert_fingerprint)
            results[peer.peer_id] = True
        except PeerClientError:
            results[peer.peer_id] = False
    return results
```

- [ ] **Step 5: Wire `POST /api/core/pin` to accept + act on `shared`**

In `app.py`, extend the existing handler (~line 1419):

```python
    @app.post("/api/core/pin")
    async def set_core_pin(pin: str = Body(..., embed=True),
                           shared: bool = Body(False, embed=True),
                           _=Depends(require_local), __=Depends(require_scope("admin"))):
        if not isinstance(pin, str) or not _PIN_RE.match(pin):
            raise HTTPException(status_code=400, detail="pin must be 4-12 digits")
        _pin_store.set_pin(pin, shared=shared)
        push_results = {}
        if shared and cfg.peers_enabled:
            from wavr.api_credentials import push_pin_to_peers
            push_results = push_pin_to_peers(pin, _peer_store)
        return {"set": True, "pushed_to_peers": push_results}
```

**Implementer note:** this makes the ALREADY-EXISTING `/api/core/pin`
endpoint do a synchronous fan-out HTTP call to every peer when `shared=True`
— for a handful of peers this is fine (matches the "bulk propagation" UX
the design spec describes as a simple synchronous loop, §4.4), but if
`peer_store.list()` could realistically be large, consider running the
pushes concurrently (`asyncio.gather` over a thread-pooled `push_pin_to_
peers`, since `post_json` is currently synchronous/blocking `urllib`) rather
than serially. For Wavr's realistic peer counts (a handful of Cores/
Desktops in one home) serial is acceptable for v1 — flag this as a known
scaling note, not a blocker.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_pin_sharing.py tests/test_api_credentials.py -v`
Expected: PASS

- [ ] **Step 7: Run the full existing PIN test suite (regression check)**

Run: `cd backend && python -m pytest tests/ -v -k pin`
Expected: PASS, no regression (every pre-existing PIN test still passes `shared` defaulting to `False`)

- [ ] **Step 8: Commit**

```bash
git add backend/wavr/pin_store.py backend/wavr/api_credentials.py backend/wavr/app.py backend/tests/test_pin_sharing.py backend/tests/test_api_credentials.py
git commit -m "feat(credentials): PIN local-vs-shared -- push to peers on set

Additive schema migration (shared column, defaults False -- byte-identical
for every existing install that never sets it). Per-peer push result, no
silent retry queue (design spec §5)."
```

---

## Task 2: Local passkey — Wavr-native keypair

**Files:**
- Create: `backend/wavr/passkey_store.py`
- Test: `backend/tests/test_passkey_store.py` (new)

**Interfaces:**
- Produces: `class PasskeyStore` — `.generate() -> str` (creates a fresh Ed25519 keypair if none exists, stores the private key LOCAL-ONLY, returns the public key as base64), `.has_key() -> bool`, `.sign_challenge(challenge: bytes) -> bytes`, `.public_key_b64() -> str | None`, `.export_private_key() -> bytes | None` (raw private key bytes, used ONLY by `passkey_transfer.py`'s encrypted export — never returned by any HTTP-facing route directly), `.import_private_key(raw: bytes) -> None` (overwrites this instance's key with one transferred from another — used by QR-transfer redeem)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_passkey_store.py
import pytest
from wavr.passkey_store import PasskeyStore


def test_generate_creates_a_key_and_is_idempotent(tmp_path):
    store = PasskeyStore(str(tmp_path / "pk.db"))
    assert store.has_key() is False
    pub1 = store.generate()
    assert store.has_key() is True
    pub2 = store.generate()  # already exists -- returns the SAME key, doesn't rotate silently
    assert pub1 == pub2


def test_sign_challenge_verifiable_with_public_key(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    import base64
    store = PasskeyStore(str(tmp_path / "pk.db"))
    store.generate()
    challenge = b"pair-me-please"
    sig = store.sign_challenge(challenge)
    pub_bytes = base64.b64decode(store.public_key_b64())
    Ed25519PublicKey.from_public_bytes(pub_bytes).verify(sig, challenge)  # raises if invalid


def test_public_key_b64_none_before_generate(tmp_path):
    store = PasskeyStore(str(tmp_path / "pk.db"))
    assert store.public_key_b64() is None


def test_export_then_import_roundtrip_produces_same_public_key(tmp_path):
    src = PasskeyStore(str(tmp_path / "src.db"))
    src.generate()
    exported = src.export_private_key()
    dst = PasskeyStore(str(tmp_path / "dst.db"))
    dst.import_private_key(exported)
    assert dst.public_key_b64() == src.public_key_b64()


def test_export_none_when_no_key(tmp_path):
    store = PasskeyStore(str(tmp_path / "pk.db"))
    assert store.export_private_key() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_passkey_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.passkey_store'`

- [ ] **Step 3: Implement**

```python
# backend/wavr/passkey_store.py
"""Wavr-native local passkey (2026-07-09 design spec §4.1): NOT W3C WebAuthn
(LAN IPs/mDNS names don't satisfy browser RP-ID rules) -- a plain Ed25519
challenge-response keypair, functionally equivalent (private key never
leaves this device except via the explicit, one-time QR transfer in
passkey_transfer.py), portable between a person's OWN instances without any
platform cloud account.

`cryptography` is a LAZY import here (same discipline as tls.py's
`ensure_cert`) -- a base install without the `[tls]` extra never needs this
module touched; only calling `generate()`/`sign_challenge()`/`import_
private_key()` requires it installed. (`[tls]` is reused rather than adding
a new extra -- Ed25519 keypairs are the same `cryptography` package tls.py
already depends on for cert generation.)

Private key stored RAW (not further encrypted at rest) in the SAME
git-ignored wavr.db every other *_store.py uses -- consistent with this
codebase's existing threat model (the db file itself is the trust boundary;
see pin_store.py/devices.py, neither encrypts at rest either, both rely on
the OS-level file permissions + "this box is physically yours" assumption
already documented in the ADRs). If a stronger at-rest guarantee is wanted
later (OS Keystore/Secure Enclave integration), that's a follow-up, not a
silent regression from what's shipped everywhere else in this file today."""
from __future__ import annotations

import base64
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS passkey (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    private_key BLOB NOT NULL
);
"""


class PasskeyStore:
    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def has_key(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM passkey WHERE id = 1").fetchone()
        return row is not None

    def generate(self) -> str:
        """Idempotent: an existing key is never silently rotated (a rotation
        would break every peer that already trusts the old public key --
        rotation, if ever needed, must be an explicit separate action, not a
        side-effect of calling generate() again)."""
        existing = self.public_key_b64()
        if existing is not None:
            return existing
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO passkey (id, private_key) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET private_key = excluded.private_key",
                (raw,))
            self._conn.commit()
        return self.public_key_b64()

    def _load_key(self):
        with self._lock:
            row = self._conn.execute("SELECT private_key FROM passkey WHERE id = 1").fetchone()
        if row is None:
            return None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(row["private_key"])

    def public_key_b64(self) -> str | None:
        key = self._load_key()
        if key is None:
            return None
        from cryptography.hazmat.primitives import serialization
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        return base64.b64encode(pub).decode()

    def sign_challenge(self, challenge: bytes) -> bytes:
        key = self._load_key()
        if key is None:
            raise RuntimeError("no passkey generated yet")
        return key.sign(challenge)

    def export_private_key(self) -> bytes | None:
        with self._lock:
            row = self._conn.execute("SELECT private_key FROM passkey WHERE id = 1").fetchone()
        return row["private_key"] if row else None

    def import_private_key(self, raw: bytes) -> None:
        """Overwrites THIS instance's key with a transferred one -- used
        exactly once, at QR-transfer redeem time (passkey_transfer.py).
        Deliberately allows overwriting an existing key (the whole point of
        transfer is "now this instance also answers to the same passkey as
        the source instance")."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO passkey (id, private_key) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET private_key = excluded.private_key",
                (raw,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_passkey_store.py -v`
Expected: PASS (5/5)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/passkey_store.py backend/tests/test_passkey_store.py
git commit -m "feat(credentials): PasskeyStore -- local Ed25519 keypair, not yet wired to any route

Challenge-response equivalent to a passkey without W3C WebAuthn's RP-ID
domain requirement (LAN instances have no public domain). Private key never
leaves the instance except via the explicit QR transfer (next task)."
```

---

## Task 3: Local-passkey QR transfer

**Files:**
- Create: `backend/wavr/passkey_transfer.py`
- Modify: `backend/wavr/api_credentials.py`
- Test: `backend/tests/test_passkey_transfer.py` (new)

**Interfaces:**
- Consumes: `wavr.passkey_store.PasskeyStore` (Task 2)
- Produces: `class PasskeyTransferManager` (mirrors `PairingManager`'s TTL'd single-use-code shape) — `.mint(private_key: bytes) -> str` (returns a short one-time transfer code), `.redeem(code: str) -> bytes | None`; two routes on `api_credentials.py`: `POST /api/credentials/passkey/export` (admin, local — mints a code + returns it as data the frontend renders into a QR) and `POST /api/credentials/passkey/import` (admin, local — takes a scanned code, calls `PasskeyStore.import_private_key`)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_passkey_transfer.py
import pytest
from datetime import datetime, timedelta, timezone
from wavr.passkey_transfer import PasskeyTransferManager


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += timedelta(seconds=s)


def test_mint_and_redeem_roundtrip():
    mgr = PasskeyTransferManager()
    code = mgr.mint(b"private-key-bytes")
    assert mgr.redeem(code) == b"private-key-bytes"


def test_redeem_is_single_use():
    mgr = PasskeyTransferManager()
    code = mgr.mint(b"key")
    assert mgr.redeem(code) is not None
    assert mgr.redeem(code) is None


def test_redeem_unknown_code_returns_none():
    mgr = PasskeyTransferManager()
    assert mgr.redeem("nope") is None


def test_redeem_expired_returns_none():
    clock = _Clock()
    mgr = PasskeyTransferManager(now_fn=clock, ttl=60)
    code = mgr.mint(b"key")
    clock.advance(61)
    assert mgr.redeem(code) is None
```

```python
# append to backend/tests/test_api_credentials.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from wavr.api_credentials import build_credentials_router
from wavr.passkey_store import PasskeyStore
from wavr.passkey_transfer import PasskeyTransferManager


def test_export_then_import_passkey_via_api(tmp_path):
    src_store = PasskeyStore(str(tmp_path / "src.db"))
    src_store.generate()
    transfer = PasskeyTransferManager()
    src_app = FastAPI()
    src_app.include_router(build_credentials_router(
        pin_store=None, peer_store=None, passkey_store=src_store, transfer_mgr=transfer))
    src_client = TestClient(src_app)

    r = src_client.post("/api/credentials/passkey/export")
    assert r.status_code == 200
    code = r.json()["transfer_code"]

    dst_store = PasskeyStore(str(tmp_path / "dst.db"))
    dst_app = FastAPI()
    dst_app.include_router(build_credentials_router(
        pin_store=None, peer_store=None, passkey_store=dst_store, transfer_mgr=transfer))
    dst_client = TestClient(dst_app)

    r2 = dst_client.post("/api/credentials/passkey/import", json={"transfer_code": code})
    assert r2.status_code == 200
    assert dst_store.public_key_b64() == src_store.public_key_b64()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_passkey_transfer.py -v -k "roundtrip or single_use or unknown or expired"`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.passkey_transfer'`

- [ ] **Step 3: Implement `passkey_transfer.py`**

```python
# backend/wavr/passkey_transfer.py
"""One-time QR transfer of a local passkey's private key between two
instances the SAME admin controls (2026-07-09 design spec §4.1's default
passkey path). In-memory, TTL'd, single-use -- same shape as
pairing.PairingManager's codes, same reasoning: this is a short-lived
handshake artifact (the admin scans a QR within seconds), never a durable
record.

The transfer code itself is what the QR encodes -- short enough to fit
comfortably in a QR at a phone-scan-friendly density, long enough
(128-bit, url-safe) that guessing it during its short TTL is infeasible.
The frontend/native layer renders the code into an actual QR image and
scans it back into a string on the OTHER instance -- this module never
touches image data, only the code string <-> private key bytes mapping."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

TRANSFER_TTL_SECONDS = 60  # short: admin is expected to scan within seconds, not minutes


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PasskeyTransferManager:
    def __init__(self, now_fn=_utcnow, ttl: float = TRANSFER_TTL_SECONDS):
        self._now = now_fn
        self._ttl = ttl
        self._pending: dict[str, tuple[bytes, datetime]] = {}

    def mint(self, private_key: bytes) -> str:
        self._purge_expired()
        code = secrets.token_urlsafe(16)
        self._pending[code] = (private_key, self._now() + timedelta(seconds=self._ttl))
        return code

    def redeem(self, code: str) -> bytes | None:
        entry = self._pending.pop(code, None)
        if entry is None:
            return None
        key, expires = entry
        if self._now() >= expires:
            return None
        return key

    def _purge_expired(self) -> None:
        now = self._now()
        self._pending = {k: v for k, v in self._pending.items() if now < v[1]}
```

- [ ] **Step 4: Implement the `api_credentials.py` router (introduces `build_credentials_router`)**

Append to `backend/wavr/api_credentials.py`:

```python
from fastapi import APIRouter, Body, HTTPException


def build_credentials_router(pin_store, peer_store, passkey_store, transfer_mgr) -> APIRouter:
    """Local-admin-only router (app.py wires require_local + require_scope
    ("admin") on every route here, same pattern as api_devices.py/
    api_peers.py's admin routers -- none of these are safe to expose
    unauthenticated)."""
    router = APIRouter()

    @router.post("/api/credentials/passkey/export")
    async def export_passkey():
        if not passkey_store.has_key():
            passkey_store.generate()
        raw = passkey_store.export_private_key()
        code = transfer_mgr.mint(raw)
        return {"transfer_code": code}

    @router.post("/api/credentials/passkey/import")
    async def import_passkey(transfer_code: str = Body(..., embed=True)):
        raw = transfer_mgr.redeem(transfer_code)
        if raw is None:
            raise HTTPException(status_code=403, detail="invalid or expired transfer code")
        passkey_store.import_private_key(raw)
        return {"imported": True, "public_key": passkey_store.public_key_b64()}

    return router
```

**Implementer note:** this is the SAME `transfer_mgr` INSTANCE shared
between the two `_app()` factories in the test above (that's how the
in-process test simulates "two devices" — a real cross-device transfer
needs the transfer CODE itself carried physically by the QR scan, not a
shared in-memory manager; in the real wired app, each instance has its OWN
`PasskeyTransferManager`, and `import_passkey` needs to know WHICH
instance's manager to redeem against. **Revisit this before wiring into
app.py (Task 5):** the export side's `transfer_code` must be redeemable
FROM THE SAME instance that minted it — so the QR must ALSO encode which
instance to redeem against (e.g. `{base_url}#{transfer_code}`), and `POST
/api/credentials/passkey/import` on the SCANNING instance must itself call
OUT to the source instance's `/api/credentials/passkey/export/redeem`
endpoint (a new one, not yet drafted here) rather than redeeming locally.
Add this missing redeem-remotely step as part of Task 5's app.py wiring,
using `peer_client.post_json` the same way Phase 1's `/api/peers/confirm`
calls out to a peer — but note this transfer explicitly does NOT require
the two instances to already be paired peers first (a brand-new second
instance transferring its very first credential may not be paired yet) —
so this call is unauthenticated-in-subnet-bounded, like `/api/peers/
exchange`, not using a peer token.

- [ ] **Step 5: Fix the design per the implementer note, re-run tests**

Adjust `export_passkey` to return `{"transfer_code": code, "base_url": self_base_url}`
(threading `self_base_url` into `build_credentials_router` the same way
Phase 1's `build_peers_router` takes one), change `import_passkey`'s body
to `{"base_url": str, "transfer_code": str}`, and have it call
`peer_client.get_json(base_url, f"/api/credentials/passkey/export/redeem/
{transfer_code}")` (a new tiny GET-and-consume endpoint, unauthenticated-
in-subnet, that pops the code from the SOURCE instance's own manager and
returns the raw key) rather than redeeming against a shared local
manager. Update `test_export_then_import_passkey_via_api` to route through
this real cross-instance shape (reuse the SAME `routed_transport` /
`monkeypatch` pattern Phase 1's `test_full_bidirectional_pairing_two_
instances` used) instead of sharing one `PasskeyTransferManager` instance
directly.

Run: `cd backend && python -m pytest tests/test_passkey_transfer.py tests/test_api_credentials.py -v`
Expected: PASS after the fix

- [ ] **Step 6: Commit**

```bash
git add backend/wavr/passkey_transfer.py backend/wavr/api_credentials.py backend/tests/test_passkey_transfer.py backend/tests/test_api_credentials.py
git commit -m "feat(credentials): local-passkey QR transfer between instances

Short-TTL, single-use, unauthenticated-in-subnet (does not require the two
instances to be paired peers first -- a brand-new second instance has
nothing to authenticate with yet). Corrected mid-task from a same-process
sharing bug to the real cross-instance shape."
```

---

## Task 4: Biometric — the narrow backend contract, and the native handoff

**Files:**
- Modify: `backend/wavr/api_credentials.py` (a tiny status endpoint only)
- Test: `backend/tests/test_api_credentials.py` (append)

Biometric unlock (design spec §4.1) is, by nature, entirely a NATIVE, per-platform concern — it unlocks the LOCAL device's already-stored portable credential (the passkey private key from Task 2, or a cached PIN), it never itself travels or gets verified server-side. This backend plan's only job is to make sure there IS something local worth gating behind biometric, which Tasks 2-3 already provide.

- [ ] **Step 1: Add a trivial "is there a local credential to gate" status endpoint**

```python
# append to backend/wavr/api_credentials.py's build_credentials_router
    @router.get("/api/credentials/status")
    async def credentials_status():
        return {
            "pin_set": pin_store.is_set() if pin_store else False,
            "pin_shared": pin_store.is_shared() if pin_store else False,
            "passkey_set": passkey_store.has_key() if passkey_store else False,
        }
```

- [ ] **Step 2: Test it**

```python
# append to backend/tests/test_api_credentials.py
def test_credentials_status_reflects_store_state(tmp_path):
    from wavr.pin_store import PinStore
    pin = PinStore(str(tmp_path / "pin.db"))
    pin.set_pin("1234", shared=True)
    passkey = PasskeyStore(str(tmp_path / "pk.db"))
    passkey.generate()
    app = FastAPI()
    app.include_router(build_credentials_router(
        pin_store=pin, peer_store=None, passkey_store=passkey, transfer_mgr=PasskeyTransferManager()))
    r = TestClient(app).get("/api/credentials/status")
    assert r.json() == {"pin_set": True, "pin_shared": True, "passkey_set": True}
```

Run: `cd backend && python -m pytest tests/test_api_credentials.py -v -k credentials_status`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/wavr/api_credentials.py backend/tests/test_api_credentials.py
git commit -m "feat(credentials): GET /api/credentials/status -- what a native biometric gate has to unlock"
```

- [ ] **Step 4: Hand off the native biometric UI work explicitly**

This backend plan stops here for biometric. The remaining work — actually
wiring Windows Hello/Touch ID (Desktop, via whatever Tauri plugin is
current when this is picked up) and confirming Core's existing
`BiometricPrompt` (Kotlin, `core-launcher`) reads/gates the SAME local
credential this plan introduces rather than its own separate one — belongs
to whoever owns each native shell (`tauri-desktop-shell-engineer` for
Desktop, the Core-launcher owner for Android) and should be scoped as its
own small follow-up plan once someone has actually checked the installed
Tauri version's plugin options. Do not write that native code speculatively
here.

---

## Task 5: Cloud WebAuthn (opt-in)

**Files:**
- Create: `backend/wavr/webauthn_store.py`
- Modify: `backend/wavr/api_credentials.py`
- Modify: `backend/wavr/config.py`, `backend/pyproject.toml`
- Test: `backend/tests/test_webauthn_credentials.py` (new)

**Interfaces:**
- Consumes: `webauthn` (PyPI `py_webauthn`, lazy-imported, new `[webauthn]` extra) — verify the installed version's exact function names/signatures before writing calls against it (`pip show webauthn` or check `https://github.com/duo-labs/py_webauthn`'s current README once the extra is actually installed in the dev venv; the shape below matches `py_webauthn` 2.x's well-known top-level functions as of this design, but DO NOT assume — confirm against the actually-installed version first, exactly like Task 4 (Phase 1)'s `getpeercert` note)
- Produces: `class WebAuthnStore` (SQLite: credential_id, public_key, sign_count), `POST /api/credentials/webauthn/register/options`, `POST /api/credentials/webauthn/register/verify`, `POST /api/credentials/webauthn/authenticate/options`, `POST /api/credentials/webauthn/authenticate/verify` — all gated behind `cfg.webauthn_enabled` (new, default OFF, separate from `WAVR_PEERS_ENABLED`)

- [ ] **Step 1: Add the config flag + extra**

`backend/wavr/config.py`: add `webauthn_enabled: bool`, env `WAVR_WEBAUTHN_ENABLED` (default off, same `.lower() in ("1","true","yes")` pattern as every other bool flag).

`backend/pyproject.toml`: add `webauthn = ["webauthn>=2.0"]` to `[project.optional-dependencies]`.

- [ ] **Step 2: Write the failing tests (store layer only — the ceremony endpoints need the real library's exact API confirmed first, see Step 4)**

```python
# backend/tests/test_webauthn_credentials.py
import pytest
from wavr.webauthn_store import WebAuthnStore


def test_add_and_get_credential(tmp_path):
    store = WebAuthnStore(str(tmp_path / "wa.db"))
    store.add(credential_id=b"cred-id-bytes", public_key=b"pub-key-bytes", sign_count=0)
    cred = store.get(b"cred-id-bytes")
    assert cred.public_key == b"pub-key-bytes"
    assert cred.sign_count == 0


def test_get_unknown_credential_returns_none(tmp_path):
    store = WebAuthnStore(str(tmp_path / "wa.db"))
    assert store.get(b"nope") is None


def test_update_sign_count(tmp_path):
    store = WebAuthnStore(str(tmp_path / "wa.db"))
    store.add(b"cred", b"pub", 0)
    store.update_sign_count(b"cred", 5)
    assert store.get(b"cred").sign_count == 5


def test_has_credential(tmp_path):
    store = WebAuthnStore(str(tmp_path / "wa.db"))
    assert store.has_any() is False
    store.add(b"cred", b"pub", 0)
    assert store.has_any() is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_webauthn_credentials.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.webauthn_store'`

- [ ] **Step 4: Implement `webauthn_store.py`**

```python
# backend/wavr/webauthn_store.py
"""Cloud WebAuthn credential storage (2026-07-09 design spec §4.1/§4.3,
opt-in). Only the PUBLIC key + credential id + signature counter are ever
stored -- the private key lives in the platform authenticator (iCloud
Keychain / Google Password Manager) and NEVER touches Wavr, which is the
entire point of WebAuthn (contrast with passkey_store.py's local-passkey
path, where Wavr DOES hold the private key because there is no platform
RP-ID-bound authenticator to defer to on a LAN appliance).

Single-row-per-credential (a real deployment might register one credential
per admin device that opts in) -- unlike pin_store.py/passkey_store.py's
single-row-id=1 pattern, this table can hold multiple rows."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webauthn_credentials (
    credential_id BLOB PRIMARY KEY,
    public_key    BLOB NOT NULL,
    sign_count    INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class WebAuthnCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int


class WebAuthnStore:
    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, credential_id: bytes, public_key: bytes, sign_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO webauthn_credentials (credential_id, public_key, sign_count)"
                " VALUES (?, ?, ?)", (credential_id, public_key, sign_count))
            self._conn.commit()

    def get(self, credential_id: bytes) -> WebAuthnCredential | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT credential_id, public_key, sign_count FROM webauthn_credentials"
                " WHERE credential_id = ?", (credential_id,)).fetchone()
        if row is None:
            return None
        return WebAuthnCredential(row["credential_id"], row["public_key"], row["sign_count"])

    def update_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE webauthn_credentials SET sign_count = ? WHERE credential_id = ?",
                (sign_count, credential_id))
            self._conn.commit()

    def has_any(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM webauthn_credentials LIMIT 1").fetchone()
        return row is not None

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 5: Run store tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_webauthn_credentials.py -v`
Expected: PASS (4/4)

- [ ] **Step 6: Install `[webauthn]` and confirm the real library's API before writing the ceremony endpoints**

```bash
cd backend && pip install -e ".[webauthn]"
python -c "import webauthn; print(webauthn.__version__); print([n for n in dir(webauthn) if not n.startswith('_')])"
```

Read the printed function list. The ceremony endpoints (registration
options/verify, authentication options/verify) MUST be written against
whatever this actually prints — do not proceed from memory of the
library's shape. As of this design, `py_webauthn` 2.x exposes roughly
`generate_registration_options`, `verify_registration_response`,
`generate_authentication_options`, `verify_authentication_response`, and
typed option/`credential` dataclasses — confirm the exact names/kwargs
now and write the four endpoints in `api_credentials.py` following THIS
library's actual signatures, with the same lazy-import-inside-the-function
discipline every other optional-extra module in this codebase uses
(`camera.py`/`ha_client.py`/`tls.py`).

- [ ] **Step 7: Implement the four ceremony endpoints**

Once Step 6's real API is confirmed, implement (rp_id = this instance's
own hostname/IP — note WebAuthn's RP-ID rules genuinely don't allow a bare
IP for most authenticators; this may mean the cloud-WebAuthn path requires
the instance to have a real resolvable hostname, which most LAN Wavr
instances won't — **document this constraint plainly in the endpoint's
docstring and the frontend's opt-in warning copy** rather than silently
shipping a path that fails on most installs; if `py_webauthn`/browsers
reject the setup entirely for IP-only hosts, note that finding back in this
plan's ledger and treat cloud WebAuthn as "designed for, blocked on a real
resolvable name" rather than pretending it fully works):

- `POST /api/credentials/webauthn/register/options` → `generate_registration_options(...)`, store the challenge server-side (short TTL, mirror `PasskeyTransferManager`'s shape) for the verify step to check against
- `POST /api/credentials/webauthn/register/verify` → `verify_registration_response(...)`, on success `WebAuthnStore.add(...)`
- `POST /api/credentials/webauthn/authenticate/options` → `generate_authentication_options(...)`
- `POST /api/credentials/webauthn/authenticate/verify` → `verify_authentication_response(...)`, on success `WebAuthnStore.update_sign_count(...)`

Write real tests against these using `py_webauthn`'s own test vectors/
fixtures if the library ships any (check its test suite on the installed
version for reusable canned request/response pairs — reusing a vetted
library's own fixtures beats hand-crafting WebAuthn CBOR/attestation blobs
from scratch, which is exactly the kind of hand-rolled-crypto-parsing this
plan chose a library specifically to avoid).

- [ ] **Step 8: Commit**

```bash
git add backend/wavr/webauthn_store.py backend/wavr/api_credentials.py backend/wavr/config.py backend/pyproject.toml backend/tests/test_webauthn_credentials.py
git commit -m "feat(credentials): opt-in cloud WebAuthn passkey (WAVR_WEBAUTHN_ENABLED, default off)

Public-key-only storage (private key stays in the platform authenticator,
never touches Wavr -- the actual point of WebAuthn). Ceremony endpoints
written against py_webauthn's confirmed installed API, not assumed. RP-ID's
real-hostname requirement documented as a known constraint for LAN-only
installs."
```

---

## Task 6: Frontend — credential method picker

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Settings panel: "Admin credential" section**

Three method cards (PIN / Biometric / Passkey), each showing set/unset
state and a local-vs-shared toggle where applicable (PIN, local passkey —
NOT biometric, which per Task 4 has no sharing concept; NOT cloud WebAuthn,
which is inherently portable via the platform, not a local/shared choice
Wavr controls). Passkey card gets two sub-options: "Transfer via QR"
(renders the `transfer_code` from `/api/credentials/passkey/export` as a
QR — reuse whatever QR-rendering approach the Mobile pairing design already
specified, check `docs/superpowers/specs/2026-07-07-wavr-mobile-core-attachment-design.md`
for the QR pattern already chosen there) and "Enable cloud sync" (behind
the explicit external-connection warning toggle, same copy pattern as the
narrator's Gemini opt-in — find and mirror that exact warning copy).

- [ ] **Step 2: Manual verification + Impeccable pass**

Mandatory `/polish` + `/audit`, per CLAUDE.md, before this is done.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): admin credential picker -- PIN/biometric/passkey, local vs shared"
```

---

## Task 7: Real hardware bring-up (manual)

- [ ] Set a shared PIN on Desktop, confirm it now also unlocks Core's panel.
- [ ] Generate a local passkey on Core, export via QR, scan/import on Desktop, confirm both instances' public keys match and either can sign a test challenge the other verifies.
- [ ] (If a resolvable hostname is available) enable cloud WebAuthn on one instance, complete a real registration+authentication ceremony with an actual platform authenticator (Touch ID / Windows Hello / a phone's passkey manager) — else document the RP-ID blocker found in Task 5 Step 7 as confirmed-in-practice.
- [ ] Document results in the SDD ledger.

---

## Plan Self-Review

**Spec coverage:** §4.1 (three credential methods + the local-only nature of biometric), §4.2 (per-credential local/shared choice), §4.3 (cloud exception with warning) all covered. §4.4 (remote config / bulk propagation) is intentionally NOT in this plan — that's Phase 4's whole subject; Task 1's `push_pin_to_peers` is a narrow, credential-specific instance of the SAME pattern Phase 4 generalizes, not a duplication of it.

**Placeholder scan:** Task 3's QR-transfer design was caught and corrected mid-task (Step 4→5) rather than shipped wrong — flagged explicitly rather than silently left as a subtle same-process-only bug. Task 5's WebAuthn library API is explicitly marked "confirm before writing" rather than guessed, and its RP-ID/bare-IP limitation is surfaced rather than hidden.

**Type consistency:** `PasskeyStore.export_private_key()`/`.import_private_key()` (Task 2) byte shape flows unchanged through `PasskeyTransferManager.mint/redeem` (Task 3) and the corrected cross-instance transfer endpoints. `PeerStore`/`peer_client` usage (Task 1) matches Phase 1's actual signatures with no drift.
