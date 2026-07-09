# Wavr Cross-Instance Sensor Fusion — Implementation Plan (Phase 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once two instances are peer-paired (Phase 1), each one's `FusionEngine` also sees the other's live sensor evidence, per-modality, tagged with its original type (camera stays `camera`, network stays `network`) — Desktop's camera and Core's network/BLE combine into one confidence per mapped room, on BOTH sides, with zero changes to `fusion.py`. This is also what makes an MCP client attached to either instance see "the whole house" for free (design spec §7) — no MCP code changes anywhere in this plan.

**Architecture:** A new `RemoteSource` (`backend/wavr/sources/remote.py`), built on the exact same shape as `RuViewSource`: an injectable-transport async generator yielding `SensingEvent`s, registered with `SourceManager` like any other source. It authenticates to the peer using the token `PeerStore` already holds (Phase 1), fetches a WS ticket, opens the peer's `/ws/live` over pinned WSS, and translates each incoming (fused) `RoomState` back into per-modality `SensingEvent`s using `RoomState.sources` (the `{modality, presence, confidence, age_s, health}` list `fusion.py` already produces) plus the peer's room-name mapping (`PeerStore.room_map`, already in the schema from Phase 1 Task 2). `SourceManager` registers/unregisters one `RemoteSource` per active peer automatically as peers are paired/unpaired.

**Tech Stack:** Same as Phase 1 — Python `websockets` (already a dependency via `uvicorn[standard]`, used identically by `RuViewSource`), stdlib `ssl` for WSS cert pinning.

## Global Constraints

