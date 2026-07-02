# Wavr Camadas 3 + 4 — Away mode + AI narration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Two layers on the fusion foundation. **Camada 3 (away mode):** a house-level presence monitor that subscribes to `RoomState`, computes home/away, and emits MQTT `wavr/house/state` (retained) + `wavr/house/event` (arrived/left) on transitions — so Home Assistant can arm on "left". **Camada 4 (AI narration):** a `POST /api/narrate` endpoint that summarizes current state + recent history into natural language via Gemini. Both mock-tested; no broker, no Gemini install, no key needed to build/test.

**Architecture:** `AwayMonitor` mirrors `RulesEngine` (Hub subscriber + injected `publish` seam), tracking per-room occupancy → house-level home/away with debounce. `Narrator` builds a prompt from state+history and calls an injected `generate(prompt)->str` seam whose real default lazy-imports the Gemini SDK. `create_app` starts the away monitor as a lifespan task (same opt-in as MQTT) and exposes `POST /api/narrate` (CSRF+loopback, injectable narrator). `fusion.py`/`Hub`/sources/`RulesEngine` untouched.

**Tech Stack:** Python 3.11+, asyncio. `google-generativeai` (OPTIONAL `[genai]` extra, NOT installed — lazy-imported on the real path).

## Global Constraints

- Platform Windows 11; interpreter `C:\IA\wavr\.venv\Scripts\python.exe`; run from `C:\IA\wavr`.
- RoomState dict keys EXACT `{room, occupied, confidence, vitals, sources, explanation, ts}`. Do NOT change the shape or `fusion.py`.
- **PRIVACY — Camada 3:** away monitor publishes only house-level home/away + arrived/left — never room detail, frames, CSI, or vitals. `localhost` broker only. Nothing to Plano B.
- **PRIVACY — Camada 4 (THE ONE CLOUD EGRESS, by design):** narration sends DERIVED state to Gemini (Google cloud) — room names, occupied bools, confidence, timestamps, occupancy explanations. It MUST NEVER send frames, CSI, raw camera data, MAC addresses, or the RTSP URLs. This is the only path in the whole system that leaves the LAN; it is opt-in (requires `GEMINI_API_KEY` set) AND user-initiated (an explicit `POST /api/narrate`), CSRF+loopback guarded. The prompt builder must include ONLY the canonical RoomState fields, never anything else.
- OPT-IN: away monitor runs only when MQTT is enabled/injected (same gate as Camada 2). Narration returns 503 when no narrator/key is configured — never crashes.
- Optional deps: `google-generativeai` under `[genai]`, NOT default, NOT installed. Lazy import. Tests never import it.
- TDD; files < 500 lines; DRY, YAGNI. Tests never open a socket to a broker or call Gemini.

**Branch:** `camada3-4-away-narration` off `master` (A/B/C/camera-config/Camada2 merged).

**Existing interfaces:**
- `wavr.hub.Hub` — `subscribe()`/`unsubscribe(q)`/`async publish(item)`.
- `wavr.rules.RulesEngine(publish, prefix)` — the pattern AwayMonitor mirrors (sync `handle(rs)` + async `run(hub)`).
- `create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None, rules_publish=None)` — builds `_hub`, `_storage`, a `latest: dict[room->RoomState]`, `_rules_publish` (None unless injected/mqtt_enabled), `require_local` CSRF dep, an existing lifespan that starts the manager + optional RulesEngine + closes cameras. `GET /api/state` returns `latest`; `GET /api/history` returns `_storage.recent(limit)`.
- `wavr.config.load_config()` — add away + gemini fields.

---

### Task 1: `AwayMonitor` — house-level home/away + arrived/left events

**Files:**
- Create: `backend/wavr/away.py`
- Create: `backend/tests/test_away.py`
- Modify: `backend/wavr/config.py` (`away_grace`)
- Modify: `backend/tests/test_config.py`

**Interfaces:**
- Produces: `AwayMonitor(publish: Callable[[str,str,bool],None], prefix: str = "wavr", away_grace: int = 3)` with sync `handle(rs: dict)` and async `run(hub)`. Tracks per-room occupancy; house is "home" if ANY known room is occupied. Publishes retained `{prefix}/house/state` (`"home"`/`"away"`) on any house-state change, and `{prefix}/house/event` (`"arrived"`/`"left"`, not retained) on transitions EXCEPT the first determination. "Away" is debounced: declared only after `away_grace` consecutive all-vacant updates; "home" is immediate. `Config` gains `away_grace: int`.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_away.py`

```python
import asyncio
import pytest
from wavr.away import AwayMonitor
from wavr.hub import Hub

