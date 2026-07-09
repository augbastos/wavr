# Wavr Remote Configuration & Bulk Propagation — Implementation Plan (Phase 4 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin can read/change settings on any peer-paired instance from wherever they're standing, and apply the same change to several peers at once from one screen. Per the design spec §4.4, this is explicitly **not a new distributed protocol** — it's Phase 1's portable `central`+`admin` identity applied to settings endpoints that already exist (PIN — Phase 3 already did this one specifically; cameras, house map, connectors, etc. — this plan generalizes the pattern instead of hand-writing one push function per settings type).

**Architecture:** One generic, allowlisted proxy (`POST /api/peers/{id}/proxy`) that forwards an admin's request to a specific peer using the credential `PeerStore` already holds (Phase 1), plus a bulk wrapper that fans the same call out to several peers and reports per-peer results — no rollback, no silent retry, exactly matching the design spec §5's failure-handling table and Phase 3's `push_pin_to_peers` precedent.

**Tech Stack:** Same as Phases 1-3 — no new dependency.

## Global Constraints

- **Allowlist, not a blind proxy.** `POST /api/peers/{id}/proxy` must only forward to a fixed, explicit set of local paths already known to be admin-facing settings endpoints (PIN, cameras, house map, connectors, device role changes) — never an arbitrary path, and never `/api/peers/*` itself (no proxying-a-proxy) or anything loopback-root-only (`/api/block`, etc. — those stay loopback-root-only even when the caller is a trusted peer, per the EXISTING `require_root` boundary that already refuses even a `central` peer today; this plan does not weaken that).
- **No silent retry, no rollback.** A bulk push reports exactly which peers succeeded and which didn't; the admin decides what to do about a failure. Matches design spec §5 and Phase 3 Task 1's `push_pin_to_peers`.
- **Default-OFF via Phase 1's `WAVR_PEERS_ENABLED`** — no new flag needed, this is a direct consequence of having peers at all.
- **No push.** Local commits only.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/wavr/peer_proxy.py` (new) | The allowlist + the single-peer forward + the bulk fan-out, as plain testable functions |
| `backend/wavr/api_peers.py` (modify) | `POST /api/peers/{id}/proxy`, `POST /api/peers/bulk-apply` |
| `frontend/index.html` (modify) | "Apply to peers" control on existing settings screens (cameras, house map, connectors, device roles) |
| `backend/tests/test_peer_proxy.py` (new) | Allowlist enforcement, single forward, bulk fan-out with mixed success/failure |

---

## Task 1: `peer_proxy.py` — allowlisted forward + bulk fan-out

**Files:**
- Create: `backend/wavr/peer_proxy.py`
- Test: `backend/tests/test_peer_proxy.py` (new)

**Interfaces:**
- Consumes: `wavr.peers.PeerStore` (Phase 1), `wavr.peer_client.post_json, get_json, PeerClientError` (Phase 1)
- Produces: `ALLOWED_PROXY_PATHS: frozenset[str]` (module constant), `def forward_to_peer(peer_store, peer_id: str, method: str, path: str, body: dict | None = None) -> dict` (raises `PeerProxyError` — new — on unknown peer, disallowed path, or a `PeerClientError`), `def bulk_apply(peer_store, peer_ids: list[str], method: str, path: str, body: dict | None = None) -> dict[str, dict]` (peer_id → `{"ok": True, "result": ...}` or `{"ok": False, "error": str}`), `class PeerProxyError(RuntimeError)`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_peer_proxy.py
import pytest
from wavr.peers import PeerStore
from wavr.peer_proxy import ALLOWED_PROXY_PATHS, PeerProxyError, bulk_apply, forward_to_peer


def _store(tmp_path):
    return PeerStore(str(tmp_path / "peers.db"))


def test_allowlist_excludes_dangerous_paths():
    assert "/api/block" not in ALLOWED_PROXY_PATHS
    assert "/api/peers" not in ALLOWED_PROXY_PATHS
    assert "/api/core/pin" in ALLOWED_PROXY_PATHS  # a real settings endpoint IS allowed


def test_forward_rejects_disallowed_path(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core", "https://core:8000", "FP", "dev1", "tok")
    with pytest.raises(PeerProxyError, match="not allowed"):
        forward_to_peer(store, peer_id, "POST", "/api/block", {})


def test_forward_rejects_unknown_peer(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(PeerProxyError, match="unknown peer"):
        forward_to_peer(store, "nope", "POST", "/api/core/pin", {"pin": "1234"})


def test_forward_calls_post_json_with_stored_credential(tmp_path, monkeypatch):
    store = _store(tmp_path)
    peer_id = store.add("Core", "https://core:8000", "FP", "dev1", "tok-abc")
    import wavr.peer_proxy as mod
    calls = []
    def fake_post_json(base_url, path, body, token=None, pinned_fingerprint=None, **k):
        calls.append((base_url, path, body, token, pinned_fingerprint))
        return {"set": True}
    monkeypatch.setattr(mod, "post_json", fake_post_json)
    result = forward_to_peer(store, peer_id, "POST", "/api/core/pin", {"pin": "1234"})
    assert result == {"set": True}
    assert calls[0] == ("https://core:8000", "/api/core/pin", {"pin": "1234"}, "tok-abc", "FP")


def test_forward_get_uses_get_json(tmp_path, monkeypatch):
    store = _store(tmp_path)
    peer_id = store.add("Core", "https://core:8000", "FP", "dev1", "tok")
    import wavr.peer_proxy as mod
    monkeypatch.setattr(mod, "get_json", lambda *a, **k: {"house": "map"})
    result = forward_to_peer(store, peer_id, "GET", "/api/house")
    assert result == {"house": "map"}


def test_bulk_apply_reports_per_peer_mixed_results(tmp_path, monkeypatch):
    store = _store(tmp_path)
    ok_id = store.add("Core", "https://core:8000", "FP1", "dev1", "tok1")
    down_id = store.add("Desktop2", "https://d2:8000", "FP2", "dev2", "tok2")
    import wavr.peer_proxy as mod
    def fake_post_json(base_url, *a, **k):
        if "d2" in base_url:
            raise mod.PeerClientError("unreachable")
        return {"set": True}
    monkeypatch.setattr(mod, "post_json", fake_post_json)
    result = bulk_apply(store, [ok_id, down_id], "POST", "/api/core/pin", {"pin": "1234"})
    assert result[ok_id] == {"ok": True, "result": {"set": True}}
    assert result[down_id]["ok"] is False
    assert "unreachable" in result[down_id]["error"]


def test_bulk_apply_skips_disallowed_path_for_every_peer(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core", "https://core:8000", "FP", "dev1", "tok")
    result = bulk_apply(store, [peer_id], "POST", "/api/block", {})
    assert result[peer_id]["ok"] is False
    assert "not allowed" in result[peer_id]["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peer_proxy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.peer_proxy'`

