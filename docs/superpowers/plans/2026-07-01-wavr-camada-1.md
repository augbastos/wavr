# Wavr — Camada 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation of Wavr — ingest a live WiFi-sensing stream, normalize it to a canonical event, store history, and render it on a live dashboard — with the same code running against real sensor data (Plano A, private) or simulated data (Plano B, public showcase).

**Architecture:** A Python/FastAPI backend reads events from a swappable `SensorSource` (RuView WebSocket or Simulated), writes each to SQLite, and fans them out to browser clients over its own WebSocket. A single-file HTML dashboard consumes events through a swappable `DataProvider` (WebSocket in Plano A, in-browser Simulator in Plano B). The data source is an interface on both ends — that seam is what lets one codebase serve both the private house and the public demo without real biometrics ever leaving the LAN.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, `websockets`, SQLite (stdlib), pytest + pytest-asyncio; vanilla HTML/CSS/JS; Cloudflare Pages (Plano B deploy).

## Global Constraints

- **Platform:** Windows 11, PowerShell primary. All shell commands are PowerShell. Use a venv at `C:\IA\wavr\.venv`.
- **Python:** 3.11+ (uses `X | None` types, `match`-free but modern stdlib).
- **Canonical event shape — EXACT, do not add/rename fields:** `{"room": str, "presence": bool, "motion": float, "breathing_bpm": float|None, "heart_bpm": float|None, "ts": str}` where `ts` is ISO-8601 UTC.
- **RuView WebSocket URL is `ws://localhost:3000/ws/sensing`** (NOT 8765 — that port is unpublished). Auth: `Authorization: Bearer <token>` header; if the server rejects the header, fall back to `?token=<token>` query param. Only messages with `type == "sensing_update"` are events.
- **RuView field mapping (verified against the live container):** `presence` ← `classification.presence`; `motion` ← `features.motion_band_power`; `breathing_bpm` ← `vital_signs.breathing_rate_bpm`; `heart_bpm` ← `vital_signs.heart_rate_bpm`; `ts` ← `timestamp` (unix float → ISO-8601). `room` is NOT in the payload — it is supplied by config (one RuView server = one room).
- **Privacy (non-negotiable):** the frontend auto-selects the Simulator whenever `location.hostname !== "localhost"`, so a public deploy can never connect to a local backend. The backend is never port-forwarded or tunneled. No real event ever leaves the LAN.
- **Secrets:** the RuView token comes from env (`WAVR_RUVIEW_TOKEN`); never hard-code it, never commit `.env`, never put any token in the frontend.
- **Discipline:** TDD (test first), frequent commits, every file < 500 lines, DRY, YAGNI. Camadas 2/3/4 (rules, away-mode, AI) are OUT OF SCOPE — only leave documented seams.

**Repo root for all paths below:** `C:\IA\wavr\`

---

### Task 1: Repo scaffold + canonical event model

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/wavr/__init__.py` (empty)
- Create: `backend/wavr/events.py`
- Create: `backend/tests/__init__.py` (empty)
- Create: `backend/tests/test_events.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `SensingEvent` frozen dataclass with fields `room: str, presence: bool, motion: float, breathing_bpm: float|None, heart_bpm: float|None, ts: str` and method `to_dict() -> dict`. Module function `normalize_ruview(raw: dict, room: str) -> SensingEvent`.

- [ ] **Step 1: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
*.db
.env
.pytest_cache/
node_modules/
```

- [ ] **Step 2: Create `backend/pyproject.toml`**

```toml
[project]
name = "wavr"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "websockets>=13",
  "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["wavr*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Create the two empty `__init__.py` files** (`backend/wavr/__init__.py`, `backend/tests/__init__.py`) — no content.

- [ ] **Step 4: Create the venv and install (one-time setup)**

Run (from `C:\IA\wavr`):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e "backend[dev]"
```
Expected: `Successfully installed wavr-0.1.0 ...` plus fastapi, websockets, pytest, etc.

- [ ] **Step 5: Write the failing test** — `backend/tests/test_events.py`

```python
from wavr.events import SensingEvent, normalize_ruview

# Real frame captured from the live RuView container (trimmed to the fields we use).
RUVIEW_FRAME = {
    "type": "sensing_update",
    "classification": {"presence": True, "motion_level": "present_still", "confidence": 0.43},
    "features": {"motion_band_power": 9.7758, "breathing_band_power": 23.65},
    "vital_signs": {"breathing_rate_bpm": 9.707, "heart_rate_bpm": 46.22, "signal_quality": 0.60},
    "timestamp": 1782924055.636,
}

def test_normalize_maps_ruview_fields_to_canonical():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert ev.room == "sala"
    assert ev.presence is True
    assert ev.motion == 9.7758
    assert ev.breathing_bpm == 9.707
    assert ev.heart_bpm == 46.22
    # unix 1782924055.636 → ISO-8601 UTC
    assert ev.ts.startswith("2026-")
    assert ev.ts.endswith("+00:00")

def test_to_dict_has_exact_canonical_keys():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert set(ev.to_dict().keys()) == {"room", "presence", "motion", "breathing_bpm", "heart_bpm", "ts"}

def test_missing_vitals_become_none():
    frame = {"type": "sensing_update", "classification": {"presence": False},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    ev = normalize_ruview(frame, room="quarto")
    assert ev.presence is False
    assert ev.motion == 0.0
    assert ev.breathing_bpm is None
    assert ev.heart_bpm is None
```