def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}

def test_first_occupied_sets_home_retained_no_event():
    msgs = []
    AwayMonitor(lambda t, p, r: msgs.append((t, p, r))).handle(_rs("sala", True))
    assert msgs == [("wavr/house/state", "home", True)]   # retained state, NO arrived event on first determination

def test_away_is_debounced_home_is_immediate():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=3)
    m.handle(_rs("sala", True))                            # home
    msgs.clear()
    m.handle(_rs("sala", False))                           # all-vacant streak 1 -> not yet away
    m.handle(_rs("sala", False))                           # streak 2
    assert msgs == []                                      # debounced, nothing published yet
    m.handle(_rs("sala", False))                           # streak 3 == grace -> away
    assert msgs == [("wavr/house/state", "away", True),
                    ("wavr/house/event", "left", False)]

def test_arrived_event_on_away_to_home():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1)
    m.handle(_rs("sala", True))                            # home (first, no event)
    m.handle(_rs("sala", False))                           # grace 1 -> away
    msgs.clear()
    m.handle(_rs("sala", True))                            # away -> home
    assert msgs == [("wavr/house/state", "home", True),
                    ("wavr/house/event", "arrived", False)]

def test_house_is_any_room_occupied():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)), away_grace=1)
    m.handle(_rs("sala", True)); m.handle(_rs("quarto", True))
    msgs.clear()
    m.handle(_rs("sala", False))                           # sala vacant but quarto still occupied -> still home
    assert not any(t == "wavr/house/state" and p == "away" for t, p, r in msgs)

def test_no_duplicate_state_when_unchanged():
    msgs = []
    m = AwayMonitor(lambda t, p, r: msgs.append((t, p, r)))
    m.handle(_rs("sala", True)); m.handle(_rs("sala", True))
    assert [m2 for m2 in msgs if m2[0] == "wavr/house/state"] == [("wavr/house/state", "home", True)]

async def test_run_consumes_hub_and_unsubscribes():
    msgs = []
    hub = Hub()
    task = asyncio.create_task(AwayMonitor(lambda t, p, r: msgs.append(t)).run(hub))
    await asyncio.sleep(0)
    await hub.publish(_rs("sala", True))
    await asyncio.sleep(0.01)
    assert "wavr/house/state" in msgs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub._subscribers == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_away.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.away'`.

- [ ] **Step 3: Implement** — create `backend/wavr/away.py`

```python
from __future__ import annotations

from typing import Callable


class AwayMonitor:
    """House-level presence: home if ANY room is occupied, else away (debounced).
    Publishes retained house state + arrived/left edge events for home automation.
    Only house-level home/away is published — never room detail/frames/vitals."""

    def __init__(self, publish: Callable[[str, str, bool], None],
                 prefix: str = "wavr", away_grace: int = 3):
        self._publish = publish
        self._prefix = prefix
        self._grace = away_grace
        self._rooms: dict[str, bool] = {}
        self._house: bool | None = None   # True=home, False=away, None=undetermined
        self._vacant_streak = 0

    def handle(self, rs: dict) -> None:
        self._rooms[rs["room"]] = bool(rs["occupied"])
        if any(self._rooms.values()):
            self._vacant_streak = 0
            self._set_house(True)
        else:
            self._vacant_streak += 1
            if self._vacant_streak >= self._grace:
                self._set_house(False)

    def _set_house(self, home: bool) -> None:
        if self._house == home:
            return
        first = self._house is None
        self._house = home
        self._publish(f"{self._prefix}/house/state", "home" if home else "away", True)
        if not first:
            self._publish(f"{self._prefix}/house/event", "arrived" if home else "left", False)

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_away.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Config** — modify `backend/wavr/config.py`

Add `away_grace: int` to the dataclass and `away_grace=int(os.getenv("WAVR_AWAY_GRACE", "3"))` to `load_config()`.

- [ ] **Step 6: Config test** — modify `backend/tests/test_config.py`

```python
def test_config_has_away_default(monkeypatch):
    monkeypatch.delenv("WAVR_AWAY_GRACE", raising=False)
    from wavr.config import load_config
    assert load_config().away_grace == 3
