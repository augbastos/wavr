# Wavr Sub-plan A — Fusion Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
>
> **This supersedes** `2026-07-01-wavr-camada-1.md` (the pre-fusion plan). Sub-plans B (real sources) and C (camera + CV) come after this one; all three share the Rev 2 spec.

**Goal:** Build the fusion core end-to-end on simulated multi-modal data — many modalities → a `FusionEngine` → a confidence-scored `RoomState` per room → a live dashboard that shows the fused state *and why* — and deploy the public showcase (Plano B). No hardware, no camera, no GPU needed for this sub-plan.

**Architecture:** A `SimulatedSource` emits canonical events tagged with a `modality` for several `(room, modality)` sensors. A `FusionEngine` keeps the latest event per `(room, modality)` and computes a `RoomState` (occupied + confidence + per-modality breakdown + explanation). The FastAPI backend stores each `RoomState`, fans it out over a WebSocket, and serves the dashboard same-origin. The same single-file dashboard runs against the backend (Plano A) or an in-browser multi-modal simulator (Plano B).

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, python-dotenv, pytest + pytest-asyncio + httpx; vanilla HTML/CSS/JS; Cloudflare Pages.

## Global Constraints

- **Platform:** Windows 11, PowerShell. Venv at `C:\IA\wavr\.venv`. All shell commands are PowerShell.
- **Python:** 3.11+.
- **Canonical event shape — EXACT:** `{"room": str, "modality": str, "presence": bool, "motion": float, "breathing_bpm": float|None, "heart_bpm": float|None, "confidence": float, "ts": str}`; `ts` = ISO-8601 UTC with `+00:00` offset. `modality` ∈ `{"wifi_csi","network","camera","sim"}` (B/C add real ones).
- **RoomState shape — EXACT:** `{"room": str, "occupied": bool, "confidence": float, "vitals": dict, "sources": list[dict], "explanation": str, "ts": str}`.
- **Privacy:** the frontend auto-selects the Simulator whenever `location.hostname` is not `localhost`/`127.0.0.1` (fail-safe). The backend binds loopback only and uses a Host-header allowlist (defense-in-depth vs DNS-rebinding). No real data leaves the LAN. (This sub-plan uses only simulated data, but the guards ship now.)
- **TDD discipline:** Every code task follows: write failing test → run it, watch it fail *for the right reason* (proves the test is wired up before you make it pass) → minimal implementation → run, watch it pass → commit. Files < 500 lines. DRY, YAGNI.
- **Control plane:** the system has a global on/off and a per-source on/off at runtime, via a `SourceManager` (one async task per enabled source). Global off = no tasks running (~zero footprint). Disabling a source cancels its task (in Sub-plan C, that closes the camera RTSP so no CV runs). Endpoints: `GET /api/system`, `POST /api/system/toggle`, `POST /api/sources/{name}/toggle`.
- **Out of scope here:** real sources (Sub-plan B), camera/CV/toggle (Sub-plan C), rules/away/AI (Camadas 2-4).

**Repo root for all paths:** `C:\IA\wavr\` (already a git repo; commits from earlier design work exist).

---

### Task 1: Scaffold + setup + canonical `SensingEvent`

**Files:**
- Create: `backend/pyproject.toml`, `backend/wavr/__init__.py`, `backend/tests/__init__.py`, `.gitignore`
- Create: `backend/wavr/events.py`
- Create: `backend/tests/test_events.py`

**Interfaces:**
- Produces: `SensingEvent` frozen dataclass (fields per Global Constraints) with `to_dict() -> dict`; `normalize_ruview(raw: dict, room: str) -> SensingEvent` (sets `modality="wifi_csi"`).

- [ ] **Step 1: `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
*.db
.env
.pytest_cache/
node_modules/
*.pt
```

- [ ] **Step 2: `backend/pyproject.toml`**

```toml
[project]
name = "wavr"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
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

- [ ] **Step 3: Create the two empty `__init__.py` files** (`backend/wavr/__init__.py`, `backend/tests/__init__.py`).

- [ ] **Step 4: One-time environment setup**

Run from `C:\IA\wavr`:
```powershell
python --version        # must be 3.11+; if not, install 3.11+ or use: py -3.11 -m venv .venv
# If script execution is blocked, allow it once for your user:
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
cd backend
pip install -e ".[dev]"            # run from inside backend/ so the extras path is unambiguous
# git repo already exists; if starting fresh (from repo root): git init + git config user.name/user.email
```
Expected: `Successfully installed wavr-0.1.0 fastapi ... pytest ...`. All later commands assume the venv is active and you run from `C:\IA\wavr\backend`.

- [ ] **Step 5: Write the failing test** — `backend/tests/test_events.py`

```python
from wavr.events import SensingEvent, normalize_ruview

RUVIEW_FRAME = {
    "type": "sensing_update",
    "classification": {"presence": True, "confidence": 0.43},
    "features": {"motion_band_power": 9.7758},
    "vital_signs": {"breathing_rate_bpm": 9.707, "heart_rate_bpm": 46.22},
    "timestamp": 1782924055.636,
}

def test_normalize_sets_wifi_csi_modality_and_maps_fields():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert ev.room == "sala"
    assert ev.modality == "wifi_csi"
    assert ev.presence is True
    assert ev.motion == 9.7758
    assert ev.breathing_bpm == 9.707
    assert ev.heart_bpm == 46.22
    assert ev.confidence == 0.43
    assert ev.ts.startswith("2026-") and ev.ts.endswith("+00:00")

def test_to_dict_has_exact_canonical_keys():
    ev = normalize_ruview(RUVIEW_FRAME, room="sala")
    assert set(ev.to_dict().keys()) == {
        "room", "modality", "presence", "motion",
        "breathing_bpm", "heart_bpm", "confidence", "ts",
    }

def test_missing_vitals_and_confidence_default():
    frame = {"type": "sensing_update", "classification": {"presence": False},
             "features": {}, "vital_signs": {}, "timestamp": 1782924055.0}
    ev = normalize_ruview(frame, room="quarto")
    assert ev.presence is False and ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.confidence == 0.0
```

- [ ] **Step 6: Run it — expect FAIL** (`ModuleNotFoundError: No module named 'wavr.events'`).
Run from `C:\IA\wavr\backend`: `pytest tests/test_events.py -v`