- [ ] **Step 3: Implement**

```python
# backend/wavr/peer_proxy.py
"""Remote configuration on a peer (2026-07-09 design spec §4.4, Phase 4):
NOT a new protocol -- Phase 1's portable central+admin identity, applied to
settings endpoints that already exist. This module is the one place the
allowlist lives, so every proxied call goes through the SAME gate
regardless of whether it's a single-peer read/write or a bulk fan-out.

The allowlist is deliberately a FIXED set of paths, not a pattern/regex --
a new settings endpoint added elsewhere in app.py does NOT automatically
become remotely-proxyable just by existing; it must be explicitly added
here, so "can an admin push this from another instance" is always a
reviewable, intentional decision, not an accident of URL shape."""
from __future__ import annotations

from wavr.peer_client import PeerClientError, get_json, post_json

# Every path here is an EXISTING app.py route already gated by
# require_local + require_scope("admin") or require_scope("control") on the
# TARGET instance -- proxying doesn't bypass that; the peer's own gates
# still run against the forwarded request's Bearer token (our stored
# credential for them, which IS a central-role token, so it passes those
# gates exactly as if the admin were local to that instance). Loopback-
# root-only routes (/api/block, the ARP-blocking primitive) are
# DELIBERATELY excluded -- see this plan's Global Constraints.
ALLOWED_PROXY_PATHS = frozenset({
    "/api/core/pin",
    "/api/house",
    "/api/house/room",
    "/api/cameras",
    "/api/connectors",
    "/api/devices",
})


class PeerProxyError(RuntimeError):
    """Unknown peer, disallowed path, or the underlying peer call failed."""


def forward_to_peer(peer_store, peer_id: str, method: str, path: str,
                     body: dict | None = None) -> dict:
    peer = peer_store.get(peer_id)
    if peer is None or peer.revoked:
        raise PeerProxyError(f"unknown peer: {peer_id}")
    base_path = path.split("/", 3)
    allowed = any(path == p or path.startswith(p + "/") for p in ALLOWED_PROXY_PATHS)
    if not allowed:
        raise PeerProxyError(f"path not allowed for peer proxy: {path}")
    token = peer_store.token_for(peer_id)
    if token is None:
        raise PeerProxyError(f"no valid credential for peer: {peer_id}")
    try:
        if method.upper() == "GET":
            return get_json(peer.base_url, path, token=token,
                             pinned_fingerprint=peer.cert_fingerprint)
        return post_json(peer.base_url, path, body or {}, token=token,
                          pinned_fingerprint=peer.cert_fingerprint)
    except PeerClientError as exc:
        raise PeerProxyError(str(exc)) from exc


def bulk_apply(peer_store, peer_ids: list, method: str, path: str,
               body: dict | None = None) -> dict:
    """Per-peer result, no rollback, no retry queue (design spec §5) -- the
    admin sees exactly what happened on each peer and decides what to do
    about a failure."""
    results = {}
    for peer_id in peer_ids:
        try:
            result = forward_to_peer(peer_store, peer_id, method, path, body)
            results[peer_id] = {"ok": True, "result": result}
        except PeerProxyError as exc:
            results[peer_id] = {"ok": False, "error": str(exc)}
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peer_proxy.py -v`
Expected: PASS (7/7)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/peer_proxy.py backend/tests/test_peer_proxy.py
git commit -m "feat(peers): peer_proxy -- allowlisted single/bulk remote config forwarding