```

- [ ] **Step 7: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (expect 91 + new).

```powershell
git add backend/wavr/away.py backend/tests/test_away.py backend/wavr/config.py backend/tests/test_config.py
git commit -m "feat: AwayMonitor -- house-level home/away + arrived/left events (debounced)"
```

---

### Task 2: Wire `AwayMonitor` into `create_app` lifespan

**Files:**
- Modify: `backend/wavr/app.py`
- Modify: `backend/tests/test_rules_wiring.py` (add away assertion) OR create `backend/tests/test_away_wiring.py`

**Interfaces:**
- Consumes: `AwayMonitor` (Task 1), the existing `_rules_publish` decision in `create_app`.
- Produces: when `_rules_publish` is set (injected publisher or `mqtt_enabled`), an `AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace)` also runs as a lifespan task on `_hub`, cancelled on shutdown. Reuses the same publisher as `RulesEngine` — one MQTT surface.

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_away_wiring.py`

```python
import asyncio
import pytest
from wavr.app import create_app
from wavr.hub import Hub

def _rs(room, occupied):
    return {"room": room, "occupied": occupied, "confidence": 0.9,
            "vitals": {}, "sources": [], "explanation": "", "ts": "2026-07-02T10:00:00+00:00"}

async def test_away_monitor_publishes_house_state_when_enabled():
    msgs = []
    hub = Hub()
    app = create_app(sources=[], hub=hub, rules_publish=lambda t, p, r: msgs.append((t, p, r)))
    async with app.router.lifespan_context(app):
        await hub.publish(_rs("sala", True))
        await asyncio.sleep(0.02)
    assert any(t == "wavr/house/state" for t, _, _ in msgs)      # away monitor ran

async def test_no_away_task_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_MQTT_ENABLED", raising=False)
    hub = Hub()
    app = create_app(sources=[], hub=hub)                         # no publisher, mqtt off
    async with app.router.lifespan_context(app):
        pass
    assert hub._subscribers == set()                             # no rules AND no away subscriber
```

> Note: `test_away_monitor_publishes_house_state_when_enabled` asserts BOTH rules and away subscribe to the hub — with the injected publisher, publishing one occupied RoomState should yield both a `wavr/rooms/sala/state` (rules) and a `wavr/house/state` (away) message. Assert the house one specifically. Use the `lifespan_context` mechanism (same as the existing rules-wiring test) to avoid the cross-loop hazard.

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_away_wiring.py -q`
Expected: FAIL — no away task starts, no `wavr/house/state` message.

- [ ] **Step 3: Implement** — modify `backend/wavr/app.py`

Add import: `from wavr.away import AwayMonitor`.

Where `_rules` is decided (after `_rules_publish`), add:

```python
    _away = AwayMonitor(_rules_publish, prefix=cfg.mqtt_prefix, away_grace=cfg.away_grace) if _rules_publish else None
```

In `lifespan`, start it alongside the rules task and cancel it in the `finally` (mirror the existing rules_task handling; suppress CancelledError + Exception so shutdown always proceeds):

```python
        rules_task = asyncio.create_task(_rules.run(_hub)) if _rules else None
        away_task = asyncio.create_task(_away.run(_hub)) if _away else None
        try:
            yield
        finally:
            for t in (rules_task, away_task):
                if t:
                    t.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await t
            await manager.stop()
            if _owns_cameras:
                with suppress(Exception):
                    _cameras.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_away_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (all green; `test_app.py` unaffected — mqtt off, no publisher).

```powershell
git add backend/wavr/app.py backend/tests/test_away_wiring.py
git commit -m "feat: wire AwayMonitor into lifespan (shares the MQTT publisher, same opt-in)"
```

---

### Task 3: `Narrator` — state+history → natural language via injected LLM seam (lazy Gemini)

**Files:**
- Create: `backend/wavr/narrator.py`
- Create: `backend/tests/test_narrator.py`
- Modify: `backend/wavr/config.py` (gemini fields)
- Modify: `backend/tests/test_config.py`
- Modify: `backend/pyproject.toml` (`[genai]` optional extra)

**Interfaces:**
- Produces: `build_prompt(state: dict, history: list) -> str` (a prompt from ONLY canonical RoomState fields — room/occupied/confidence/ts/explanation; NEVER vitals-raw/frames/urls). `Narrator(generate: Callable[[str], str])` with `narrate(state: dict, history: list) -> str` = `generate(build_prompt(...))`. `make_gemini_generate(api_key: str, model: str) -> Callable[[str], str]` — lazy-imports the Gemini SDK, returns a `generate(prompt)->str`. `Config` gains `gemini_api_key: str` (from `GEMINI_API_KEY`), `gemini_model: str`.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_narrator.py`

```python
from wavr.narrator import Narrator, build_prompt

STATE = {"sala": {"room": "sala", "occupied": True, "confidence": 0.77, "vitals": {"breathing_bpm": 14.2},
                  "sources": [{"modality": "wifi_csi"}], "explanation": "wifi: presente", "ts": "2026-07-02T10:00:00+00:00"}}