- [ ] **Step 7: Implement `backend/wavr/events.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass(frozen=True)
class SensingEvent:
    room: str
    modality: str            # "wifi_csi" | "network" | "camera" | "sim"
    presence: bool
    motion: float
    breathing_bpm: float | None
    heart_bpm: float | None
    confidence: float        # the modality's own confidence 0..1
    ts: str                  # ISO-8601 UTC (+00:00)

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _f(v):
    return None if v is None else float(v)


def normalize_ruview(raw: dict, room: str) -> SensingEvent:
    classification = raw.get("classification", {})
    features = raw.get("features", {})
    vitals = raw.get("vital_signs", {})
    ts = raw.get("timestamp")
    return SensingEvent(
        room=room,
        modality="wifi_csi",
        presence=bool(classification.get("presence", False)),
        motion=float(features.get("motion_band_power", 0.0)),
        breathing_bpm=_f(vitals.get("breathing_rate_bpm")),
        heart_bpm=_f(vitals.get("heart_rate_bpm")),
        confidence=float(classification.get("confidence", 0.0)),
        ts=_iso_from_unix(ts) if ts is not None else datetime.now(timezone.utc).isoformat(),
    )
```

- [ ] **Step 8: Run — expect 3 passed.** `pytest tests/test_events.py -v`

- [ ] **Step 9: Commit**

```powershell
git add .gitignore backend/pyproject.toml backend/wavr backend/tests
git commit -m "feat: canonical SensingEvent (+modality,+confidence) + RuView normalizer"
```

---

### Task 2: `RoomState` model

**Files:**
- Create: `backend/wavr/roomstate.py`
- Create: `backend/tests/test_roomstate.py`

**Interfaces:**
- Produces: `RoomState` frozen dataclass (fields per Global Constraints) with `to_dict() -> dict`.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_roomstate.py`

```python
from wavr.roomstate import RoomState

def test_roomstate_to_dict_has_exact_keys():
    rs = RoomState(room="quarto", occupied=True, confidence=0.72,
                   vitals={"breathing_bpm": 14.2, "heart_bpm": 68.0},
                   sources=[{"modality": "wifi_csi", "presence": True, "confidence": 0.61}],
                   explanation="wifi: respiração → 72% ocupado",
                   ts="2026-07-01T16:20:01+00:00")
    d = rs.to_dict()
    assert set(d.keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}
    assert d["occupied"] is True and d["confidence"] == 0.72
```

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_roomstate.py -v`

- [ ] **Step 3: Implement `backend/wavr/roomstate.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class RoomState:
    room: str
    occupied: bool
    confidence: float
    vitals: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    explanation: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
```

- [ ] **Step 4: Run — expect 1 passed.** `pytest tests/test_roomstate.py -v`

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/roomstate.py backend/tests/test_roomstate.py
git commit -m "feat: RoomState fused-output model"
```

---

### Task 3: `SensorSource` protocol + multi-modal `SimulatedSource`

**Files:**
- Create: `backend/wavr/sources/__init__.py` (empty), `backend/wavr/sources/base.py`, `backend/wavr/sources/simulated.py`
- Create: `backend/tests/test_simulated_source.py`

**Interfaces:**
- Consumes: `SensingEvent`.
- Produces: `SensorSource` Protocol (`events() -> AsyncIterator[SensingEvent]`). `SimulatedSource(interval=1.0)` that yields events for a fixed set of `(room, modality)` sensors deterministically (no RNG).

- [ ] **Step 1: Create `backend/wavr/sources/__init__.py` (empty) and `backend/wavr/sources/base.py`**

```python
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from wavr.events import SensingEvent


@runtime_checkable
class SensorSource(Protocol):
    """A source of canonical sensing events. Each implementation emits one modality
    (or, for the simulator, several) tagged on every SensingEvent."""

    def events(self) -> AsyncIterator[SensingEvent]:
        ...
```

- [ ] **Step 2: Write the failing test** — `backend/tests/test_simulated_source.py`

```python
from wavr.events import SensingEvent
from wavr.sources.base import SensorSource
from wavr.sources.simulated import SimulatedSource, SENSORS


async def take(agen, n):
    out = []
    async for x in agen:
        out.append(x)
        if len(out) >= n:
            break
    return out


async def test_simulated_emits_one_event_per_sensor_with_modalities():
    src = SimulatedSource(interval=0.0)
    events = await take(src.events(), len(SENSORS))
    assert [(e.room, e.modality) for e in events] == list(SENSORS)
    assert all(isinstance(e, SensingEvent) for e in events)
    # at least two distinct modalities so the FusionEngine has something to fuse
    assert len({e.modality for e in events}) >= 2


async def test_simulated_is_deterministic_on_non_time_fields():
    a = await take(SimulatedSource(interval=0.0).events(), len(SENSORS))
    b = await take(SimulatedSource(interval=0.0).events(), len(SENSORS))
    key = lambda e: (e.room, e.modality, e.presence, e.motion, e.confidence)
    assert [key(e) for e in a] == [key(e) for e in b]


def test_simulated_source_satisfies_protocol():
    assert isinstance(SimulatedSource(), SensorSource)
```

- [ ] **Step 3: Run — expect FAIL** (`No module named 'wavr.sources.simulated'`). `pytest tests/test_simulated_source.py -v`

- [ ] **Step 4: Implement `backend/wavr/sources/simulated.py`**

```python
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import AsyncIterator

from wavr.events import SensingEvent

# Fixed fictional apartment: which modality "watches" which room.
# "casa" = house-level presence (network); rooms get wifi_csi and/or camera.
SENSORS = [
    ("casa", "network"),
    ("sala", "wifi_csi"),
    ("quarto", "wifi_csi"),
    ("quarto", "camera"),
    ("quintal", "camera"),
]


class SimulatedSource:
    """Emits a plausible multi-modal fictional stream. No real data, no RNG."""

    def __init__(self, interval: float = 1.0):
        self._interval = interval
        self._tick = 0

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            for idx, (room, modality) in enumerate(SENSORS):
                yield self._make(room, modality, idx)
            self._tick += 1
            await asyncio.sleep(self._interval if self._interval else 0)

    def _make(self, room: str, modality: str, idx: int) -> SensingEvent:
        phase = self._tick + idx
        present = (phase % 7) < 4
        gives_vitals = modality == "wifi_csi"
        # camera is high-confidence, network low, wifi mid
        conf = {"camera": 0.95, "wifi_csi": 0.9, "network": 0.6, "sim": 0.6}.get(modality, 0.5)
        return SensingEvent(
            room=room,
            modality=modality,
            presence=present,
            motion=round(abs(math.sin(phase / 3.0)) * 10, 3) if present else 0.0,
            breathing_bpm=round(12 + 3 * math.sin(phase / 5.0), 2) if (present and gives_vitals) else None,
            heart_bpm=round(60 + 10 * math.sin(phase / 4.0), 2) if (present and gives_vitals) else None,
            confidence=conf if present else round(conf * 0.3, 3),
            ts=datetime.now(timezone.utc).isoformat(),
        )
