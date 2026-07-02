# Wavr Camada 2 — Rules + MQTT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** React to fused `RoomState` and emit MQTT for home automation — a `RulesEngine` subscribes to the `Hub`, publishes each room's current occupancy to a retained MQTT topic, and emits edge events on occupancy transitions (vacant↔occupied). Opt-in via config; fully mock-tested (no broker, no paho install needed to build/test).

**Architecture:** A `RulesEngine` holds an injected `publish(topic, payload, retain)` seam and pure `handle(roomstate)` logic (retained state + edge detection). The real default publisher lazy-imports `paho-mqtt` and connects to `localhost:1883`; tests inject a fake publisher. `create_app` starts the engine's `run(hub)` as a lifespan task when MQTT is enabled (or when a publisher is injected). `Hub`/`FusionEngine`/sources untouched — this is a pure consumer bolted onto the existing `Hub` fan-out.

**Tech Stack:** Python 3.11+, asyncio, `paho-mqtt` (OPTIONAL extra, NOT installed — lazy-imported on the real path only). Standard `json`.

## Global Constraints

- Platform Windows 11; interpreter `C:\IA\wavr\.venv\Scripts\python.exe`; run from `C:\IA\wavr`.
- The `RoomState` dict on the Hub has EXACT keys `{room, occupied, confidence, vitals, sources, explanation, ts}` (`occupied` bool, `confidence` float, `ts` ISO-8601). The RulesEngine consumes these — do NOT change the shape or `fusion.py`.
- PRIVACY: only DERIVED state leaves via MQTT (room name + occupied bool + confidence + ts) — NEVER frames, CSI, MACs, or raw vitals. Publish to `localhost:1883` only (LAN broker); nothing touches Plano B / the public build.
- OPT-IN: MQTT is off by default (`WAVR_MQTT_ENABLED` default false). When disabled, no RulesEngine task starts, no paho import happens — existing behavior and tests are unchanged.
- `paho-mqtt` is an OPTIONAL extra (`[mqtt]`), NOT a default dependency, NOT installed in this plan. The real import is lazy (inside the publisher factory) so the module loads and all tests run without it.
- TDD; files < 500 lines; DRY, YAGNI. Tests never import paho, never open a socket to a broker.

**Branch:** `camada2-rules-mqtt` off `master` (A/B/C/camera-config merged).

**Existing interfaces:**
- `wavr.hub.Hub` — `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `async publish(item: dict)`. RoomState dicts flow through it (published by `create_app._ingest`).
- `create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None)` — builds `_hub`, registers sources, `lifespan` starts/stops the `SourceManager`. `_ingest` does `await _hub.publish(rs.to_dict())`.
- `wavr.config.load_config() -> Config` — dataclass; add MQTT fields.

---

### Task 1: `RulesEngine` — occupancy edge detection + retained state, injected publish seam

**Files:**
- Create: `backend/wavr/rules.py`
- Create: `backend/tests/test_rules.py`
- Modify: `backend/wavr/config.py` (MQTT config fields)
- Modify: `backend/tests/test_config.py` (assert defaults)

**Interfaces:**
- Produces: `RulesEngine(publish: Callable[[str, str, bool], None], prefix: str = "wavr")` with sync `handle(rs: dict) -> None` (publishes retained state topic + edge event on transition) and `async run(hub) -> None` (subscribes to the hub, calls `handle` per RoomState, unsubscribes in `finally`). `Config` gains `mqtt_enabled: bool`, `mqtt_host: str`, `mqtt_port: int`, `mqtt_prefix: str`.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_rules.py`