Fixed allowlist (deliberately excludes loopback-root-only routes like
/api/block). No rollback, per-peer result reporting (design spec §5).
Not wired to any route yet."
```

---

## Task 2: Wire the proxy + bulk-apply routes

**Files:**
- Modify: `backend/wavr/api_peers.py`
- Test: `backend/tests/test_peers.py` (append)

**Interfaces:**
- Consumes: `wavr.peer_proxy.forward_to_peer, bulk_apply, PeerProxyError` (Task 1)
- Produces: `POST /api/peers/{id}/proxy {method, path, body}` and `POST /api/peers/bulk-apply {peer_ids, method, path, body}` on the admin peers router

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_peers.py
def test_proxy_route_forwards_and_returns_result(tmp_path, monkeypatch):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    device_id, _ = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "tok")
    import wavr.peer_proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "post_json", lambda *a, **k: {"set": True})
    client = TestClient(app)
    r = client.post(f"/api/peers/{peer_id}/proxy",
                     json={"method": "POST", "path": "/api/core/pin", "body": {"pin": "1234"}})
    assert r.status_code == 200
    assert r.json() == {"set": True}


def test_proxy_route_disallowed_path_returns_403(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    device_id, _ = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "tok")
    client = TestClient(app)
    r = client.post(f"/api/peers/{peer_id}/proxy",
                     json={"method": "POST", "path": "/api/block", "body": {}})
    assert r.status_code == 403


def test_bulk_apply_route_returns_per_peer_results(tmp_path, monkeypatch):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    d1, _ = devices.add("Core", "central")
    d2, _ = devices.add("Desktop2", "central")
    p1 = peers.add("Core", "https://core:8000", "FP1", d1, "tok1")
    p2 = peers.add("Desktop2", "https://d2:8000", "FP2", d2, "tok2")
    import wavr.peer_proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "post_json", lambda *a, **k: {"set": True})
    client = TestClient(app)
    r = client.post("/api/peers/bulk-apply", json={
        "peer_ids": [p1, p2], "method": "POST", "path": "/api/core/pin",
        "body": {"pin": "1234"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body[p1]["ok"] is True and body[p2]["ok"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k "proxy_route or bulk_apply_route"`