```

- [ ] **Step 5: Run — expect 3 passed.** `pytest tests/test_simulated_source.py -v`

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/sources backend/tests/test_simulated_source.py
git commit -m "feat: SensorSource protocol + multi-modal SimulatedSource"
```

---

### Task 4: `FusionEngine` (the core)

**Files:**
- Create: `backend/wavr/fusion.py`
- Create: `backend/tests/test_fusion.py`

**Interfaces:**
- Consumes: `SensingEvent`, `RoomState`.
- Produces: `FusionEngine(weights: dict[str,float] | None = None, threshold: float = 0.5)` with `update(event: SensingEvent) -> RoomState` (records the event, returns the recomputed state for its room) and `state(room: str) -> RoomState | None`.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_fusion.py`

```python
from wavr.events import SensingEvent
from wavr.fusion import FusionEngine


def ev(room, modality, presence, conf, br=None, hr=None):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=br, heart_bpm=hr, confidence=conf,
                        ts="2026-07-01T10:00:00+00:00")


def test_single_present_modality_makes_room_occupied():
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", True, 0.9))   # strength = 0.85 * 0.9 = 0.765
    assert rs.room == "sala"
    assert rs.occupied is True
    assert 0.0 < rs.confidence <= 1.0
    assert rs.sources[0]["modality"] == "wifi_csi"


def test_high_weight_camera_overrides_low_weight_network():
    f = FusionEngine(weights={"camera": 1.0, "network": 0.3})
    f.update(ev("quarto", "network", False, 0.5))
    rs = f.update(ev("quarto", "camera", True, 0.95))
    assert rs.occupied is True          # camera (present, heavy) beats network (absent, light)
    assert len(rs.sources) == 2


def test_vitals_surface_from_wifi_csi():
    f = FusionEngine()
    rs = f.update(ev("quarto", "wifi_csi", True, 0.9, br=14.0, hr=66.0))
    assert rs.vitals == {"breathing_bpm": 14.0, "heart_bpm": 66.0}


def test_all_absent_makes_room_empty_with_zero_confidence():
    f = FusionEngine()
    rs = f.update(ev("sala", "wifi_csi", False, 0.4))
    assert rs.occupied is False
    assert rs.confidence == 0.0


def test_explanation_lists_modalities():
    f = FusionEngine()
    f.update(ev("quarto", "network", False, 0.4))
    rs = f.update(ev("quarto", "camera", True, 0.9))
    assert "network" in rs.explanation and "camera" in rs.explanation


def test_weak_lone_source_scores_below_strong_lone_source():
    # A lone coarse source (network) must not report the same confidence as a
    # lone precise source (camera) — the old num/den made both 100%.
    f = FusionEngine()
    net = f.update(ev("casa", "network", True, 0.6))    # strength 0.5 * 0.6 = 0.30
    cam = f.update(ev("quintal", "camera", True, 0.9))  # strength 1.0 * 0.9 = 0.90
    assert net.confidence < cam.confidence
    assert cam.confidence > 0.5
```

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_fusion.py -v`

- [ ] **Step 3: Implement `backend/wavr/fusion.py`**

```python
from __future__ import annotations

from wavr.events import SensingEvent
from wavr.roomstate import RoomState

# Default trust weights per modality. Camera (video) is most precise; network
# (device presence) is house-level and coarse. Tunable via config later.
DEFAULT_WEIGHTS = {"camera": 1.0, "wifi_csi": 0.85, "network": 0.5, "sim": 0.6}


class FusionEngine:
    """Explainable fusion. Per room, confidence = agreement × strength, where
    `agreement` is the fraction of trusted mass saying "present" and `strength`
    is the best present evidence (weight × the source's own confidence). This stops
    a lone weak source (e.g. coarse network) from ever reporting 100%, and lets a
    trusted source dominate when modalities disagree."""

    def __init__(self, weights: dict | None = None, threshold: float = 0.5):
        self._weights = weights or DEFAULT_WEIGHTS
        self._threshold = threshold
        self._latest: dict[str, dict[str, SensingEvent]] = {}  # room -> modality -> event

    def update(self, event: SensingEvent) -> RoomState:
        self._latest.setdefault(event.room, {})[event.modality] = event
        return self._fuse(event.room, event.ts)

    def state(self, room: str) -> RoomState | None:
        if room not in self._latest:
            return None
        last_ts = max(e.ts for e in self._latest[room].values())
        return self._fuse(room, last_ts)

    def _fuse(self, room: str, ts: str) -> RoomState:
        events = self._latest[room]
        num = 0.0        # weighted mass saying "present"
        den = 0.0        # total weighted mass
        strength = 0.0   # best present evidence (weight × confidence)
        sources = []
        vitals: dict = {}
        for modality, e in events.items():
            mass = self._weights.get(modality, 0.5) * e.confidence
            den += mass
            if e.presence:
                num += mass
                strength = max(strength, mass)
            sources.append({"modality": modality, "presence": e.presence,
                            "confidence": round(e.confidence, 3)})
            if e.presence and e.breathing_bpm is not None:
                vitals = {"breathing_bpm": e.breathing_bpm, "heart_bpm": e.heart_bpm}
        agreement = num / den if den > 0 else 0.0
        confidence = round(agreement * strength, 3)
        occupied = confidence >= self._threshold
        parts = [f"{s['modality']}: {'presente' if s['presence'] else 'vazio'}" for s in sources]
        explanation = " · ".join(parts) + f" → {int(confidence * 100)}% ocupado"
        return RoomState(room=room, occupied=occupied, confidence=confidence,
                         vitals=vitals, sources=sources, explanation=explanation, ts=ts)
```

- [ ] **Step 4: Run — expect 6 passed.** `pytest tests/test_fusion.py -v`

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/fusion.py backend/tests/test_fusion.py
git commit -m "feat: FusionEngine — weighted, explainable RoomState per room"
```

---

### Task 5: SQLite storage (RoomState history)

**Files:**
- Create: `backend/wavr/storage.py`, `backend/tests/test_storage.py`

**Interfaces:**
- Consumes: `RoomState`.
- Produces: `Storage(path=":memory:" | "wavr.db")` with `insert_state(rs: RoomState) -> None`, `recent(limit=200) -> list[dict]` (chronological RoomState dicts), `close()`. Stores ONLY derived RoomState — never raw frames/CSI.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_storage.py`