- [ ] **Step 6: Run it to confirm it fails**

Run (from `C:\IA\wavr\backend`): `pytest tests/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.events'`.

- [ ] **Step 7: Implement `backend/wavr/events.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass(frozen=True)
class SensingEvent:
    room: str
    presence: bool
    motion: float
    breathing_bpm: float | None
    heart_bpm: float | None
    ts: str  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def normalize_ruview(raw: dict, room: str) -> SensingEvent:
    """Map a RuView `sensing_update` payload to the canonical SensingEvent.

    `room` is injected by the caller — RuView payloads carry no room label
    (one server senses one space).
    """
    classification = raw.get("classification", {})
    features = raw.get("features", {})
    vitals = raw.get("vital_signs", {})
    ts = raw.get("timestamp")
    return SensingEvent(
        room=room,
        presence=bool(classification.get("presence", False)),
        motion=float(features.get("motion_band_power", 0.0)),
        breathing_bpm=vitals.get("breathing_rate_bpm"),
        heart_bpm=vitals.get("heart_rate_bpm"),
        ts=_iso_from_unix(ts) if ts is not None else datetime.now(timezone.utc).isoformat(),
    )
```

- [ ] **Step 8: Run tests to confirm they pass**

Run: `pytest tests/test_events.py -v`
Expected: 3 passed.

- [ ] **Step 9: Commit**

```powershell
git add .gitignore backend/pyproject.toml backend/wavr backend/tests
git commit -m "feat: canonical SensingEvent + RuView normalizer"
```

---

### Task 2: `SensorSource` interface + `SimulatedSource`

**Files:**
- Create: `backend/wavr/sources/__init__.py` (empty)
- Create: `backend/wavr/sources/base.py`
- Create: `backend/wavr/sources/simulated.py`
- Create: `backend/tests/test_simulated_source.py`

**Interfaces:**
- Consumes: `SensingEvent` from `wavr.events`.
- Produces: `SensorSource` Protocol with `async def events(self) -> AsyncIterator[SensingEvent]`. `SimulatedSource(rooms: Sequence[str] = ("sala","quarto","cozinha"), interval: float = 1.0)` implementing it. It generates events deterministically (no RNG) so tests are stable.