```python
import asyncio
import json
import pytest
from wavr.rules import RulesEngine
from wavr.hub import Hub

def _rs(room, occupied, confidence=0.8, ts="2026-07-02T10:00:00+00:00"):
    return {"room": room, "occupied": occupied, "confidence": confidence,
            "vitals": {}, "sources": [], "explanation": "", "ts": ts}

def test_handle_publishes_retained_state_each_call():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", True, 0.77))
    state = [m for m in msgs if m[0] == "wavr/rooms/sala/state"]
    assert len(state) == 1
    topic, payload, retain = state[0]
    assert retain is True
    assert json.loads(payload) == {"occupied": True, "confidence": 0.77,
                                    "ts": "2026-07-02T10:00:00+00:00"}

def test_edge_event_only_on_transition():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", False))   # first sighting -> no edge event
    eng.handle(_rs("sala", False))   # no change -> no edge event
    eng.handle(_rs("sala", True))    # vacant -> occupied -> event
    eng.handle(_rs("sala", True))    # no change -> no event
    eng.handle(_rs("sala", False))   # occupied -> vacant -> event
    events = [m for m in msgs if m[0] == "wavr/rooms/sala/event"]
    assert [p for _, p, _ in events] == ["occupied", "vacant"]
    assert all(r is False for _, _, r in events)   # events are not retained

def test_edge_events_are_per_room():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    eng.handle(_rs("sala", False)); eng.handle(_rs("quarto", False))
    eng.handle(_rs("sala", True))                       # only sala flips
    events = [m for m in msgs if m[0].endswith("/event")]
    assert events == [("wavr/rooms/sala/event", "occupied", False)]

def test_prefix_is_configurable():
    msgs = []
    RulesEngine(lambda t, p, r: msgs.append(t), prefix="casa").handle(_rs("sala", True))
    assert msgs[0] == "casa/rooms/sala/state"

async def test_run_consumes_hub_and_unsubscribes():
    msgs = []
    hub = Hub()
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)))
    task = asyncio.create_task(eng.run(hub))
    await asyncio.sleep(0)                              # let it subscribe
    await hub.publish(_rs("sala", True))
    await asyncio.sleep(0.01)
    assert any(t == "wavr/rooms/sala/state" for t, _, _ in msgs)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub._subscribers == set()                   # unsubscribed on cancel
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_rules.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.rules'`.

- [ ] **Step 3: Implement** — create `backend/wavr/rules.py`

```python
from __future__ import annotations

import json
from typing import Callable


class RulesEngine:
    """Consumes fused RoomState from the Hub and emits MQTT for home automation.
    Publishes each room's current occupancy to a RETAINED state topic (so a broker
    subscriber always sees the latest), and an edge EVENT topic only when occupancy
    flips. Only derived state is published — never frames/CSI/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None], prefix: str = "wavr"):
        self._publish = publish
        self._prefix = prefix
        self._last: dict[str, bool] = {}   # room -> last occupied

    def handle(self, rs: dict) -> None:
        room = rs["room"]
        occupied = bool(rs["occupied"])
        self._publish(
            f"{self._prefix}/rooms/{room}/state",
            json.dumps({"occupied": occupied, "confidence": rs["confidence"], "ts": rs["ts"]}),
            True,   # retained: latest state persists on the broker
        )
        prev = self._last.get(room)
        if prev is not None and prev != occupied:
            self._publish(f"{self._prefix}/rooms/{room}/event",
                          "occupied" if occupied else "vacant", False)
        self._last[room] = occupied

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_rules.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Add MQTT config** — modify `backend/wavr/config.py`

Add to the `Config` dataclass:

```python
    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_prefix: str
```

And in `load_config()`:

```python
        mqtt_enabled=os.getenv("WAVR_MQTT_ENABLED", "").lower() in ("1", "true", "yes"),
        mqtt_host=os.getenv("WAVR_MQTT_HOST", "localhost"),
        mqtt_port=int(os.getenv("WAVR_MQTT_PORT", "1883")),
        mqtt_prefix=os.getenv("WAVR_MQTT_PREFIX", "wavr"),