```python
from wavr.roomstate import RoomState
from wavr.storage import Storage


def rs(room, occupied, ts):
    return RoomState(room=room, occupied=occupied, confidence=0.8,
                     vitals={"breathing_bpm": 13.0, "heart_bpm": 65.0},
                     sources=[{"modality": "wifi_csi", "presence": occupied, "confidence": 0.7}],
                     explanation="x", ts=ts)


def test_insert_and_recent_roundtrips_chronologically():
    st = Storage(":memory:")
    st.insert_state(rs("sala", True, "2026-07-01T10:00:00+00:00"))
    st.insert_state(rs("quarto", False, "2026-07-01T10:00:01+00:00"))
    rows = st.recent()
    assert [r["room"] for r in rows] == ["sala", "quarto"]
    assert rows[0]["occupied"] is True and rows[1]["occupied"] is False
    assert rows[0]["sources"][0]["modality"] == "wifi_csi"   # JSON columns round-trip
    assert set(rows[0].keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}
    st.close()


def test_recent_limit_keeps_newest():
    st = Storage(":memory:")
    for i in range(5):
        st.insert_state(rs("sala", True, f"2026-07-01T10:00:0{i}+00:00"))
    rows = st.recent(limit=2)
    assert [r["ts"] for r in rows] == ["2026-07-01T10:00:03+00:00", "2026-07-01T10:00:04+00:00"]
    st.close()
```

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_storage.py -v`

- [ ] **Step 3: Implement `backend/wavr/storage.py`**

```python
from __future__ import annotations

import json
import sqlite3