- **`fusion.py` is NOT modified.** Every task in this plan either adds a new module or extends `SourceManager`'s call sites; `FusionEngine.update()` and `DEFAULT_WEIGHTS` stay byte-for-byte unchanged — a `RemoteSource`'s events are indistinguishable, at fusion time, from a same-box source of the same modality.
- **Default-OFF, additive.** `RemoteSource` registration is gated on `cfg.peers_enabled` (Phase 1's flag — no new flag needed; fusion is an automatic consequence of pairing, not a separately toggled feature, per the design spec's "no new protocol, applies the portable identity" framing) AND on each individual peer actually having a non-empty `room_map` (an unmapped peer contributes nothing — see Task 3).
- **Targets and identities are explicitly OUT of scope for this plan.** `RoomState.targets` (x/y in the SOURCE instance's own homography/coordinate frame) do not make sense merged into a different instance's frame without a much larger cross-instance calibration effort — not attempted here. `RoomState.identities` (who's home) crossing instance boundaries is a privacy decision Augusto has not made (every other identity feature in this codebase — BLE/network "who's home" — shipped behind its own explicit opt-in gate and a documented ethical review; cross-instance identity propagation deserves the same treatment, not a silent side-effect of pairing). `RemoteSource` MUST NOT propagate either field — emit `targets=()` and `identities=()` on every `SensingEvent` it produces, regardless of what the peer's `RoomState` contained.
- **No push.** Same rule as Phase 1 — local commits only.
- **`ssl.CERT_NONE` + manual fingerprint pin in `peer_ws_client.py` is intentional** — identical TOFU-then-pin model as Phase 1's `peer_client.py` (see that plan's Global Constraints for the full rationale). A security pass on this plan's WSS connect code should verify the fingerprint check actually raises on mismatch, not add real CA validation (impossible for a self-signed LAN peer) or drop the check.
- **Room-name mismatch fails safe, not silent.** A peer's room with no entry in `room_map` contributes nothing (dropped) and surfaces exactly once as an admin-visible notice (design spec §5) — never silently fused into the wrong room, never silently dropped forever without any trace.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/wavr/sources/remote.py` (new) | `RemoteSource` — WS client to a peer's `/ws/live`, `RoomState` → `SensingEvent[]` translation |
| `backend/wavr/peer_ws_client.py` (new) | The WS-ticket-fetch + pinned-WSS-connect transport `RemoteSource` uses (mirrors `peer_client.py`'s injectable shape, but for a persistent WS instead of one-shot POST/GET) |
| `backend/wavr/app.py` (modify) | Register/unregister a `RemoteSource` per peer automatically on pair/unpair; surface unmapped-room warnings |
| `backend/wavr/api_peers.py` (modify) | Add `PUT /api/peers/{id}/room-map` (admin sets the mapping) and include a `warnings` field in `GET /api/peers` |
| `frontend/index.html` (modify) | Room-mapping UI in the Peers panel (per-peer: pick "their room X" → "my room Y" for each of their rooms) |
| `backend/tests/test_remote_source.py` (new) | `RemoteSource` translation logic, mock-tested, no network |
| `backend/tests/test_peer_ws_client.py` (new) | Injectable-transport tests for the ticket-fetch + connect sequence |

---

## Task 1: `peer_ws_client.py` — ticket fetch + pinned WSS connect

**Files:**
- Create: `backend/wavr/peer_ws_client.py`
- Test: `backend/tests/test_peer_ws_client.py` (new)

**Interfaces:**
- Consumes: `wavr.peer_client.post_json, PeerClientError` (Phase 1 Task 4)
- Produces: `async def connect_peer_live(base_url: str, token: str, pinned_fingerprint: str, ticket_fetcher=None, ws_connect=None) -> AsyncIterator[dict]` — an async generator yielding decoded JSON frames from the peer's `/ws/live`, fetching a fresh ticket via `ticket_fetcher` (default: `post_json(base_url, "/api/ws-ticket", {}, token=token, pinned_fingerprint=pinned_fingerprint)`) before every connection attempt (a ticket is single-use — see `pairing.py`'s `TICKET_TTL_SECONDS`/`redeem_ticket`), then opening `wss://.../ws/live?ticket=...` via `ws_connect` (default real `websockets.connect` with a pinned `ssl.SSLContext`)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_peer_ws_client.py
import pytest
from wavr.peer_ws_client import connect_peer_live


async def _collect(agen, n):
    out = []
    try:
        async for item in agen:
            out.append(item)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out


async def test_fetches_ticket_then_connects_with_it():
    calls = []

    def fake_ticket_fetcher(base_url, token, pinned_fingerprint):
        calls.append(("ticket", base_url, token, pinned_fingerprint))
        return "tick-abc"

    async def fake_ws_connect(url, pinned_fingerprint):
        calls.append(("connect", url, pinned_fingerprint))
        async def gen():
            yield {"room": "sala", "occupied": True}
        return gen()

    agen = connect_peer_live("https://core:8000", "tok", "FP",
                              ticket_fetcher=fake_ticket_fetcher, ws_connect=fake_ws_connect)
    frames = await _collect(agen, 1)
    assert frames == [{"room": "sala", "occupied": True}]
    assert calls[0] == ("ticket", "https://core:8000", "tok", "FP")
    assert calls[1][1] == "wss://core:8000/ws/live?ticket=tick-abc"


async def test_reconnects_forever_on_drop():
    attempt = {"n": 0}

    def fake_ticket_fetcher(base_url, token, pinned_fingerprint):
        return f"tick-{attempt['n']}"

    async def fake_ws_connect(url, pinned_fingerprint):
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise ConnectionError("dropped")
        async def gen():
            yield {"room": "sala", "occupied": False}
        return gen()

    agen = connect_peer_live("https://core:8000", "tok", "FP",
                              ticket_fetcher=fake_ticket_fetcher, ws_connect=fake_ws_connect,
                              reconnect_delay=0)
    frames = await _collect(agen, 1)
    assert frames == [{"room": "sala", "occupied": False}]
    assert attempt["n"] == 2  # first attempt failed, second succeeded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peer_ws_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.peer_ws_client'`

- [ ] **Step 3: Implement**

```python
# backend/wavr/peer_ws_client.py
"""Connect to a peer instance's /ws/live as an authenticated companion (Phase
2: RemoteSource uses this to receive their live RoomState stream). Same
reconnect-forever discipline as RuViewSource -- a peer rebooting or dropping
off Wi-Fi must never crash this instance's SourceManager, just go stale
(fusion.py's existing freshness decay handles the rest)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator, Callable

from wavr.peer_client import PeerClientError, post_json

DEFAULT_RECONNECT_DELAY = 3.0


def _default_ticket_fetcher(base_url: str, token: str, pinned_fingerprint: str) -> str:
    """POST /api/ws-ticket on the peer using OUR credential for them (from
    PeerStore.token_for), returning the single-use ticket. Raises
    PeerClientError on failure -- the caller (connect_peer_live) treats that
    as a connection-level error and reconnects after the usual delay."""
    result = post_json(base_url, "/api/ws-ticket", {}, token=token,
                        pinned_fingerprint=pinned_fingerprint)
    return result["ticket"]


async def _default_ws_connect(url: str, pinned_fingerprint: str) -> AsyncIterator[dict]:
    """Real WS client: pinned WSS connect (self-signed peer, same TOFU-then-
    pin model as peer_client.py -- CERT_NONE + manual fingerprint verify
    against the ALREADY-PAIRED, admin-confirmed fingerprint, never blind)."""
    import ssl
    import hashlib
    import websockets

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(url, ssl=ctx) as ws:
        der = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        observed = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        if observed != pinned_fingerprint:
            raise PeerClientError(
                f"peer WS certificate fingerprint mismatch: expected "
                f"{pinned_fingerprint}, got {observed} -- possible MitM")
        async for raw in ws:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue


async def connect_peer_live(base_url: str, token: str, pinned_fingerprint: str,
                             ticket_fetcher=None, ws_connect=None,
                             reconnect_delay: float = DEFAULT_RECONNECT_DELAY
                             ) -> AsyncIterator[dict]:
    """Yield decoded RoomState frames from a peer's /ws/live forever, fetching
    a fresh single-use ticket before EVERY connection attempt (a ticket
    can't be reused across reconnects -- pairing.py's TICKET_TTL_SECONDS is
    short by design). `ticket_fetcher(base_url, token, pinned_fingerprint) ->
    str` and `ws_connect(url, pinned_fingerprint) -> AsyncIterator[dict]` are
    both injectable for tests."""
    fetch_ticket = ticket_fetcher or _default_ticket_fetcher
    connect = ws_connect or _default_ws_connect
    while True:
        try:
            ticket = fetch_ticket(base_url, token, pinned_fingerprint)
            ws_url = base_url.replace("https://", "wss://").rstrip("/") + \
                f"/ws/live?ticket={ticket}"
            stream = connect(ws_url, pinned_fingerprint)
            async with contextlib.aclosing(stream):
                async for frame in stream:
                    yield frame
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("peer_ws_client: connection error; reconnecting", exc_info=True)
        if reconnect_delay:
            await asyncio.sleep(reconnect_delay)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peer_ws_client.py -v`
Expected: PASS (2/2)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/peer_ws_client.py backend/tests/test_peer_ws_client.py
git commit -m "feat(peers): peer_ws_client -- ticket-fetch + pinned-WSS connect to a peer's /ws/live

Reconnect-forever (RuViewSource pattern). Fresh ticket per attempt (tickets
are single-use/short-TTL). Fingerprint re-verified on every connect, not
just at pairing time."
```

---

## Task 2: `sources/remote.py` — `RemoteSource`

**Files:**
- Create: `backend/wavr/sources/remote.py`
- Test: `backend/tests/test_remote_source.py` (new)

**Interfaces:**
- Consumes: `wavr.peer_ws_client.connect_peer_live` (Task 1), `wavr.events.SensingEvent` (existing)
- Produces: `class RemoteSource` — `__init__(self, base_url, token, pinned_fingerprint, room_map: dict[str, str], connect=None)`, `.events() -> AsyncIterator[SensingEvent]` (matches every other source's duck-typed interface `SourceManager` expects)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_remote_source.py
import pytest
from wavr.sources.remote import RemoteSource

ROOMSTATE_FRAME = {
    "room": "sala", "occupied": True, "confidence": 0.72,
    "vitals": {"breathing_bpm": 14.2, "heart_bpm": 68},
    "sources": [
        {"modality": "camera", "presence": True, "confidence": 0.9, "age_s": 1, "health": "live"},
        {"modality": "network", "presence": True, "confidence": 0.5, "age_s": 3, "health": "live"},
    ],
    "targets": [{"id": 1, "x": 1.2, "y": 0.4}],
    "identities": [{"person": "Augusto", "source": "ble", "rssi": -60}],
    "explanation": "...", "ts": "2026-07-09T12:00:00+00:00",
}


async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out


async def test_emits_one_event_per_source_modality():
    async def connect(base_url, token, pinned_fingerprint):
        yield ROOMSTATE_FRAME
    src = RemoteSource("https://core:8000", "tok", "FP",
                        room_map={"sala": "living_room"}, connect=connect)
    evs = await _first_n(src, 2)
    assert {e.modality for e in evs} == {"camera", "network"}
    assert all(e.room == "living_room" for e in evs)
    cam = next(e for e in evs if e.modality == "camera")
    assert cam.presence is True and cam.confidence == 0.9


async def test_never_propagates_targets_or_identities():
    async def connect(base_url, token, pinned_fingerprint):
        yield ROOMSTATE_FRAME
    src = RemoteSource("https://core:8000", "tok", "FP",
                        room_map={"sala": "living_room"}, connect=connect)
    evs = await _first_n(src, 1)
    assert evs[0].targets == ()
    assert evs[0].identities == ()


async def test_unmapped_room_is_dropped_not_crashed():
    async def connect(base_url, token, pinned_fingerprint):
        yield ROOMSTATE_FRAME  # room "sala", no mapping given below
        yield {**ROOMSTATE_FRAME, "room": "sala"}  # a second frame, still dropped
    src = RemoteSource("https://core:8000", "tok", "FP", room_map={}, connect=connect)
    agen = src.events()
    got_any = False
    try:
        async with __import__("asyncio").timeout(0.2):
            async for ev in agen:
                got_any = True
                break
    except TimeoutError:
        pass
    finally:
        await agen.aclose()
    assert got_any is False


async def test_vitals_only_attached_to_the_single_presence_source():
    frame = {**ROOMSTATE_FRAME, "sources": [
        {"modality": "wifi_csi", "presence": True, "confidence": 0.8, "age_s": 1, "health": "live"},
    ]}
    async def connect(base_url, token, pinned_fingerprint):
        yield frame
    src = RemoteSource("https://core:8000", "tok", "FP",
                        room_map={"sala": "quarto"}, connect=connect)
    [ev] = await _first_n(src, 1)
    assert ev.breathing_bpm == 14.2 and ev.heart_bpm == 68


async def test_vitals_dropped_when_multiple_presence_sources_ambiguous():
    async def connect(base_url, token, pinned_fingerprint):
        yield ROOMSTATE_FRAME  # two presence:true sources -> ambiguous attribution
    src = RemoteSource("https://core:8000", "tok", "FP",
                        room_map={"sala": "quarto"}, connect=connect)
    evs = await _first_n(src, 2)
    assert all(e.breathing_bpm is None and e.heart_bpm is None for e in evs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_remote_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wavr.sources.remote'`

- [ ] **Step 3: Implement**

```python
# backend/wavr/sources/remote.py
"""Cross-instance fusion (2026-07-09 design spec §3, Phase 2): re-emit a
peer's live RoomState as LOCAL SensingEvents, one per modality, so this
instance's OWN FusionEngine folds them in exactly like a same-box source.
fusion.py is never modified or even imported here -- this module only
produces the same SensingEvent shape every other source already produces.

Deliberately coarser than a same-box source: a RoomState's `sources` list
(fusion.py's per-modality summary) carries presence/confidence/age/health
per modality, but NOT each modality's own raw motion/vitals -- those were
already collapsed into RoomState.vitals (a SINGLE winning modality's vitals,
picked by fusion.py's own precedence, not per-modality). This module can
only re-attach vitals to a modality when there is EXACTLY ONE presence=true
source in the frame (unambiguous the vitals came from it); with 2+
presence=true sources, attribution is genuinely unknown, so vitals are
dropped rather than guessed -- an honest gap, not a silent wrong answer.

Targets and identities are NEVER propagated (design spec §3 scope note,
this plan's Global Constraints) -- every emitted SensingEvent carries
targets=() and identities=() regardless of what the peer's RoomState held."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from wavr.events import SensingEvent
from wavr.peer_ws_client import connect_peer_live


class RemoteSource:
    """Duck-types the same `.events() -> AsyncIterator[SensingEvent]`
    interface as every other source (RuViewSource, NetworkSource, ...) --
    SourceManager doesn't know or care this one's evidence originates on a
    different physical box."""

    def __init__(self, base_url: str, token: str, pinned_fingerprint: str,
                 room_map: dict, connect=None):
        self._base_url = base_url
        self._token = token
        self._fp = pinned_fingerprint
        self._room_map = room_map
        self._connect = connect or connect_peer_live
        self._warned_rooms: set[str] = set()

    async def events(self) -> AsyncIterator[SensingEvent]:
        async for frame in self._connect(self._base_url, self._token, self._fp):
            if not isinstance(frame, dict) or "room" not in frame:
                continue
            their_room = frame["room"]
            our_room = self._room_map.get(their_room)
            if our_room is None:
                if their_room not in self._warned_rooms:
                    self._warned_rooms.add(their_room)
                    logging.warning(
                        "RemoteSource: peer room %r has no local mapping -- "
                        "dropping its evidence until mapped (design spec §5)",
                        their_room)
                continue
            sources = frame.get("sources") or []
            presence_count = sum(1 for s in sources if s.get("presence"))
            vitals = frame.get("vitals") or {}
            for s in sources:
                modality = s.get("modality")
                if not modality:
                    continue
                attach_vitals = presence_count == 1 and s.get("presence")
                try:
                    yield SensingEvent(
                        room=our_room, modality=modality,
                        presence=bool(s.get("presence", False)),
                        motion=0.0,  # not preserved in RoomState.sources; informational only
                        breathing_bpm=vitals.get("breathing_bpm") if attach_vitals else None,
                        heart_bpm=vitals.get("heart_bpm") if attach_vitals else None,
                        confidence=float(s.get("confidence", 0.0)),
                        ts=frame.get("ts") or _now_iso(),
                        targets=(), identities=(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.warning("RemoteSource: bad source entry; skipping",
                                     exc_info=True)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_remote_source.py -v`
Expected: PASS (5/5)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/sources/remote.py backend/tests/test_remote_source.py
git commit -m "feat(sources): RemoteSource -- peer RoomState -> local per-modality SensingEvents

fusion.py untouched. Targets/identities never cross the instance boundary
(scoped out, see plan). Vitals attached only when unambiguous (exactly one
presence:true source in the frame)."
```

---

## Task 3: Room-mapping API

**Files:**
- Modify: `backend/wavr/api_peers.py`
- Test: `backend/tests/test_peers.py` (append)

**Interfaces:**
- Consumes: `wavr.peers.PeerStore.set_room_map` (Phase 1 Task 2, already exists — this task only adds the HTTP surface)
- Produces: `PUT /api/peers/{id}/room-map {room_map: {their_room: our_room}}` on the admin peers router; `GET /api/peers` response gains a `warnings: list[str]` field per peer (empty unless `RemoteSource` has logged an unmapped-room warning for it — see Task 4 for how the warning reaches the store)

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_peers.py
def test_set_room_map_via_api(tmp_path):
    app, devices, peers, pairing, exchange = _app(tmp_path)
    device_id, _ = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "tok")
    client = TestClient(app)
    r = client.put(f"/api/peers/{peer_id}/room-map",
                    json={"room_map": {"sala": "living_room"}})
    assert r.status_code == 200
    assert peers.get(peer_id).room_map == {"sala": "living_room"}


def test_set_room_map_unknown_peer_404s(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.put("/api/peers/nope/room-map", json={"room_map": {}})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_peers.py -v -k room_map_via_api`
Expected: FAIL with 404 (route doesn't exist) or 405

- [ ] **Step 3: Implement**

In `backend/wavr/api_peers.py`, add to the admin router (same router `GET /api/peers`/`DELETE /api/peers/{id}` are on):

```python
    @router.put("/api/peers/{peer_id}/room-map")
    async def set_room_map(peer_id: str, room_map: dict = Body(..., embed=True)):
        if peer_store.get(peer_id) is None:
            raise HTTPException(status_code=404, detail="unknown peer")
        peer_store.set_room_map(peer_id, room_map)
        return {"ok": True}
```

And extend the existing `list_peers`/`Peer.to_dict()` response to include
`warnings` — add a `warnings: dict[str, list[str]]` in-memory dict as a new
parameter to `build_peers_router` (or a tiny injected `warnings_store`
object with `.for_peer(peer_id) -> list[str]`), defaulting to empty; Task 4
wires `RemoteSource`'s unmapped-room log line into this same store instead
of only logging, so the admin sees it in the UI, not just in server logs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_peers.py -v`
Expected: PASS (full file, no regression)

- [ ] **Step 5: Commit**

```bash
git add backend/wavr/api_peers.py backend/tests/test_peers.py
git commit -m "feat(peers): PUT /api/peers/{id}/room-map -- admin sets the fusion room mapping"
```

---

## Task 4: Auto-register/unregister `RemoteSource` per peer

**Files:**
- Modify: `backend/wavr/app.py`
- Test: `backend/tests/test_peers.py` or a new `backend/tests/test_remote_source_wiring.py` (integration-level, exercises `SourceManager` + the peer lifecycle together)

**Interfaces:**
- Consumes: `wavr.sources.remote.RemoteSource` (Task 2), `wavr.sourcemanager.SourceManager.register/unregister` (existing)
- Produces: on `POST /api/peers/confirm` success and on app startup (for already-paired peers loaded from `PeerStore`), a `RemoteSource` is registered under the name `f"peer:{peer_id}"`; on `DELETE /api/peers/{id}`, it's unregistered

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_remote_source_wiring.py
"""Proves pairing a peer auto-registers a RemoteSource with SourceManager,
and unpairing auto-unregisters it -- without spinning up a real peer
connection (RemoteSource itself is unit-tested in test_remote_source.py;
this only checks the WIRING calls SourceManager correctly)."""
import pytest
from unittest.mock import AsyncMock

from wavr.sourcemanager import SourceManager
from wavr.peers import PeerStore


async def test_pairing_registers_and_unpairing_unregisters_remote_source(tmp_path):
    manager = SourceManager(AsyncMock())
    peers = PeerStore(str(tmp_path / "peers.db"))
    # This test's exact shape depends on how app.py's confirm/finish/unpair
    # handlers are refactored to call a shared `_sync_remote_sources(peers,
    # manager)` helper (write that helper as part of Step 2 below) -- call
    # it directly here rather than spinning up the full FastAPI app, so this
    # stays a fast unit test of the sync logic itself.
    from wavr.app import _sync_remote_sources  # the helper Step 2 introduces
    device_id = "dev1"
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "tok")
    _sync_remote_sources(peers, manager)
    assert f"peer:{peer_id}" in manager.status()["sources"] or \
        any(s["name"] == f"peer:{peer_id}" for s in manager.status()["sources"])
    peers.revoke(peer_id)
    _sync_remote_sources(peers, manager)
    assert not any(s["name"] == f"peer:{peer_id}" for s in manager.status()["sources"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_remote_source_wiring.py -v`
Expected: FAIL with `ImportError: cannot import name '_sync_remote_sources'`

- [ ] **Step 3: Implement the sync helper + call sites**

In `backend/wavr/app.py`, add a module-level (or `create_app`-local, matching how `_default_sources` is scoped — check whether it's module-level or nested) helper:

```python
def _sync_remote_sources(peer_store, source_manager) -> None:
    """Registers a RemoteSource for every non-revoked, room-mapped peer that
    doesn't already have one; unregisters any registered RemoteSource whose
    peer is gone/revoked. Called after every pair/unpair AND once at startup
    (for peers that were already paired before this boot) -- idempotent, so
    calling it redundantly is always safe."""
    import asyncio
    from wavr.sources.remote import RemoteSource

    live_peer_ids = set()
    for peer in peer_store.list():
        if peer.revoked or not peer.room_map:
            continue  # unmapped peer contributes nothing (Global Constraints)
        live_peer_ids.add(peer.peer_id)
        name = f"peer:{peer.peer_id}"
        if name not in source_manager._factories:  # see implementer note below
            token = peer_store.token_for(peer.peer_id)
            if token is None:
                continue
            source_manager.register(
                name,
                lambda p=peer, t=token: RemoteSource(
                    p.base_url, t, p.cert_fingerprint, p.room_map),
                enabled=True)
    for existing_name in list(source_manager._factories):
        if existing_name.startswith("peer:") and existing_name[5:] not in live_peer_ids:
            asyncio.create_task(source_manager.unregister(existing_name))
```

**Implementer note:** `source_manager._factories` is a private attribute —
check whether `SourceManager` already exposes a public way to check "is a
source registered" (its `.status()` method returns names; prefer building
a small public helper there, e.g. `SourceManager.is_registered(name) ->
bool`, over reaching into `_factories` directly from `app.py`) and add it
to `sourcemanager.py` as a one-line addition if it doesn't exist, rather
than breaking the module's existing encapsulation. Update this task's code
to use that public method once added.

Call `_sync_remote_sources(_peer_store, manager)` (using whatever the
actual local variable names are in `create_app` — check `_peers`/`_peer_
store` and the `SourceManager` instance's variable name, likely `manager`
per `manager = SourceManager(_ingest)` at line 551) at the end of the
`/api/peers/confirm` and `/api/peers/finish` handlers (`api_peers.py` — this
means `build_peers_router` needs the `source_manager` instance passed in as
an additional constructor parameter, alongside `peer_store`/`exchange_mgr`/
etc.) and the `DELETE /api/peers/{id}` handler, AND once during `create_app`
startup right after `_peer_store`/`manager` both exist (so peers paired in a
previous run resume fusion automatically on restart, matching how cameras
already resume from `CameraStore` on boot).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_remote_source_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd backend && python -m pytest tests/ -v`
Expected: PASS, no regression

- [ ] **Step 6: Commit**

```bash
git add backend/wavr/app.py backend/wavr/sourcemanager.py backend/wavr/api_peers.py backend/tests/test_remote_source_wiring.py
git commit -m "feat(app): auto-register/unregister RemoteSource per peer

Pairing (with a room map already set) or app startup (for existing peers)
brings the fusion feed up automatically; unpairing tears it down. Unmapped
peers contribute nothing until an admin sets their room map."
```

---

## Task 5: Frontend — room-mapping UI

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Extend the Peers panel (Phase 1 Task 8) with a room-mapping step**

After a peer is paired, if `peer.room_map` is empty, show a prompt: "Map Core's rooms to yours" — fetch the peer's own room list (`GET {peer.base_url}/api/house` — already an existing endpoint, per `project_wavr.md`'s history, unauthenticated read per today's `require_scope("presence:read")` which `central` already has by default) alongside this instance's own `GET /api/house`, render two lists, let the admin draw simple name→name pairs, `PUT /api/peers/{id}/room-map` on save. Surface `peer.warnings` (Task 3) as a small badge/banner on the peer's row if non-empty.

- [ ] **Step 2: Manual verification + Impeccable pass**

Same mandatory `/polish` + `/audit` pass as every other frontend change in this repo (CLAUDE.md), plus a Playwright check that an unpaired/unmapped peer never shows stale room-mapping UI from a previous peer (state reset between peers).

- [ ] **Step 3: Commit**

```bash
git add frontend/index.html
git commit -m "feat(frontend): room-mapping UI for peer fusion + warning badges"
```

---

## Task 6: Real hardware bring-up (manual)

- [ ] Pair Desktop + G9 Core (Phase 1's pairing, already proven live this session).
- [ ] Set the room map (Core's `quarto-1` → Desktop's equivalent room, or whatever the two house maps actually share).
- [ ] Confirm Desktop's dashboard shows the room's confidence rise when Core's network/BLE detects presence, and vice versa if Desktop has a camera on that mapped room.
- [ ] Confirm MCP (stdio or HTTP, either instance) reflects the fused state — the "unlocks for free" claim from the design spec §7 — without any MCP-side code change.
- [ ] Kill one instance's process; confirm the other's fused confidence for that room decays over `WAVR_SOURCE_STALE_S` rather than freezing or crashing.
- [ ] Document results in the SDD ledger, same convention as every prior sub-plan.

---

## Plan Self-Review

**Spec coverage:** §3 (fusion) and §7 (MCP-for-free) fully covered. Room-name reconciliation (§3) covered by Task 3/5. Peer-drop decay (§5) covered by Task 6's verification (relies entirely on `fusion.py`'s EXISTING freshness/staleness mechanism — nothing new to build, only to verify).

**Placeholder scan:** Task 4's `_factories` implementer note flags a real encapsulation decision to make during implementation (add a public `SourceManager.is_registered`) rather than guessing its current shape wrong.

**Type consistency:** `RemoteSource.__init__`'s signature (Task 2) matches exactly how Task 4's `_sync_remote_sources` constructs it (`base_url, token, pinned_fingerprint, room_map`). `connect_peer_live`'s signature (Task 1) matches `RemoteSource`'s default `connect` (Task 2). `PeerStore.token_for`/`.list`/`.room_map` (Phase 1) are consumed identically across Tasks 2-4 with no drift.