- [ ] **Step 1: Write `backend/wavr/sources/base.py`** (no test — it's a pure interface, exercised via implementations)

```python
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from wavr.events import SensingEvent


@runtime_checkable
class SensorSource(Protocol):
    """A source of canonical sensing events. Implementations: RuViewSource, SimulatedSource."""

    def events(self) -> AsyncIterator[SensingEvent]:
        ...
```

- [ ] **Step 2: Write the failing test** — `backend/tests/test_simulated_source.py`

```python
import pytest

from wavr.events import SensingEvent
from wavr.sources.base import SensorSource
from wavr.sources.simulated import SimulatedSource


async def take(agen, n):
    out = []
    async for x in agen:
        out.append(x)
        if len(out) >= n:
            break
    return out


async def test_simulated_yields_one_event_per_room_deterministically():
    src = SimulatedSource(rooms=["sala", "quarto"], interval=0.0)
    events = await take(src.events(), 2)
    assert [e.room for e in events] == ["sala", "quarto"]
    assert all(isinstance(e, SensingEvent) for e in events)
    # deterministic: same config → same first values
    src2 = SimulatedSource(rooms=["sala", "quarto"], interval=0.0)
    events2 = await take(src2.events(), 2)
    assert [e.to_dict() for e in events] == [e.to_dict() for e in events2] or \
        [(e.room, e.presence, e.motion) for e in events] == [(e.room, e.presence, e.motion) for e in events2]


def test_simulated_source_satisfies_protocol():
    assert isinstance(SimulatedSource(), SensorSource)
```

Note: `ts` uses the wall clock so two runs may differ on `ts`; the test tolerates that by comparing the non-time fields.

- [ ] **Step 3: Run it to confirm it fails**

Run: `pytest tests/test_simulated_source.py -v`
Expected: FAIL — `No module named 'wavr.sources.simulated'`.

- [ ] **Step 4: Implement `backend/wavr/sources/simulated.py`**

```python
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import AsyncIterator, Sequence

from wavr.events import SensingEvent


class SimulatedSource:
    """Generates a plausible fictional-apartment stream. No real data, no RNG."""

    def __init__(self, rooms: Sequence[str] = ("sala", "quarto", "cozinha"), interval: float = 1.0):
        self._rooms = list(rooms)
        self._interval = interval
        self._tick = 0

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            for idx, room in enumerate(self._rooms):
                yield self._make_event(room, idx)
            self._tick += 1
            if self._interval:
                await asyncio.sleep(self._interval)
            else:
                await asyncio.sleep(0)  # yield control without waiting

    def _make_event(self, room: str, idx: int) -> SensingEvent:
        phase = self._tick + idx
        present = (phase % 7) < 4
        motion = round(abs(math.sin(phase / 3.0)) * 10, 3) if present else 0.0
        return SensingEvent(
            room=room,
            presence=present,
            motion=motion,
            breathing_bpm=round(12 + 3 * math.sin(phase / 5.0), 2) if present else None,
            heart_bpm=round(60 + 10 * math.sin(phase / 4.0), 2) if present else None,
            ts=datetime.now(timezone.utc).isoformat(),
        )
```

- [ ] **Step 5: Create empty `backend/wavr/sources/__init__.py`.**

- [ ] **Step 6: Run tests to confirm they pass**

Run: `pytest tests/test_simulated_source.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/sources backend/tests/test_simulated_source.py
git commit -m "feat: SensorSource protocol + deterministic SimulatedSource"
```

---

### Task 3: `RuViewSource` (real WebSocket adapter)

**Files:**
- Create: `backend/wavr/sources/ruview.py`
- Create: `backend/tests/test_ruview_source.py`

**Interfaces:**
- Consumes: `normalize_ruview`, `SensingEvent` from `wavr.events`.
- Produces: module function `parse_message(message: str|bytes, room: str) -> SensingEvent|None` (pure, unit-tested) and class `RuViewSource(url: str, token: str, room: str)` with `async def events(self) -> AsyncIterator[SensingEvent]` (connects the WebSocket, uses `parse_message`).

- [ ] **Step 1: Write the failing test** — `backend/tests/test_ruview_source.py`

```python
import json

import pytest

from wavr.sources.ruview import parse_message
from wavr.sources.base import SensorSource
from wavr.sources.ruview import RuViewSource

SENSING = json.dumps({
    "type": "sensing_update",
    "classification": {"presence": True},
    "features": {"motion_band_power": 3.5},
    "vital_signs": {"breathing_rate_bpm": 14.0, "heart_rate_bpm": 66.0},
    "timestamp": 1782924055.0,
})
NON_SENSING = json.dumps({"type": "heartbeat", "tick": 1})

def test_parse_message_returns_canonical_event_for_sensing_update():
    ev = parse_message(SENSING, room="sala")
    assert ev is not None
    assert ev.room == "sala"
    assert ev.presence is True
    assert ev.motion == 3.5
    assert ev.breathing_bpm == 14.0

def test_parse_message_ignores_non_sensing_frames():
    assert parse_message(NON_SENSING, room="sala") is None

def test_ruview_source_satisfies_protocol():
    assert isinstance(RuViewSource("ws://localhost:3000/ws/sensing", "tok", "sala"), SensorSource)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_ruview_source.py -v`
Expected: FAIL — `No module named 'wavr.sources.ruview'`.

- [ ] **Step 3: Implement `backend/wavr/sources/ruview.py`**

```python
from __future__ import annotations

import json
from typing import AsyncIterator

# Explicit asyncio client (websockets>=13). Using this import instead of the
# top-level `websockets.connect` avoids the legacy client, whose keyword is
# `extra_headers` instead of `additional_headers` — a silent version trap.
from websockets.asyncio.client import connect

from wavr.events import SensingEvent, normalize_ruview


def parse_message(message: str | bytes, room: str) -> SensingEvent | None:
    """Turn one raw WS frame into a canonical event, or None if it isn't a sensing frame."""
    raw = json.loads(message)
    if raw.get("type") != "sensing_update":
        return None
    return normalize_ruview(raw, room)


class RuViewSource:
    """Streams canonical events from a RuView server's WebSocket.

    URL is ws://localhost:3000/ws/sensing (port 3000, not 8765). Auth is sent as a
    Bearer header; if a server rejects it, switch to `f"{url}?token={token}"` and
    drop the header (see the commented fallback below).
    """

    def __init__(self, url: str, token: str, room: str):
        self._url = url
        self._token = token
        self._room = room

    async def events(self) -> AsyncIterator[SensingEvent]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        async with connect(self._url, additional_headers=headers) as ws:
            async for message in ws:
                event = parse_message(message, self._room)
                if event is not None:
                    yield event
        # Fallback if the header is rejected (401/403 during connect):
        #   async with connect(f"{self._url}?token={self._token}") as ws:
        #       ... same loop ...
```

- [ ] **Step 4: Run unit tests to confirm they pass**

Run: `pytest tests/test_ruview_source.py -v`
Expected: 3 passed.

- [ ] **Step 5: Live smoke test (manual — confirms the real URL + auth actually stream)**

Ensure the RuView container is running (`wsl -- docker ps` shows `ruview`). Then run this throwaway script from `C:\IA\wavr\backend` with the venv active (replace `<TOKEN>` with the value in `C:\Users\broku\AppData\Local\Temp\ruview-token.txt`):

```powershell
$env:WAVR_RUVIEW_TOKEN="<TOKEN>"
python -c "import asyncio; from wavr.sources.ruview import RuViewSource; import os
async def main():
    src = RuViewSource('ws://localhost:3000/ws/sensing', os.environ['WAVR_RUVIEW_TOKEN'], 'sala')
    n=0
    async for ev in src.events():
        print(ev.to_dict()); n+=1
        if n>=3: break
asyncio.run(main())"
```
Expected: 3 canonical dicts printed with real `presence`/`motion`/`breathing_bpm` values.
- If it prints events → done.
- If connect fails with 401/403 → apply the `?token=` fallback in `events()` and re-run.
- If connect fails "connection refused" → the WS is not on 3000; re-check `wsl -- docker ps` port mapping and confirm the container is up.

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/sources/ruview.py backend/tests/test_ruview_source.py
git commit -m "feat: RuViewSource WebSocket adapter + parse_message"
```

---

### Task 4: SQLite history storage

**Files:**
- Create: `backend/wavr/storage.py`
- Create: `backend/tests/test_storage.py`

**Interfaces:**
- Consumes: `SensingEvent` from `wavr.events`.
- Produces: class `Storage(path: str = "wavr.db")` with `insert(event: SensingEvent) -> None`, `recent(limit: int = 200) -> list[dict]` (chronological order, each dict is the canonical shape with `presence` as bool), and `close() -> None`.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_storage.py`

```python
from wavr.events import SensingEvent
from wavr.storage import Storage


def make(room, presence, ts):
    return SensingEvent(room=room, presence=presence, motion=1.0,
                        breathing_bpm=13.0, heart_bpm=65.0, ts=ts)

def test_insert_and_recent_returns_chronological_dicts():
    st = Storage(":memory:")
    st.insert(make("sala", True, "2026-07-01T10:00:00+00:00"))
    st.insert(make("quarto", False, "2026-07-01T10:00:01+00:00"))
    rows = st.recent()
    assert [r["room"] for r in rows] == ["sala", "quarto"]
    assert rows[0]["presence"] is True and rows[1]["presence"] is False
    assert set(rows[0].keys()) == {"room", "presence", "motion", "breathing_bpm", "heart_bpm", "ts"}
    st.close()

def test_recent_respects_limit_and_keeps_newest():
    st = Storage(":memory:")
    for i in range(5):
        st.insert(make("sala", True, f"2026-07-01T10:00:0{i}+00:00"))
    rows = st.recent(limit=2)
    assert [r["ts"] for r in rows] == ["2026-07-01T10:00:03+00:00", "2026-07-01T10:00:04+00:00"]
    st.close()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL — `No module named 'wavr.storage'`.

- [ ] **Step 3: Implement `backend/wavr/storage.py`**

```python
from __future__ import annotations

import sqlite3

from wavr.events import SensingEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room          TEXT    NOT NULL,
    presence      INTEGER NOT NULL,
    motion        REAL    NOT NULL,
    breathing_bpm REAL,
    heart_bpm     REAL,
    ts            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
"""


class Storage:
    def __init__(self, path: str = "wavr.db"):
        # check_same_thread=False: the FastAPI pump task and request handlers may
        # touch this from different threads. Fine for a single-user local app.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert(self, event: SensingEvent) -> None:
        self._conn.execute(
            "INSERT INTO events (room, presence, motion, breathing_bpm, heart_bpm, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (event.room, int(event.presence), event.motion,
             event.breathing_bpm, event.heart_bpm, event.ts),
        )
        self._conn.commit()

    def recent(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT room, presence, motion, breathing_bpm, heart_bpm, ts"
            " FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    @staticmethod
    def _to_dict(r: sqlite3.Row) -> dict:
        return {
            "room": r["room"],
            "presence": bool(r["presence"]),
            "motion": r["motion"],
            "breathing_bpm": r["breathing_bpm"],
            "heart_bpm": r["heart_bpm"],
            "ts": r["ts"],
        }

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `pytest tests/test_storage.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/storage.py backend/tests/test_storage.py
git commit -m "feat: SQLite Storage (insert + recent history)"
```

---

### Task 5: In-memory `Hub` (fan-out broadcaster)

**Files:**
- Create: `backend/wavr/hub.py`
- Create: `backend/tests/test_hub.py`

**Interfaces:**
- Produces: class `Hub` with `subscribe() -> asyncio.Queue`, `unsubscribe(q: asyncio.Queue) -> None`, `async publish(item: dict) -> None`. This subscribe/publish pair is ALSO the extension seam for Camadas 2/3 (a future rule engine just subscribes).

- [ ] **Step 1: Write the failing test** — `backend/tests/test_hub.py`

```python
from wavr.hub import Hub


async def test_publish_fans_out_to_all_subscribers():
    hub = Hub()
    a = hub.subscribe()
    b = hub.subscribe()
    await hub.publish({"room": "sala"})
    assert (await a.get())["room"] == "sala"
    assert (await b.get())["room"] == "sala"

async def test_unsubscribe_stops_delivery():
    hub = Hub()
    a = hub.subscribe()
    hub.unsubscribe(a)
    await hub.publish({"room": "quarto"})
    assert a.empty()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_hub.py -v`
Expected: FAIL — `No module named 'wavr.hub'`.

- [ ] **Step 3: Implement `backend/wavr/hub.py`**

```python
from __future__ import annotations

import asyncio


class Hub:
    """Fan-out broadcaster. Each subscriber gets its own queue of published items.

    This is the extension seam: Camada 2/3 (rules, alerts) will just `subscribe()`
    and react to events without touching the ingest/storage path.
    """

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, item: dict) -> None:
        for q in list(self._subscribers):
            await q.put(item)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `pytest tests/test_hub.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/hub.py backend/tests/test_hub.py
git commit -m "feat: Hub fan-out broadcaster (Camada 2/3 seam)"
```

---

### Task 6: Config + FastAPI app (wire source → storage → hub; serve `/ws/live`, `/api/history`, `/api/state`)

**Files:**
- Create: `backend/wavr/config.py`
- Create: `backend/wavr/app.py`
- Create: `backend/tests/test_app.py`
- Create: `backend/.env.example`

**Interfaces:**
- Consumes: `SimulatedSource`, `RuViewSource`, `Storage`, `Hub`.
- Produces: `Config` dataclass + `load_config()` + `make_source(cfg)`; `create_app(source=None, storage=None, hub=None) -> FastAPI` and module-level `app`. Endpoints: `GET /api/history?limit=` → `list[dict]`; `GET /api/state` → `dict[room, latest_event]` (seam for Camada 4 AI); `WS /ws/live` → streams canonical dicts.

- [ ] **Step 1: Write `backend/wavr/config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # reads ./.env (git-ignored) if present, so WAVR_* vars work


@dataclass
class Config:
    source: str          # "simulated" | "ruview"
    ruview_url: str
    ruview_token: str
    room: str
    db_path: str


def load_config() -> Config:
    return Config(
        source=os.getenv("WAVR_SOURCE", "simulated"),
        ruview_url=os.getenv("WAVR_RUVIEW_URL", "ws://localhost:3000/ws/sensing"),
        ruview_token=os.getenv("WAVR_RUVIEW_TOKEN", ""),
        room=os.getenv("WAVR_ROOM", "sala"),
        db_path=os.getenv("WAVR_DB", "wavr.db"),
    )


def make_source(cfg: Config):
    from wavr.sources.simulated import SimulatedSource
    from wavr.sources.ruview import RuViewSource
    if cfg.source == "ruview":
        return RuViewSource(cfg.ruview_url, cfg.ruview_token, cfg.room)
    return SimulatedSource()
```

- [ ] **Step 2: Write `backend/.env.example`**

```dotenv
# Copy to .env and fill in. .env is git-ignored.
# Plano A with real data: set WAVR_SOURCE=ruview and paste the container token.
WAVR_SOURCE=simulated
WAVR_RUVIEW_URL=ws://localhost:3000/ws/sensing
WAVR_RUVIEW_TOKEN=
WAVR_ROOM=sala
WAVR_DB=wavr.db
```

- [ ] **Step 3: Write the failing test** — `backend/tests/test_app.py`

```python
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.sources.simulated import SimulatedSource


def build_client():
    app = create_app(
        source=SimulatedSource(rooms=["sala"], interval=0.01),
        storage=Storage(":memory:"),
        hub=Hub(),
    )
    return TestClient(app)


def test_history_endpoint_returns_list():
    with build_client() as client:
        # let the pump insert a few events
        import time; time.sleep(0.2)
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert body and set(body[0].keys()) == {"room", "presence", "motion", "breathing_bpm", "heart_bpm", "ts"}


def test_ws_live_streams_events():
    with build_client() as client:
        with client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
            assert msg["room"] == "sala"


def test_state_endpoint_returns_latest_per_room():
    with build_client() as client:
        import time; time.sleep(0.2)
        r = client.get("/api/state")
        assert r.status_code == 200
        state = r.json()
        assert "sala" in state
        assert set(state["sala"].keys()) == {"room", "presence", "motion", "breathing_bpm", "heart_bpm", "ts"}
```

- [ ] **Step 4: Run it to confirm it fails**

Run: `pytest tests/test_app.py -v`
Expected: FAIL — `No module named 'wavr.app'`.

- [ ] **Step 5: Implement `backend/wavr/app.py`**

```python
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from wavr.config import load_config, make_source
from wavr.storage import Storage
from wavr.hub import Hub


def create_app(source=None, storage=None, hub=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _source = source or make_source(cfg)
    latest: dict[str, dict] = {}  # room -> last canonical dict (Camada 4 seam)

    async def _pump():
        async for event in _source.events():
            d = event.to_dict()
            _storage.insert(event)
            latest[d["room"]] = d
            await _hub.publish(d)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_pump())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Wavr", lifespan=lifespan)

    @app.get("/api/history")
    async def history(limit: int = 200):
        return _storage.recent(limit)

    @app.get("/api/state")
    async def state():
        return latest

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        await ws.accept()
        q = _hub.subscribe()
        try:
            while True:
                item = await q.get()
                await ws.send_json(item)
        except WebSocketDisconnect:
            pass
        finally:
            _hub.unsubscribe(q)

    return app


app = create_app()
```

- [ ] **Step 6: Run tests to confirm they pass**

Run: `pytest tests/test_app.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run the FULL suite**

Run: `pytest -v`
Expected: all tests from Tasks 1–6 pass (≈ 12).

- [ ] **Step 8: Manual run — Plano A against simulated data**

Run (from `C:\IA\wavr\backend`, venv active):
```powershell
uvicorn wavr.app:app --port 8000
```
Then in a browser open `http://localhost:8000/api/history` — expect a growing JSON list. Ctrl+C to stop.

- [ ] **Step 9: Commit**

```powershell
git add backend/wavr/config.py backend/wavr/app.py backend/tests/test_app.py backend/.env.example
git commit -m "feat: config + FastAPI app (ws/live, api/history, api/state seam)"
```

---

### Task 7: Frontend dashboard (`index.html`) with `DataProvider` seam

**Files:**
- Create: `frontend/index.html` (single file: HTML + CSS + JS, includes both providers)
- Modify: `backend/wavr/app.py` (add `GET /` to serve the dashboard same-origin — kills CORS)

**Interfaces:**
- Consumes: backend `WS /ws/live` and `GET /api/history` (Plano A, served same-origin from the backend at `:8000`); nothing external in Plano B.
- Produces: a `DataProvider` contract `{ start(onEvent), history() }` with `WebSocketProvider` (Plano A) and `SimulatorProvider` (Plano B). Mode auto-selects: `live` on `localhost`, `simulated` otherwise.

- [ ] **Step 1: Create `frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wavr — Live Home Sensing</title>
<style>
  :root {
    --bg:#0f1216; --surface:#171c22; --ink:#e8edf2; --muted:#9aa7b4;
    --accent:#3db54a; --warn:#e0b341; --line:#232a32; --radius:14px;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:20px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; justify-content:space-between; }
  h1 { font-size:1.25rem; margin:0; letter-spacing:-0.01em; }
  .mode { font-size:.8rem; color:var(--muted); }
  .mode b { color:var(--accent); }
  main { padding:24px; max-width:1000px; margin:0 auto; }
  .rooms { display:grid; gap:16px;
           grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }
  .card { background:var(--surface); border:1px solid var(--line);
          border-radius:var(--radius); padding:18px; }
  .card h2 { margin:0 0 10px; font-size:1rem; text-transform:capitalize; }
  .pill { display:inline-block; font-size:.75rem; padding:3px 10px; border-radius:999px; }
  .pill.on { background:rgba(61,181,74,.15); color:var(--accent); }
  .pill.off { background:rgba(154,167,180,.12); color:var(--muted); }
  .metric { display:flex; justify-content:space-between; margin-top:10px;
            font-variant-numeric:tabular-nums; }
  .metric span:first-child { color:var(--muted); }
  .bar { height:6px; border-radius:3px; background:var(--line); margin-top:12px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--accent); width:0; transition:width .3s ease-out; }
  h3 { margin:28px 0 12px; font-size:.9rem; color:var(--muted); font-weight:600; }
  .timeline { background:var(--surface); border:1px solid var(--line);
              border-radius:var(--radius); padding:8px 0; max-height:260px; overflow:auto; }
  .row { display:flex; gap:14px; padding:7px 18px; font-size:.82rem;
         border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
  .row:last-child { border-bottom:0; }
  .row .t { color:var(--muted); min-width:88px; }
  .row .r { text-transform:capitalize; min-width:80px; }
  @media (prefers-reduced-motion:reduce){ .bar>i{ transition:none; } }
</style>
</head>
<body>
<header>
  <h1>Wavr</h1>
  <div class="mode" id="mode"></div>
</header>
<main>
  <div class="rooms" id="rooms"></div>
  <h3>Timeline</h3>
  <div class="timeline" id="timeline"></div>
</main>
<script>
// ---- DataProvider contract: { start(onEvent), history() } ----
function WebSocketProvider() {
  const base = location.origin;
  return {
    async history() {
      try { const r = await fetch(base + "/api/history?limit=100"); return await r.json(); }
      catch { return []; }
    },
    start(onEvent) {
      const url = base.replace(/^http/, "ws") + "/ws/live";
      const ws = new WebSocket(url);
      ws.onmessage = (m) => onEvent(JSON.parse(m.data));
      ws.onclose = () => setTimeout(() => this.start(onEvent), 1500); // auto-reconnect
    },
  };
}

function SimulatorProvider() {
  const rooms = ["sala", "quarto", "cozinha"];
  let tick = 0;
  const make = (room, idx) => {
    const phase = tick + idx;
    const present = (phase % 7) < 4;
    return {
      room, presence: present,
      motion: present ? +(Math.abs(Math.sin(phase/3))*10).toFixed(2) : 0,
      breathing_bpm: present ? +(12 + 3*Math.sin(phase/5)).toFixed(1) : null,
      heart_bpm: present ? +(60 + 10*Math.sin(phase/4)).toFixed(0) : null,
      ts: new Date().toISOString(),
    };
  };
  return {
    async history() {
      const out = [];
      for (let t = 0; t < 12; t++) { tick = t; rooms.forEach((r,i)=>out.push(make(r,i))); }
      tick = 12; return out;
    },
    start(onEvent) {
      setInterval(() => { rooms.forEach((r,i)=>onEvent(make(r,i))); tick++; }, 1500);
    },
  };
}

// ---- Mode selection (PRIVACY: never "live" off localhost) ----
const MODE = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
  ? "live" : "simulated";
const provider = MODE === "live" ? WebSocketProvider() : SimulatorProvider();
document.getElementById("mode").innerHTML =
  MODE === "live" ? "fonte: <b>casa real</b> (local)" : "fonte: <b>demo</b> (dados fictícios)";

// ---- Rendering ----
const roomsEl = document.getElementById("rooms");
const timelineEl = document.getElementById("timeline");
const cards = {};

function upsertRoom(ev) {
  let c = cards[ev.room];
  if (!c) {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<h2>${ev.room}</h2>
      <span class="pill"></span>
      <div class="metric"><span>Respiração</span><b class="br">—</b></div>
      <div class="metric"><span>Batimento</span><b class="hr">—</b></div>
      <div class="bar"><i></i></div>`;
    roomsEl.appendChild(el);
    c = cards[ev.room] = el;
  }
  const pill = c.querySelector(".pill");
  pill.textContent = ev.presence ? "presente" : "vazio";
  pill.className = "pill " + (ev.presence ? "on" : "off");
  c.querySelector(".br").textContent = ev.breathing_bpm != null ? ev.breathing_bpm + " rpm" : "—";
  c.querySelector(".hr").textContent = ev.heart_bpm != null ? ev.heart_bpm + " bpm" : "—";
  c.querySelector(".bar > i").style.width = Math.min(100, ev.motion * 10) + "%";
}

function pushTimeline(ev) {
  const row = document.createElement("div");
  row.className = "row";
  const t = ev.ts.slice(11, 19);
  row.innerHTML = `<span class="t">${t}</span><span class="r">${ev.room}</span>
    <span>${ev.presence ? "presente" : "vazio"}</span>
    <span>${ev.breathing_bpm != null ? ev.breathing_bpm + " rpm" : ""}</span>`;
  timelineEl.prepend(row);
  while (timelineEl.children.length > 60) timelineEl.lastChild.remove();
}

function handle(ev) { upsertRoom(ev); pushTimeline(ev); }

(async () => {
  const hist = await provider.history();
  hist.forEach(handle);
  provider.start(handle);
})();
</script>
</body>
</html>
```

- [ ] **Step 2: Serve the dashboard from the backend (same-origin, no CORS)**

Add this near the top of `backend/wavr/app.py` imports:
```python
from pathlib import Path
from fastapi.responses import FileResponse

_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
```
And add this route inside `create_app` (alongside the other routes, before `return app`):
```python
    @app.get("/")
    async def dashboard():
        return FileResponse(_INDEX)
```
`parents[2]` walks `app.py → wavr → backend → C:\IA\wavr`, then `frontend/index.html`. Serving the page from the backend means the dashboard's `location.origin` fetch/WS calls are same-origin — no CORS, no separate static server.

- [ ] **Step 3: Verify Plano A (live)**

Start the backend from `C:\IA\wavr\backend` (venv active): `uvicorn wavr.app:app --port 8000`. Open `http://localhost:8000/`. Because the host is `localhost`, the page picks **live** mode, fetches `/api/history`, and streams `/ws/live`. Expect room cards updating and a growing timeline. The mode label reads **fonte: casa real (local)**.

- [ ] **Step 4: Verify with Playwright (automated)**

Use the Playwright MCP: navigate to `http://localhost:8000/`, wait for a `.card` to appear, assert at least one room card and one timeline `.row` render and that a `.pill` text updates within a few seconds. Take a screenshot for the record.

- [ ] **Step 5: Verify Plano B (simulated) offline**

Open `frontend/index.html` directly via `file://` (no backend running). The mode label must read **fonte: demo (dados fictícios)** and cards must animate. This proves the public build is self-contained and cannot reach a backend.

- [ ] **Step 6: Commit**

```powershell
git add frontend/index.html backend/wavr/app.py
git commit -m "feat: single-file dashboard (DataProvider seam) + backend serves it same-origin"
```

---

### Task 8: Impeccable polish + Plano B deploy to Cloudflare Pages

**Files:**
- Modify: `frontend/index.html` (polish only)
- Create: `docs/seams.md` (documents the Camada 2/3/4 extension points)

**Interfaces:**
- Consumes: the finished dashboard.
- Produces: a live public URL serving the simulated demo; a seams doc so the next camadas have a written contract.

- [ ] **Step 1: Mandatory design pass (per project rule)**

Run `/impeccable polish frontend/index.html` then `/impeccable audit frontend/index.html`. Fix contrast/spacing/hierarchy findings. This is the required design filter before any client-facing deploy. Re-run the Playwright check from Task 7 Step 3 after changes.

- [ ] **Step 2: Write `docs/seams.md`**

```markdown
# Wavr — Extension seams (Camadas 2/3/4)

Camada 1 leaves these attach points so later camadas add code WITHOUT touching ingest/storage:

- **Camada 2 (rules) & 3 (away-mode):** subscribe to the live stream via `Hub.subscribe()`
  in `backend/wavr/hub.py`. A rule engine consumes the same canonical dicts the dashboard
  gets and emits actions (MQTT publish to `localhost:1883`, already inside the RuView image).
- **Camada 4 (AI narration):** read `GET /api/state` (latest per room) and `GET /api/history`
  in `backend/wavr/app.py`. An endpoint like `POST /api/ask` would pass that context to
  Gemini/Claude. Public plane reuses the isolated `GEMINI_API_KEY_TEMPLATE` via a Cloudflare
  Worker, mirroring the existing `copilot-ask` pattern.
- **Hardware swap:** set `WAVR_SOURCE=ruview` + `WAVR_RUVIEW_TOKEN`. No code change.
```

- [ ] **Step 3: Deploy the Plano B showcase to Cloudflare Pages**

Deploy ONLY the `frontend/` dir (static). It will serve on a `*.pages.dev` host → not localhost → auto-selects **simulated**. Run:
```powershell
cd C:\IA\wavr
npx wrangler pages deploy frontend --project-name wavr
```
(If the `wavr` project doesn't exist yet, wrangler prompts to create it — accept.)
Expected: a deployed URL like `https://wavr.pages.dev`.

- [ ] **Step 4: Verify the public deploy is safe and works**

Open the `*.pages.dev` URL. Confirm:
- Mode label reads **demo (dados fictícios)**.
- Cards animate with simulated data.
- Browser devtools → Network shows NO request to `localhost` / `:8000` (privacy: the public build has no path to any backend).

- [ ] **Step 5: Commit**

```powershell
git add frontend/index.html docs/seams.md
git commit -m "feat: Impeccable polish + Plano B deploy + seams doc"
```

---

## Definition of Done (Camada 1)

Maps to the spec's success criteria (Seção 8):
- [x] Backend connects to RuView WS (`ws://localhost:3000/ws/sensing`) and receives events — Task 3 Step 5.
- [x] Events normalized to canonical shape + stored in SQLite — Tasks 1, 4, 6.
- [x] Dashboard shows real-time presence + motion + vitals per room — Task 7.
- [x] Timeline of recent history — Task 7.
- [x] Swap `RuViewSource` → `SimulatedSource` via config (`WAVR_SOURCE`), same dashboard — Tasks 2, 6.
- [x] Same HTML deploys to Cloudflare Pages running the Simulator (Plano B) — Task 8.
- [x] No real data can leave the LAN (frontend auto-simulates off-localhost; backend never exposed) — Global Constraints + Task 7 Step 4 + Task 8 Step 4.

## Out of scope (documented seams only)
Camada 2 (rules/MQTT), Camada 3 (away-mode/alerts), Camada 4 (AI narration), ESP32 hardware, Home Assistant integration, dashboard auth, RuView pose model (`--load-rvf`). See `docs/seams.md`.