from wavr.roomstate import RoomState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_states (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT    NOT NULL,
    occupied    INTEGER NOT NULL,
    confidence  REAL    NOT NULL,
    vitals      TEXT    NOT NULL,   -- JSON
    sources     TEXT    NOT NULL,   -- JSON
    explanation TEXT    NOT NULL,
    ts          TEXT    NOT NULL
);
"""


class Storage:
    """Persists ONLY derived RoomState. Never stores raw frames or CSI."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert_state(self, rs: RoomState) -> None:
        self._conn.execute(
            "INSERT INTO room_states (room, occupied, confidence, vitals, sources, explanation, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rs.room, int(rs.occupied), rs.confidence, json.dumps(rs.vitals),
             json.dumps(rs.sources), rs.explanation, rs.ts),
        )
        self._conn.commit()

    def recent(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT room, occupied, confidence, vitals, sources, explanation, ts"
            " FROM room_states ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    @staticmethod
    def _to_dict(r: sqlite3.Row) -> dict:
        return {
            "room": r["room"],
            "occupied": bool(r["occupied"]),
            "confidence": r["confidence"],
            "vitals": json.loads(r["vitals"]),
            "sources": json.loads(r["sources"]),
            "explanation": r["explanation"],
            "ts": r["ts"],
        }

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run — expect 2 passed.** `pytest tests/test_storage.py -v`

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/storage.py backend/tests/test_storage.py
git commit -m "feat: SQLite Storage for RoomState history (derived only)"
```

---

### Task 6: In-memory `Hub` (fan-out)

**Files:**
- Create: `backend/wavr/hub.py`, `backend/tests/test_hub.py`

**Interfaces:**
- Produces: `Hub` with `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `async publish(item: dict)`. Extension seam for Camadas 2/3.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_hub.py`

```python
from wavr.hub import Hub

async def test_publish_fans_out_to_all_subscribers():
    hub = Hub()
    a, b = hub.subscribe(), hub.subscribe()
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

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_hub.py -v`

- [ ] **Step 3: Implement `backend/wavr/hub.py`**

```python
from __future__ import annotations

import asyncio


class Hub:
    """Fan-out broadcaster. Extension seam: Camada 2/3 just subscribe() and react."""

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

- [ ] **Step 4: Run — expect 2 passed.** `pytest tests/test_hub.py -v`

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/hub.py backend/tests/test_hub.py
git commit -m "feat: Hub fan-out broadcaster (Camada 2/3 seam)"
```

---

### Task 7: Config

**Files:**
- Create: `backend/wavr/config.py`, `backend/.env.example`, `backend/tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass + `load_config() -> Config` (reads `.env` via dotenv). Fields: `db_path`, `sim_interval`, `fusion_threshold`. (Real-source and camera config land in Sub-plans B/C.)

- [ ] **Step 1: `backend/.env.example`**

```dotenv
# Copy to .env (git-ignored). Sub-plan A runs fully on simulated data — no secrets needed.
WAVR_DB=wavr.db
WAVR_SIM_INTERVAL=1.0
WAVR_FUSION_THRESHOLD=0.5
```

- [ ] **Step 2: Write the failing test** — `backend/tests/test_config.py`

```python
from wavr.config import load_config

def test_defaults_load_without_env():
    cfg = load_config()
    assert cfg.db_path == "wavr.db"
    assert cfg.sim_interval == 1.0
    assert cfg.fusion_threshold == 0.5
```

- [ ] **Step 3: Run — expect FAIL.** `pytest tests/test_config.py -v`

- [ ] **Step 4: Implement `backend/wavr/config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # reads ./.env (git-ignored) if present


@dataclass
class Config:
    db_path: str
    sim_interval: float
    fusion_threshold: float


def load_config() -> Config:
    return Config(
        db_path=os.getenv("WAVR_DB", "wavr.db"),
        sim_interval=float(os.getenv("WAVR_SIM_INTERVAL", "1.0")),
        fusion_threshold=float(os.getenv("WAVR_FUSION_THRESHOLD", "0.5")),
    )
```

- [ ] **Step 5: Run — expect 1 passed.** `pytest tests/test_config.py -v`

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/config.py backend/.env.example backend/tests/test_config.py
git commit -m "feat: Config (dotenv) for db, sim interval, fusion threshold"
```

---

### Task 8: `SourceManager` (runtime on/off control plane)

**Files:**
- Create: `backend/wavr/sourcemanager.py`, `backend/tests/test_sourcemanager.py`

**Interfaces:**
- Consumes: any `SensorSource` factory + an `on_event(event)` coroutine.
- Produces: `SourceManager(on_event)` with `register(name, factory, enabled=True)`, `async start()`, `async stop()`, `async set_enabled(name, enabled)`, `async set_running(running)`, `status() -> {"running": bool, "sources": [{"name","enabled","active"}]}`. Runs one asyncio task per enabled source; each task pulls `events()` and calls `on_event`. Disabling cancels the task (Sub-plan C: closes camera RTSP → no CV).

- [ ] **Step 1: Write the failing test** — `backend/tests/test_sourcemanager.py`

```python
import asyncio

from wavr.events import SensingEvent
from wavr.sourcemanager import SourceManager


class FakeSource:
    def __init__(self, room):
        self.room = room

    async def events(self):
        while True:
            yield SensingEvent(self.room, "sim", True, 1.0, None, None, 0.5,
                               "2026-07-01T10:00:00+00:00")
            await asyncio.sleep(0.001)


async def test_start_runs_enabled_sources_and_feeds_on_event():
    got = []
    m = SourceManager(lambda e: got.append(e) or asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"), enabled=True)
    await m.start()
    await asyncio.sleep(0.05)
    await m.stop()
    assert got and got[0].room == "sala"
    assert m.status()["running"] is False


async def test_disable_source_stops_its_task():
    m = SourceManager(lambda e: asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"))
    await m.start()
    await m.set_enabled("a", False)
    src = [s for s in m.status()["sources"] if s["name"] == "a"][0]
    assert src["enabled"] is False and src["active"] is False
    await m.stop()


async def test_global_stop_zeroes_active_tasks():
    m = SourceManager(lambda e: asyncio.sleep(0))
    m.register("a", lambda: FakeSource("sala"))
    m.register("b", lambda: FakeSource("quarto"))
    await m.start()
    assert all(s["active"] for s in m.status()["sources"])
    await m.set_running(False)
    assert not any(s["active"] for s in m.status()["sources"])
```

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_sourcemanager.py -v`

- [ ] **Step 3: Implement `backend/wavr/sourcemanager.py`**

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable


class SourceManager:
    """Runs one async task per ENABLED source, each feeding on_event. Global on/off
    (start/stop) + per-source on/off at runtime. Heavy sources (camera CV) only
    consume resources while enabled — disabling cancels the task."""

    def __init__(self, on_event: Callable[[object], Awaitable]):
        self._on_event = on_event
        self._factories: dict[str, Callable[[], object]] = {}
        self._enabled: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def register(self, name: str, factory: Callable[[], object], enabled: bool = True) -> None:
        self._factories[name] = factory
        self._enabled[name] = enabled

    async def start(self) -> None:
        self._running = True
        for name, en in self._enabled.items():
            if en:
                self._spawn(name)

    async def stop(self) -> None:
        self._running = False
        for name in list(self._tasks):
            await self._kill(name)

    async def set_running(self, running: bool) -> None:
        await (self.start() if running else self.stop())

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._factories:
            raise KeyError(name)
        self._enabled[name] = enabled
        if enabled and self._running:
            self._spawn(name)
        elif not enabled:
            await self._kill(name)

    def status(self) -> dict:
        return {
            "running": self._running,
            "sources": [
                {"name": n, "enabled": self._enabled[n], "active": n in self._tasks}
                for n in self._factories
            ],
        }

    def _spawn(self, name: str) -> None:
        if name not in self._tasks:
            self._tasks[name] = asyncio.create_task(self._run(name))

    async def _kill(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
            # wait_for guards against a source whose teardown blocks (e.g. a stalled
            # camera read) so a disable/stop can't hang the control plane.
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)

    async def _run(self, name: str) -> None:
        agen = self._factories[name]().events()
        try:
            async for ev in agen:
                await self._on_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("source %s crashed", name)
        finally:
            # Deterministic teardown: runs the source generator's cleanup (e.g. a
            # CameraSource releasing its RTSP stream) the moment the task is cancelled.
            with contextlib.suppress(Exception):
                await agen.aclose()
```

- [ ] **Step 4: Run — expect 3 passed.** `pytest tests/test_sourcemanager.py -v`

- [ ] **Step 5: Commit**

```powershell
git add backend/wavr/sourcemanager.py backend/tests/test_sourcemanager.py
git commit -m "feat: SourceManager — global + per-source runtime on/off control plane"
```

---

### Task 9: FastAPI app (wire sources → fusion → storage → hub)

**Files:**
- Create: `backend/wavr/app.py`, `backend/tests/test_app.py`

**Interfaces:**
- Consumes: `SourceManager`, `SimulatedSource`, `FusionEngine`, `Storage`, `Hub`, `Config`.
- Produces: `create_app(sources=None, storage=None, hub=None, fusion=None) -> FastAPI` + module `app`, where `sources` is a list of `(name, factory, enabled)` (default: one enabled `sim` source). Endpoints: `GET /api/history?limit=`; `GET /api/state`; `WS /ws/live`; `GET /api/system` → `SourceManager.status()`; `POST /api/system/toggle` (body `{"on": bool}`); `POST /api/sources/{name}/toggle` (body `{"enabled": bool}`). `GET /` (serving the dashboard) is added in Task 10.

- [ ] **Step 1: Write the failing test** — `backend/tests/test_app.py`

```python
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sources.simulated import SimulatedSource


def build_client():
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=0.01), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
    )
    return TestClient(app)


def test_history_returns_roomstate_list():
    with build_client() as client:
        import time; time.sleep(0.5)  # a rare empty result on a loaded box just means: re-run
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body
        assert set(body[0].keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}


def test_ws_live_streams_roomstate():
    with build_client() as client:
        with client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
            assert "occupied" in msg and "explanation" in msg


def test_state_returns_latest_per_room():
    with build_client() as client:
        import time; time.sleep(0.5)
        r = client.get("/api/state")
        assert r.status_code == 200
        state = r.json()
        assert state  # at least one room
        any_room = next(iter(state.values()))
        assert set(any_room.keys()) == {"room", "occupied", "confidence", "vitals", "sources", "explanation", "ts"}


LOCAL = {"X-Wavr-Local": "1"}  # state-changing routes require this header (CSRF guard)


def test_system_toggle_off_then_on():
    with build_client() as client:
        assert client.get("/api/system").json()["running"] is True
        client.post("/api/system/toggle", json={"on": False}, headers=LOCAL)
        assert client.get("/api/system").json()["running"] is False
        client.post("/api/system/toggle", json={"on": True}, headers=LOCAL)
        assert client.get("/api/system").json()["running"] is True


def test_source_toggle_disables_named_source():
    with build_client() as client:
        client.post("/api/sources/sim/toggle", json={"enabled": False}, headers=LOCAL)
        sim = [s for s in client.get("/api/system").json()["sources"] if s["name"] == "sim"][0]
        assert sim["enabled"] is False


def test_unknown_source_returns_404():
    with build_client() as client:
        r = client.post("/api/sources/nope/toggle", json={"enabled": False}, headers=LOCAL)
        assert r.status_code == 404


def test_state_change_without_local_header_is_rejected():
    with build_client() as client:
        r = client.post("/api/system/toggle", json={"on": False})  # no X-Wavr-Local
        assert r.status_code == 403
```

- [ ] **Step 2: Run — expect FAIL.** `pytest tests/test_app.py -v`

- [ ] **Step 3: Implement `backend/wavr/app.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from wavr.config import load_config
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.sourcemanager import SourceManager
from wavr.sources.simulated import SimulatedSource


def create_app(sources=None, storage=None, hub=None, fusion=None) -> FastAPI:
    cfg = load_config()
    _hub = hub or Hub()
    _storage = storage or Storage(cfg.db_path)
    _fusion = fusion or FusionEngine(threshold=cfg.fusion_threshold)
    latest: dict[str, dict] = {}  # room -> last RoomState dict (Camada 4 seam)

    async def _ingest(event):
        rs = _fusion.update(event)
        d = rs.to_dict()
        _storage.insert_state(rs)
        latest[d["room"]] = d
        await _hub.publish(d)

    manager = SourceManager(_ingest)
    for name, factory, enabled in (sources or [("sim", lambda: SimulatedSource(interval=cfg.sim_interval), True)]):
        manager.register(name, factory, enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="Wavr", lifespan=lifespan)

    # PRIVACY: reject any request whose peer isn't loopback. Enforced in code so it
    # holds even if someone runs uvicorn with --host 0.0.0.0. ("testclient" is the
    # pytest TestClient peer.) This is the load-bearing control; the Host allowlist
    # is extra defense against DNS-rebinding.
    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        host = request.client.host if request.client else None
        if host not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse({"detail": "loopback only"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
    )

    def require_local(request: Request):
        # CSRF guard for state-changing routes: a cross-origin browser page can't set
        # a custom header on a simple request without a (failing) CORS preflight, so
        # this blocks drive-by POSTs (e.g. a webpage trying to enable your camera).
        if request.headers.get("x-wavr-local") != "1":
            raise HTTPException(status_code=403, detail="missing X-Wavr-Local header")

    @app.get("/api/history")
    async def history(limit: int = 200):
        return _storage.recent(limit)

    @app.get("/api/state")
    async def state():
        return latest

    @app.get("/api/system")
    async def system():
        return manager.status()

    @app.post("/api/system/toggle")
    async def system_toggle(on: bool = Body(..., embed=True), _=Depends(require_local)):
        await manager.set_running(on)
        return manager.status()

    @app.post("/api/sources/{name}/toggle")
    async def source_toggle(name: str, enabled: bool = Body(..., embed=True), _=Depends(require_local)):
        try:
            await manager.set_enabled(name, enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown source: {name}")
        return manager.status()

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        await ws.accept()
        q = _hub.subscribe()
        try:
            while True:
                await ws.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            _hub.unsubscribe(q)

    return app


app = create_app()
```

- [ ] **Step 4: Run — expect 7 passed.** `pytest tests/test_app.py -v`

- [ ] **Step 5: Run the FULL suite — expect 28 passed** (3+1+3+6+2+2+1+3+7 = 28). `pytest -v`

- [ ] **Step 6: Manual smoke — the fused stream on simulated data**

Run from `C:\IA\wavr\backend`: `uvicorn wavr.app:app --host 127.0.0.1 --port 8000`
Open `http://localhost:8000/api/history` → a growing JSON list of RoomStates with `occupied`/`confidence`/`explanation`. Ctrl+C to stop.

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/app.py backend/tests/test_app.py
git commit -m "feat: FastAPI app — source->fusion->storage->hub, ws/live, api/state+history"
```

---

### Task 10: Frontend dashboard (RoomState + confidence + why + controls)

**Files:**
- Create: `frontend/index.html`
- Modify: `backend/wavr/app.py` (add `GET /` to serve the dashboard same-origin)

**Interfaces:**
- Consumes: `WS /ws/live` + `GET /api/history` (Plano A, same-origin); nothing external (Plano B).
- Produces: `DataProvider` `{ start(onEvent), history() }` with `WebSocketProvider` (Plano A) and `SimulatorProvider` (Plano B, generates fictional RoomStates). Renders per-room cards with a confidence bar + per-modality breakdown + a timeline. In **live** mode it also renders the control bar (global on/off + per-source toggles) wired to `/api/system`. Mode auto-selects: `live` on localhost, else `simulated`.

- [ ] **Step 1: Create `frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wavr — Fused Home Sensing</title>
<style>
  :root{--bg:#0f1216;--surface:#171c22;--ink:#e8edf2;--muted:#9aa7b4;
        --accent:#3db54a;--warn:#e0b341;--line:#232a32;--radius:14px;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}
  header{padding:20px 24px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;justify-content:space-between;}
  h1{font-size:1.25rem;margin:0;letter-spacing:-0.01em;}
  .mode{font-size:.8rem;color:var(--muted);}
  .mode b{color:var(--accent);}
  main{padding:24px;max-width:1000px;margin:0 auto;}
  .rooms{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px;}
  .card h2{margin:0 0 4px;font-size:1rem;text-transform:capitalize;}
  .conf{font-variant-numeric:tabular-nums;font-size:.82rem;color:var(--muted);}
  .pill{display:inline-block;font-size:.75rem;padding:3px 10px;border-radius:999px;margin:8px 0;}
  .pill.on{background:rgba(61,181,74,.15);color:var(--accent);}
  .pill.off{background:rgba(154,167,180,.12);color:var(--muted);}
  .bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin:6px 0 12px;}
  .bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s ease-out;}
  .src{display:flex;justify-content:space-between;font-size:.78rem;color:var(--muted);
       padding:3px 0;border-top:1px dashed var(--line);}
  .src .m{text-transform:capitalize;color:var(--ink);}
  .vit{margin-top:10px;font-size:.82rem;font-variant-numeric:tabular-nums;}
  h3{margin:28px 0 12px;font-size:.9rem;color:var(--muted);font-weight:600;}
  .timeline{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
            padding:8px 0;max-height:260px;overflow:auto;}
  .row{display:flex;gap:14px;padding:7px 18px;font-size:.82rem;border-bottom:1px solid var(--line);
       font-variant-numeric:tabular-nums;}
  .row:last-child{border-bottom:0;}
  .row .t{color:var(--muted);min-width:88px;}
  .row .r{text-transform:capitalize;min-width:80px;}
  .controls:not([hidden]){display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 24px;border-bottom:1px solid var(--line);}
  .ctl{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:7px 14px;font-size:.8rem;cursor:pointer;}
  .ctl.on{border-color:var(--accent);color:var(--accent);}
  .ctl.off{color:var(--muted);}
  .ctl.small{padding:5px 11px;font-size:.75rem;}
  .src-toggles{display:flex;gap:8px;flex-wrap:wrap;}
  @media (prefers-reduced-motion:reduce){.bar>i{transition:none;}}
</style>
</head>
<body>
<header><h1>Wavr</h1><div class="mode" id="mode"></div></header>
<div id="controls" class="controls" hidden>
  <button id="sysToggle" class="ctl"></button>
  <div id="srcToggles" class="src-toggles"></div>
</div>
<main>
  <div class="rooms" id="rooms"></div>
  <h3>Timeline</h3>
  <div class="timeline" id="timeline"></div>
</main>
<script>
// ---- DataProvider contract: { start(onEvent), history() } — items are RoomState dicts ----
function WebSocketProvider(){
  const base = location.origin;
  return {
    async history(){ try{ const r=await fetch(base+"/api/history?limit=100"); return await r.json(); }catch{ return []; } },
    start(onEvent){
      const ws = new WebSocket(base.replace(/^http/,"ws")+"/ws/live");
      ws.onmessage = (m)=> onEvent(JSON.parse(m.data));
      ws.onclose = ()=> setTimeout(()=> this.start(onEvent), 1500);
    },
  };
}
function SimulatorProvider(){
  // Fictional multi-modal apartment producing RoomState directly (no backend).
  const rooms = {
    casa:    [["network"]],
    sala:    [["wifi_csi"]],
    quarto:  [["wifi_csi"],["camera"]],
    quintal: [["camera"]],
  };
  const W = {camera:1.0, wifi_csi:0.6, network:0.45};
  let tick = 0;
  function stateFor(room){
    const mods = rooms[room];
    let num=0, den=0; const sources=[]; let vitals={};
    mods.forEach(([m],i)=>{
      const present = ((tick+i)%7)<4;
      const conf = present ? W[m] : W[m]*0.3;
      den += W[m]*conf; if(present) num += W[m]*conf;
      sources.push({modality:m, presence:present, confidence:+conf.toFixed(3)});
      if(present && m==="wifi_csi") vitals={breathing_bpm:+(12+3*Math.sin(tick/5)).toFixed(1), heart_bpm:+(60+10*Math.sin(tick/4)).toFixed(0)};
    });
    const confidence = den>0 ? +(num/den).toFixed(3) : 0;
    const parts = sources.map(s=> `${s.modality}: ${s.presence?"presente":"vazio"}`);
    return {room, occupied:confidence>=0.5, confidence, vitals, sources,
            explanation: parts.join(" · ")+` → ${Math.round(confidence*100)}% ocupado`,
            ts:new Date().toISOString()};
  }
  const all = ()=> Object.keys(rooms).map(stateFor);
  return {
    async history(){ const out=[]; for(let t=0;t<12;t++){ tick=t; out.push(...all()); } tick=12; return out; },
    start(onEvent){ setInterval(()=>{ all().forEach(onEvent); tick++; }, 1500); },
  };
}

// ---- Mode selection (PRIVACY: never "live" off localhost) ----
const MODE = (location.hostname==="localhost"||location.hostname==="127.0.0.1") ? "live" : "simulated";
const provider = MODE==="live" ? WebSocketProvider() : SimulatorProvider();
document.getElementById("mode").innerHTML = MODE==="live"
  ? "fonte: <b>casa real</b> (local)" : "fonte: <b>demo</b> (dados fictícios)";

// ---- Control plane (Plano A / live only): global on/off + per-source on/off ----
async function renderControls(){
  if(MODE!=="live") return;               // the control plane is a backend feature
  document.getElementById("controls").hidden = false;
  const post = (url,body)=> fetch(location.origin+url,{method:"POST",
    headers:{"Content-Type":"application/json","X-Wavr-Local":"1"}, body:JSON.stringify(body)});
  async function refresh(){
    let s; try{ s = await (await fetch(location.origin+"/api/system")).json(); }catch{ return; }
    const sys = document.getElementById("sysToggle");
    sys.textContent = s.running ? "Sistema: LIGADO" : "Sistema: DESLIGADO";
    sys.className = "ctl " + (s.running ? "on" : "off");
    sys.onclick = async ()=>{ await post("/api/system/toggle",{on:!s.running}); refresh(); };
    const wrap = document.getElementById("srcToggles"); wrap.innerHTML = "";
    s.sources.forEach(src=>{
      const b = document.createElement("button");
      b.className = "ctl small " + (src.enabled ? "on" : "off");
      b.textContent = `${src.name}: ${src.enabled ? "on" : "off"}`;
      b.onclick = async ()=>{ await post(`/api/sources/${src.name}/toggle`,{enabled:!src.enabled}); refresh(); };
      wrap.appendChild(b);
    });
  }
  refresh(); setInterval(refresh, 3000);
}
renderControls();

// ---- Rendering (RoomState) ----
const roomsEl = document.getElementById("rooms");
const timelineEl = document.getElementById("timeline");
const cards = {};

function upsert(rs){
  let c = cards[rs.room];
  if(!c){
    const el = document.createElement("div"); el.className="card";
    el.innerHTML = `<h2>${rs.room}</h2><div class="conf"></div>
      <span class="pill"></span><div class="bar"><i></i></div>
      <div class="srcs"></div><div class="vit"></div>`;
    roomsEl.appendChild(el); c = cards[rs.room] = el;
  }
  c.querySelector(".conf").textContent = `confiança ${Math.round(rs.confidence*100)}%`;
  const pill = c.querySelector(".pill");
  pill.textContent = rs.occupied ? "ocupado" : "vazio";
  pill.className = "pill " + (rs.occupied ? "on" : "off");
  c.querySelector(".bar>i").style.width = Math.round(rs.confidence*100) + "%";
  c.querySelector(".srcs").innerHTML = rs.sources.map(s =>
    `<div class="src"><span class="m">${s.modality}</span><span>${s.presence?"presente":"vazio"} · ${Math.round(s.confidence*100)}%</span></div>`).join("");
  const v = rs.vitals || {};
  c.querySelector(".vit").textContent = v.breathing_bpm!=null
    ? `resp ${v.breathing_bpm} rpm · bpm ${v.heart_bpm}` : "";
}

function pushTimeline(rs){
  const row = document.createElement("div"); row.className="row";
  row.innerHTML = `<span class="t">${rs.ts.slice(11,19)}</span><span class="r">${rs.room}</span>
    <span>${rs.occupied?"ocupado":"vazio"} (${Math.round(rs.confidence*100)}%)</span>`;
  timelineEl.prepend(row);
  while(timelineEl.children.length>60) timelineEl.lastChild.remove();
}

const handle = (rs)=>{ upsert(rs); pushTimeline(rs); };
(async ()=>{ (await provider.history()).forEach(handle); provider.start(handle); })();
</script>
</body>
</html>
```

- [ ] **Step 2: Serve the dashboard from the backend (same-origin, no CORS)**

Add at the **top of `backend/wavr/app.py`** (after the existing imports — the first two are imports, the `_INDEX` line is a module-level constant):
```python
from pathlib import Path
from fastapi.responses import FileResponse

_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
```
And add this route **inside `create_app`**, just before `return app`:
```python
    @app.get("/")
    async def dashboard():
        return FileResponse(_INDEX)
```

- [ ] **Step 3: Verify Plano A (live)**

Start the backend from `C:\IA\wavr\backend`: `uvicorn wavr.app:app --host 127.0.0.1 --port 8000`. Open `http://localhost:8000/`. Expect: room cards (casa, sala, quarto, quintal) with confidence bars, a per-modality breakdown under each, vitals on wifi rooms, a live timeline. Mode label: **fonte: casa real (local)**. Also expect the control bar at top: a **Sistema: LIGADO** button + a **sim: on** toggle. Click **Sistema** → flips to DESLIGADO and cards stop updating (footprint drops to ~zero); click again → resumes. Toggle **sim** off → that source stops feeding.

- [ ] **Step 4: Verify with Playwright (keep the uvicorn server running in another terminal)**

Using the Playwright MCP (agent-assisted; a human can instead just eyeball Step 3): navigate to `http://localhost:8000/`, wait for a `.card`, assert ≥1 card, that a `.pill` toggles ocupado/vazio and a `.bar>i` width changes within a few seconds, and that `.src` breakdown rows render. Screenshot for the record.

- [ ] **Step 5: Verify Plano B (simulated) offline**

Double-click `frontend\index.html` (opens `file:///C:/IA/wavr/frontend/index.html`) with NO backend running. Mode label must read **fonte: demo (dados fictícios)**; cards + confidence + breakdown must animate. Proves the public build is self-contained.

- [ ] **Step 6: Commit**

```powershell
git add frontend/index.html backend/wavr/app.py
git commit -m "feat: fused-RoomState dashboard (confidence + per-modality why) + backend serves it"
```

---

### Task 11: Impeccable polish + Plano B deploy to Cloudflare Pages

**Files:**
- Modify: `frontend/index.html` (polish only)
- Create: `docs/seams.md`

- [ ] **Step 1: Mandatory design pass (project rule)**

Run `/impeccable polish frontend/index.html` then `/impeccable audit frontend/index.html`. Fix contrast/spacing/hierarchy findings (agent-assisted; required before any client-facing deploy). Re-run the Task 10 Step 4 Playwright check after changes.

- [ ] **Step 2: Write `docs/seams.md`**

```markdown
# Wavr — Extension seams

- **Sub-plan B (real sources):** add `NetworkSource` / `RuViewSource` as more `SensorSource`
  implementations, then register them in `create_app`'s `sources` list (or via
  `SourceManager.register`). The manager already runs one task per source and fans them into the
  shared `_ingest` → FusionEngine → storage → hub — no merge code needed. FusionEngine and
  dashboard are unchanged.
- **Sub-plan C (camera + CV):** `CameraSource` (RTSP + YOLO) registered with `enabled=False` so it
  never starts at boot (safe default). Enabling is runtime via `POST /api/sources/{name}/toggle`
  (already CSRF-guarded by the `X-Wavr-Local` header). The camera MUST release RTSP + stop YOLO in
  its generator's `finally` — `SourceManager._run` triggers it via `agen.aclose()` on disable — and
  should read frames in a cancellation-responsive worker it can join, so a stalled read can't
  outlive a disable. Only derived events persist; never frames. Toggle state is in-memory: cameras
  always boot OFF (safe), no persisted ON.
- **Camada 2/3 (rules, away):** subscribe via `Hub.subscribe()` and register the subscriber as a
  task inside `create_app`'s `lifespan`. React to RoomState; emit MQTT to localhost:1883.
- **Camada 4 (AI narration):** read `GET /api/state` (latest RoomState per room) + `GET /api/history`.
- **Network granularity:** `network` is a house-level signal (a "casa" pseudo-room) — a weak hint
  that does NOT corroborate specific rooms in A/B; folding it as a per-room prior is a later refinement.
- **Deferred:** Supabase history for Plano B (intentionally not built — less surface, safer).
```

- [ ] **Step 3: Deploy Plano B to Cloudflare Pages**

Deploy ONLY `frontend/` (static, backend-less → non-localhost host → auto-simulated). Run from `C:\IA\wavr`:
```powershell
node --version                # confirm Node.js present
npx wrangler login            # first time only; a browser opens — approve Cloudflare access
npx wrangler pages deploy frontend --project-name wavr
```
Expected: a URL like `https://wavr.pages.dev`.

- [ ] **Step 4: Verify the public deploy is safe**

Open the `*.pages.dev` URL. Confirm: mode label **demo (dados fictícios)**; cards animate; DevTools → Network shows NO request to `localhost`/`:8000` (privacy: the public build cannot reach any backend).

- [ ] **Step 5: Commit**

```powershell
git add frontend/index.html docs/seams.md
git commit -m "feat: Impeccable polish + Plano B deploy + seams doc"
```

---

## Definition of Done (Sub-plan A)
- [ ] Multi-modal `SimulatedSource` → `FusionEngine` → `RoomState` end-to-end, 28 tests passing.
- [ ] Dashboard shows fused RoomState per room: confidence bar + per-modality breakdown + timeline.
- [ ] Control plane works: global on/off and per-source on/off via `SourceManager` + `/api/system`;
      global off cancels all source tasks (~zero footprint); dashboard shows the controls (live mode).
- [ ] Backend serves the dashboard same-origin; binds loopback; Host allowlist active.
- [ ] Same HTML deployed to Cloudflare Pages runs the multi-modal simulator (Plano B), backend-less.
- [ ] No real data path exists yet (this sub-plan is simulated-only) and none can leak.

## Next
Sub-plan B (NetworkSource + RuViewSource — real $0 + WiFi CSI), then Sub-plan C (CameraSource + CV + safety toggle). Each is its own plan built on this working foundation.