```

- [ ] **Step 6: Config test** — modify `backend/tests/test_config.py`

```python
def test_config_has_mqtt_defaults(monkeypatch):
    for v in ("WAVR_MQTT_ENABLED", "WAVR_MQTT_HOST", "WAVR_MQTT_PORT", "WAVR_MQTT_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.mqtt_enabled is False       # opt-in: off by default
    assert cfg.mqtt_host == "localhost"
    assert cfg.mqtt_port == 1883
    assert cfg.mqtt_prefix == "wavr"
```

- [ ] **Step 7: Run affected tests + full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_rules.py backend/tests/test_config.py -q` then `... -m pytest backend/tests -q` (expect 81 + new).

```powershell
git add backend/wavr/rules.py backend/tests/test_rules.py backend/wavr/config.py backend/tests/test_config.py
git commit -m "feat: RulesEngine — RoomState -> MQTT (retained state + occupancy edge events)"
```

---

### Task 2: Real MQTT publisher (lazy paho) + optional `[mqtt]` extra

**Files:**
- Create: `backend/wavr/mqtt_publisher.py`
- Create: `backend/tests/test_mqtt_publisher.py`
- Modify: `backend/pyproject.toml` (`[mqtt]` optional extra)

**Interfaces:**
- Produces: `make_publisher(host: str = "localhost", port: int = 1883) -> Callable[[str, str, bool], None]` — returns a `publish(topic, payload, retain)` closure that lazily connects a paho client (once) to `host:port` and publishes. Boundary helper `_client(host, port)` isolates the lazy `import paho.mqtt.client` so tests monkeypatch it without paho installed.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_mqtt_publisher.py`

```python
import pytest
from wavr import mqtt_publisher as mp

def test_make_publisher_calls_client_publish(monkeypatch):
    calls = []
    class FakeClient:
        def publish(self, topic, payload, retain=False):
            calls.append((topic, payload, retain))
    monkeypatch.setattr(mp, "_client", lambda host, port: FakeClient())
    publish = mp.make_publisher("localhost", 1883)
    publish("wavr/rooms/sala/state", '{"occupied": true}', True)
    assert calls == [("wavr/rooms/sala/state", '{"occupied": true}', True)]

def test_publisher_never_raises_on_client_error(monkeypatch):
    class BadClient:
        def publish(self, *a, **k):
            raise RuntimeError("broker down")
    monkeypatch.setattr(mp, "_client", lambda host, port: BadClient())
    publish = mp.make_publisher()
    publish("t", "p", False)   # must NOT raise — a dead broker can't crash the rules loop
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_mqtt_publisher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.mqtt_publisher'`.

- [ ] **Step 3: Implement** — create `backend/wavr/mqtt_publisher.py`

```python
from __future__ import annotations

import contextlib
import logging
from typing import Callable

_CLIENT = None


def _client(host: str, port: int):
    """Lazily create + connect a paho MQTT client (once). Lazy import so paho is
    only needed on the real path; connect_async + loop_start means publish never
    blocks and reconnects on its own if the broker is down."""
    global _CLIENT
    if _CLIENT is None:
        import paho.mqtt.client as mqtt   # optional dep, only imported when MQTT is enabled
        c = mqtt.Client()
        c.connect_async(host, port)
        c.loop_start()
        _CLIENT = c
    return _CLIENT


def make_publisher(host: str = "localhost", port: int = 1883) -> Callable[[str, str, bool], None]:
    def publish(topic: str, payload: str, retain: bool) -> None:
        with contextlib.suppress(Exception):        # a dead broker must not crash the rules loop
            _client(host, port).publish(topic, payload, retain=retain)
        # note: suppression also swallows a missing-paho ImportError, so an enabled-but-
        # uninstalled MQTT degrades to a no-op with a one-time warning rather than a crash.
    return publish
```

> Add a one-time `logging.warning` if you want visibility when paho is missing — optional; keep the suppress so it never raises.

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_mqtt_publisher.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Optional extra** — modify `backend/pyproject.toml`

Under `[project.optional-dependencies]` add:

```toml
mqtt = ["paho-mqtt>=2.0"]
```

Do NOT install it.

- [ ] **Step 6: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (no paho imported).

```powershell
git add backend/wavr/mqtt_publisher.py backend/tests/test_mqtt_publisher.py backend/pyproject.toml
git commit -m "feat: lazy paho MQTT publisher (broker-tolerant, deps optional, mock-tested)"
```

---

### Task 3: Wire the RulesEngine into `create_app` lifespan (opt-in)

**Files:**
- Modify: `backend/wavr/app.py`
- Create: `backend/tests/test_rules_wiring.py`

**Interfaces:**
- Consumes: `RulesEngine` (Task 1), `make_publisher` (Task 2), `Hub`, `create_app`.
- Produces: `create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None, rules_publish=None)`. When `rules_publish` is provided OR `cfg.mqtt_enabled` is true, a `RulesEngine` is started as a lifespan task subscribed to `_hub`; it's cancelled on shutdown. `rules_publish` (a `publish(topic, payload, retain)` callable) overrides the real paho publisher — used by tests.

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_rules_wiring.py`

```python
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from wavr.app import create_app
from wavr.hub import Hub

def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}