HISTORY = [{"room": "sala", "occupied": False, "confidence": 0.1, "vitals": {}, "sources": [],
            "explanation": "", "ts": "2026-07-02T09:59:00+00:00"}]

def test_build_prompt_includes_room_state_but_never_secrets():
    p = build_prompt(STATE, HISTORY)
    assert "sala" in p and ("ocupad" in p.lower() or "occupied" in p.lower())
    # PRIVACY: raw vitals numbers, source internals must not be dumped into the cloud prompt
    assert "14.2" not in p           # raw breathing value never sent
    assert "wifi_csi" not in p       # source modality internals not sent (occupancy summary only)

def test_narrate_calls_generate_with_prompt():
    seen = {}
    def fake_generate(prompt):
        seen["prompt"] = prompt
        return "Sala ocupada desde as 10h."
    out = Narrator(fake_generate).narrate(STATE, HISTORY)
    assert out == "Sala ocupada desde as 10h."
    assert "sala" in seen["prompt"]
```

> The prompt must summarize occupancy per room (name + occupied + confidence% + a human time from `ts`) and a short history trend — NOT dump raw dicts. Keep `vitals` values and `sources` internals OUT of the prompt (privacy: the cloud sees occupancy, not biometrics). `explanation` (already a derived human string) is OK to include.

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_narrator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.narrator'`.

- [ ] **Step 3: Implement** — create `backend/wavr/narrator.py`

```python
from __future__ import annotations

from typing import Callable


def build_prompt(state: dict, history: list) -> str:
    """Build a natural-language-summary prompt from DERIVED occupancy only. Never
    include raw vitals numbers, source internals, frames, MACs, or RTSP URLs — the
    cloud LLM sees occupancy, not biometrics."""
    lines = ["Resuma em português, em 1-2 frases, o estado de presença da casa.",
             "Estado atual por cômodo:"]
    for room, rs in sorted(state.items()):
        pct = round(rs.get("confidence", 0) * 100)
        status = "ocupado" if rs.get("occupied") else "vazio"
        lines.append(f"- {room}: {status} ({pct}% de confiança)")
    if history:
        occ = sum(1 for h in history if h.get("occupied"))
        lines.append(f"Nas últimas {len(history)} leituras houve {occ} com presença detectada.")
    return "\n".join(lines)


class Narrator:
    """Summarizes derived RoomState into natural language via an injected LLM seam."""

    def __init__(self, generate: Callable[[str], str]):
        self._generate = generate

    def narrate(self, state: dict, history: list) -> str:
        return self._generate(build_prompt(state, history))


_MODEL = None


def make_gemini_generate(api_key: str, model: str = "gemini-1.5-flash") -> Callable[[str], str]:
    """Real generator: lazy-imports the Gemini SDK. Only reached when narration is
    configured + invoked."""
    def generate(prompt: str) -> str:
        global _MODEL
        if _MODEL is None:
            import google.generativeai as genai   # optional dep
            genai.configure(api_key=api_key)
            _MODEL = genai.GenerativeModel(model)
        return _MODEL.generate_content(prompt).text
    return generate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_narrator.py -q`
Expected: PASS.

- [ ] **Step 5: Config** — modify `backend/wavr/config.py`

Add `gemini_api_key: str` and `gemini_model: str` to the dataclass, and to `load_config()`:

```python
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("WAVR_GEMINI_MODEL", "gemini-1.5-flash"),
```

Add a config test asserting defaults (empty key, default model) with the env vars deleted.

- [ ] **Step 6: Optional extra** — modify `backend/pyproject.toml`

Under `[project.optional-dependencies]`: `genai = ["google-generativeai>=0.7"]`. Do NOT install.

- [ ] **Step 7: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (no genai imported).

```powershell
git add backend/wavr/narrator.py backend/tests/test_narrator.py backend/wavr/config.py backend/tests/test_config.py backend/pyproject.toml
git commit -m "feat: Narrator -- derived-state -> NL summary via injected LLM (lazy Gemini, deps optional)"
```

---

### Task 4: `POST /api/narrate` endpoint + wiring

**Files:**
- Modify: `backend/wavr/app.py`
- Create: `backend/tests/test_narrate_api.py`

**Interfaces:**
- Consumes: `Narrator`/`make_gemini_generate` (Task 3), the existing `latest` dict + `_storage.recent`, `require_local`.
- Produces: `create_app(..., narrator=None)`. `POST /api/narrate` (CSRF+loopback): if a narrator is configured (injected, or `cfg.gemini_api_key` set → build one with `make_gemini_generate`), return `{"narration": narrator.narrate(latest, _storage.recent(50))}`; else `503 {"detail": "narration not configured (set GEMINI_API_KEY)"}`. Never crashes on a Gemini error — wrap the call and return `502` on failure.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_narrate_api.py`

```python
from fastapi.testclient import TestClient
from wavr.app import create_app