Expected: FAIL (routes don't exist — 404)

- [ ] **Step 3: Implement**

In `backend/wavr/api_peers.py`, add to the admin router:

```python
    from wavr.peer_proxy import PeerProxyError, bulk_apply, forward_to_peer

    @router.post("/api/peers/{peer_id}/proxy")
    async def proxy(peer_id: str, method: str = Body(...), path: str = Body(...),
                     body: dict = Body(None)):
        try:
            return forward_to_peer(peer_store, peer_id, method, path, body)
        except PeerProxyError as exc:
            detail = str(exc)
            status = 403 if "not allowed" in detail else \
                     404 if "unknown peer" in detail else 502
            raise HTTPException(status_code=status, detail=detail) from exc

    @router.post("/api/peers/bulk-apply")
    async def bulk_apply_route(peer_ids: list = Body(...), method: str = Body(...),
                                path: str = Body(...), body: dict = Body(None)):
        return bulk_apply(peer_store, peer_ids, method, path, body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peers.py -v`
Expected: PASS, full file

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/api_peers.py backend/tests/test_peers.py
git commit -m "feat(peers): POST /api/peers/{id}/proxy + /api/peers/bulk-apply

Wires peer_proxy.py to the admin peers router. 403 on a disallowed path,
404 on an unknown peer, per-peer result body for bulk-apply."
```

---

## Task 3: Frontend — "apply to peers" on existing settings screens

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Add a shared "apply to peers" control**

A small reusable component (checkbox list of currently-paired peers, "Apply
to selected" button) that any settings screen can drop in next to its
existing Save button — start with the camera-add form, house-map save, and
connector-toggle screens (the three concrete examples the design spec §4.4
names). Each Save action, when peers are selected, ALSO calls `POST /api/
peers/bulk-apply` with the same body it just sent locally, then shows the
per-peer result (name + ok/error) inline — never a single combined
"success" that hides a partial failure.

- [ ] **Step 2: Manual verification + Impeccable pass**

Mandatory `/polish` + `/audit`, per CLAUDE.md.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): apply-to-peers control on camera/house-map/connector settings"
```

---

## Task 4: Real hardware bring-up (manual)

- [ ] From Desktop, add a camera and apply it to the paired G9 Core via bulk-apply; confirm the camera now appears in Core's own camera list.
- [ ] Deliberately power off one peer mid-bulk-apply (if more than one is paired) and confirm the per-peer result correctly shows one success + one failure, with no partial/corrupted state on either side.
- [ ] Confirm `/api/peers/{id}/proxy` with `/api/block` as the path 403s even from an otherwise-trusted peer session (the loopback-root-only boundary holds).
- [ ] Document results in the SDD ledger.

---

## Plan Self-Review

**Spec coverage:** §4.4 (remote config + bulk propagation) and the relevant row of §5's failure-handling table (bulk push, one peer offline) are both fully covered by Tasks 1-2. This plan deliberately does NOT re-cover PIN sharing (Phase 3 Task 1 already did the credential-specific case) — it generalizes the SAME pattern to cameras/house-map/connectors/devices.

**Placeholder scan:** none found — every task ships complete, runnable code and tests.

**Type consistency:** `forward_to_peer`/`bulk_apply`'s signatures (Task 1) are called identically by the routes in Task 2, with the same `(peer_store, peer_id_or_ids, method, path, body)` argument order throughout. `PeerStore.get/token_for` (Phase 1) usage matches Phase 1's actual signatures.