def test_injected_publisher_receives_roomstate_via_hub():
    msgs = []
    hub = Hub()
    app = create_app(sources=[], hub=hub, rules_publish=lambda t, p, r: msgs.append((t, p, r)))
    with TestClient(app):                              # enters lifespan -> starts rules task
        # push a RoomState onto the same hub the app uses
        asyncio.get_event_loop().run_until_complete(_pump(hub))
    assert any(t == "wavr/rooms/sala/state" for t, _, _ in msgs)

async def _pump(hub):
    await hub.publish(_rs("sala", True))
    await asyncio.sleep(0.02)

def test_no_rules_task_when_disabled_and_no_publisher(monkeypatch):
    monkeypatch.delenv("WAVR_MQTT_ENABLED", raising=False)   # disabled default
    hub = Hub()
    app = create_app(sources=[], hub=hub)                    # no rules_publish, mqtt off
    with TestClient(app):
        pass
    assert hub._subscribers == set()                         # nothing subscribed -> no rules engine
```

> If `run_until_complete` is awkward under the running TestClient loop, drive the RoomState by publishing to `hub` inside an `async` test with `httpx.ASGITransport` lifespan, or assert via `hub._subscribers` growing by one after entering the client (a rules subscriber present). Keep whichever cleanly proves: enabled/injected → a subscriber runs and receives; disabled → none. The implementer may adapt the mechanism as long as both properties are asserted with no real broker.

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_rules_wiring.py -q`
Expected: FAIL — `create_app()` has no `rules_publish` kwarg / no rules task starts.

- [ ] **Step 3: Implement** — modify `backend/wavr/app.py`

Add imports: `from wavr.rules import RulesEngine` and `from wavr.mqtt_publisher import make_publisher`.

Add `rules_publish=None` to the `create_app` signature. After `_hub` is built and before/around the `lifespan` definition, decide whether to run rules:

```python
    _rules_publish = rules_publish
    if _rules_publish is None and cfg.mqtt_enabled:
        _rules_publish = make_publisher(cfg.mqtt_host, cfg.mqtt_port)
    _rules = RulesEngine(_rules_publish, prefix=cfg.mqtt_prefix) if _rules_publish else None
```

In `lifespan`, start/stop the rules task alongside the manager:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        rules_task = asyncio.create_task(_rules.run(_hub)) if _rules else None
        try:
            yield
        finally:
            if rules_task:
                rules_task.cancel()
                with suppress(asyncio.CancelledError):
                    await rules_task
            await manager.stop()
            if _owns_cameras:
                with suppress(Exception):
                    _cameras.close()
```

(Add `import asyncio` and ensure `from contextlib import suppress` is imported — the camera-config fix-wave already added `suppress`; reuse it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_rules_wiring.py -q`
Expected: PASS. If the `run_until_complete` mechanism fights the TestClient loop, switch to the `hub._subscribers` assertion approach described in the Step-1 note.

- [ ] **Step 5: Confirm no regression + full suite**

`test_app.py` passes explicit `sources=` and no `rules_publish`, and MQTT is off by default, so no rules task starts there — unaffected. Run the full suite:
Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/app.py backend/tests/test_rules_wiring.py
git commit -m "feat: wire RulesEngine into lifespan (opt-in via WAVR_MQTT_ENABLED / injected publisher)"
```

---

## Definition of Done
- [ ] `RulesEngine` publishes each room's occupancy to a retained `wavr/rooms/{room}/state` topic and an edge `.../event` (`occupied`/`vacant`) only on transition; per-room; prefix configurable; fully unit-tested with a fake publisher.
- [ ] Real publisher lazy-imports paho, tolerates a down/missing broker (never raises), and is an optional `[mqtt]` extra (not installed).
- [ ] The engine runs as a lifespan task only when MQTT is enabled (or a publisher is injected); off by default — existing behavior/tests unchanged.
- [ ] Only derived state is published (room/occupied/confidence/ts) — never frames/CSI/vitals; `localhost` broker only; nothing touches Plano B. `fusion.py`/`Hub`/sources untouched.
- [ ] Full suite green.

## Next
Camada 3 (away mode) builds on these edge events; Camada 4 (AI narration) reads `/api/state` + `/api/history`. Live: run a local Mosquitto broker, `pip install -e backend[mqtt]`, set `WAVR_MQTT_ENABLED=1`, and subscribe a Home Assistant to `wavr/rooms/#`.