def _client(narrator=None):
    app = create_app(sources=[], narrator=narrator)
    return TestClient(app, headers={"X-Wavr-Local": "1"})

class _FakeNarrator:
    def narrate(self, state, history):
        return "Casa vazia no momento."

def test_narrate_returns_text_when_configured():
    with _client(narrator=_FakeNarrator()) as c:
        r = c.post("/api/narrate")
        assert r.status_code == 200
        assert r.json()["narration"] == "Casa vazia no momento."

def test_narrate_503_when_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with _client(narrator=None) as c:                     # no narrator, no key
        assert c.post("/api/narrate").status_code == 503

def test_narrate_requires_local_header():
    from wavr.app import create_app
    with TestClient(create_app(sources=[], narrator=_FakeNarrator())) as c:  # no header
        assert c.post("/api/narrate").status_code == 403

def test_narrate_502_on_generator_error():
    class _Boom:
        def narrate(self, state, history):
            raise RuntimeError("gemini down")
    with _client(narrator=_Boom()) as c:
        assert c.post("/api/narrate").status_code == 502
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_narrate_api.py -q`
Expected: FAIL — no `narrator` kwarg / no `/api/narrate` route.

- [ ] **Step 3: Implement** — modify `backend/wavr/app.py`

Add import: `from wavr.narrator import Narrator, make_gemini_generate`. Add `narrator=None` to `create_app`. Build the effective narrator once:

```python
    _narrator = narrator
    if _narrator is None and cfg.gemini_api_key:
        _narrator = Narrator(make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model))
```

Add the endpoint (near the other routes):

```python
    @app.post("/api/narrate")
    async def narrate(_=Depends(require_local)):
        if _narrator is None:
            raise HTTPException(status_code=503, detail="narration not configured (set GEMINI_API_KEY)")
        try:
            text = _narrator.narrate(latest, _storage.recent(50))
        except Exception:
            raise HTTPException(status_code=502, detail="narration backend error")
        return {"narration": text}
```

> PRIVACY: `latest`/`_storage.recent` are derived RoomState only — the narrator's `build_prompt` further strips to occupancy summaries before the cloud call. Do NOT pass raw frames/vitals; there are none in this data anyway.

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_narrate_api.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Confirm no regression + full suite**

`test_app.py` doesn't inject a narrator and (in tests) `GEMINI_API_KEY` is typically unset → `_narrator` is None → the new route just 503s if hit; existing routes unaffected. Run the full suite:
Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (all).

> If a real `GEMINI_API_KEY` is present in the test environment's `.env`, `_narrator` would be built with the real Gemini generator — but no test calls it in a way that triggers a network call except through an injected fake, so no live API call happens in the suite. If `test_narrate_503_when_not_configured` fails because a key is set, have it also pass `narrator=None` AND construct the app with a cfg override / monkeypatch `cfg.gemini_api_key` to "" — note this in the report.

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/app.py backend/tests/test_narrate_api.py
git commit -m "feat: POST /api/narrate -- AI narration endpoint (opt-in, CSRF, 503 when unconfigured)"
```

---

## Definition of Done
- [ ] `AwayMonitor` publishes retained `wavr/house/state` (home/away) + `wavr/house/event` (arrived/left) on transitions; away debounced, home immediate; house = any-room-occupied; first determination emits no event; fully unit-tested.
- [ ] Away monitor runs as a lifespan task only when MQTT is enabled/injected (same opt-in), sharing the RulesEngine publisher; cancelled on shutdown; off-by-default unchanged.
- [ ] `Narrator` summarizes DERIVED occupancy (never raw vitals/sources/frames) into NL via an injected LLM seam; real default lazy-imports Gemini; deps optional/not installed.
- [ ] `POST /api/narrate` returns the narration when configured, 503 when not, 502 on backend error; CSRF+loopback guarded; the only cloud egress, opt-in + user-initiated, derived-data-only.
- [ ] `fusion.py`/`Hub`/sources/`RulesEngine`/`CameraSource` untouched; full suite green.

## Next
Live: run Mosquitto + `WAVR_MQTT_ENABLED=1` to see house state in Home Assistant; set `GEMINI_API_KEY` + `pip install -e backend[genai]` and `POST /api/narrate` for a spoken-language house summary. A dashboard "Narrar" button could call the endpoint (future frontend task).
