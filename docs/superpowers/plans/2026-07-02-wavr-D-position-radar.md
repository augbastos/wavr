# Wavr Sub-plano D — Position & Posture Radar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make Wavr position-aware: a top-down radar view showing WHERE people are in each room (x/y dots) and WHAT they're doing (standing/sitting/lying/walking) — with the simulator powering it today ($0) and hardware sources (mmWave LD2450, RuView CSI pose, camera YOLO-pose) plugging in the day they arrive, zero core rewrite.

**Architecture:** Extend the event schema with an optional `Target` list (per-person x/y in room-local meters + posture), flowing SensingEvent → FusionEngine → RoomState → WS → dashboard unchanged in shape. Target fusion v1 is honest and simple: the highest-trust source currently reporting targets for a room provides that room's targets (cross-source track association is a research problem — explicitly deferred). Presence fusion math is UNTOUCHED. A static house map (JSON, config-driven) gives the radar view its floor plan. All new sources follow the CameraSource pattern: pure parsing functions + injectable transports + lazy optional deps, fully mock-tested with no hardware.

**Tech Stack:** Python 3.11 dataclasses, FastAPI, pyserial (lazy, `[mmwave]` extra), ultralytics YOLO11-pose (lazy, existing `[camera]` extra), vanilla-JS SVG radar in the single-file dashboard.

## Global Constraints

- **No RNG in the simulator** (existing rule) — target paths are deterministic sin/cos walks.
- **Privacy invariants unchanged:** targets are DERIVED data (never frames/keypoint images). MQTT payloads stay `occupied/confidence/ts` only — targets do NOT go to MQTT. Nothing new leaves the LAN. Plano B (public demo) gets simulated targets only.
- **Backward compatible:** `targets` is optional everywhere (`None` default on SensingEvent, `[]` on RoomState). All 110 existing tests must keep passing untouched (except where a task explicitly extends one).
- **Lazy deps:** pyserial only imported inside the default mmWave transport; YOLO-pose only inside `yolo_pose_detect`. Base install stays lean. New extra: `mmwave = ["pyserial>=3.5"]`.
- **Coordinates:** room-local frame, METERS, float. Origin = the room's top-left corner on the house map, x → right, y → down (screen-like; sensor calibration offset/rotation is a documented follow-up, YAGNI now).
- **Posture vocabulary:** open strings — `"standing" | "sitting" | "lying" | "walking"` | `None` (unknown). Never an enum (future-proof: `"fallen"`, `"running"` etc. slot in without schema change).
- **Hardware absent:** LD2450/ESP32/RuView not owned yet. Everything mock-tested; live bring-up = manual step documented in Task 8. Do NOT claim hardware verification.
- Keep files under 500 lines; suite green (`.venv\Scripts\python.exe -m pytest backend/tests -q`) after every task.

**Branch:** `sub-plan-d-position-radar` off `master`.

**Existing structure (verbatim, for implementers):**
- `SensingEvent` (backend/wavr/events.py): frozen dataclass `room, modality, presence, motion, breathing_bpm, heart_bpm, confidence, ts` + `to_dict()` via `asdict`. `normalize_ruview(raw, room)` builds it from a RuView WS frame.
- `RoomState` (backend/wavr/roomstate.py): frozen dataclass `room, occupied, confidence, vitals, sources, explanation, ts`.
- `FusionEngine._fuse` (backend/wavr/fusion.py): keeps `self._latest[room][modality] = event`; weights `DEFAULT_WEIGHTS = {"camera": 1.0, "wifi_csi": 0.85, "network": 0.5, "sim": 0.6}`.
- `SimulatedSource` (backend/wavr/sources/simulated.py): deterministic `SENSORS` list, `_make(room, modality, idx)`.
- `CameraSource` (backend/wavr/sources/camera.py): the seam pattern to copy — injectable `frames`/`detect`, lazy imports, keep-alive loop.
- Dashboard: `frontend/index.html` single file; `DataProvider` contract `{start(onEvent), history()}` delivering RoomState dicts; `SimulatorProvider` mirrors backend fusion math in JS.
- Config: `backend/wavr/config.py` `load_config()` reads `WAVR_*` env.

---

### Task 1: Target schema — events, roomstate, fusion pass-through

**Files:**
- Modify: `backend/wavr/events.py`
- Modify: `backend/wavr/roomstate.py`
- Modify: `backend/wavr/fusion.py`
- Test: `backend/tests/test_targets.py` (new)

**Interfaces:**
- Produces: `Target` frozen dataclass in `backend/wavr/events.py`:
  `Target(id: int, x: float | None, y: float | None, z: float | None = None, posture: str | None = None, velocity: float | None = None, confidence: float = 0.0)` with `to_dict()`.
- Produces: `SensingEvent.targets: tuple = ()` (new optional field, LAST field, default empty tuple — frozen+hashable safe).
- Produces: `RoomState.targets: list = field(default_factory=list)` (list of target dicts).
- Produces: FusionEngine rule — `RoomState.targets` = targets (as dicts) of the highest-weight modality whose latest event has non-empty `targets` AND `presence=True`; `[]` when none.

- [ ] **Step 1: Write failing tests** (`backend/tests/test_targets.py`)

```python
from wavr.events import SensingEvent, Target
from wavr.fusion import FusionEngine


def _ev(modality, presence=True, targets=(), conf=0.9):
    return SensingEvent(room="sala", modality=modality, presence=presence,
                        motion=1.0, breathing_bpm=None, heart_bpm=None,
                        confidence=conf, ts="2026-07-02T00:00:00+00:00",
                        targets=targets)


def test_target_to_dict_roundtrip():
    t = Target(id=1, x=1.5, y=2.0, posture="sitting", velocity=0.0, confidence=0.8)
    d = t.to_dict()
    assert d["x"] == 1.5 and d["posture"] == "sitting" and d["z"] is None


def test_event_targets_default_empty_and_serializes():
    e = _ev("network")
    assert e.targets == ()
    assert e.to_dict()["targets"] == []          # JSON-friendly list


def test_fusion_passes_through_targets_from_best_source():
    f = FusionEngine()
    t_csi = (Target(id=1, x=1.0, y=1.0, confidence=0.7),)
    t_cam = (Target(id=1, x=2.0, y=2.0, posture="standing", confidence=0.9),)
    f.update(_ev("wifi_csi", targets=t_csi))
    rs = f.update(_ev("camera", targets=t_cam))
    assert rs.targets == [t_cam[0].to_dict()]     # camera (1.0) beats wifi_csi (0.85)


def test_fusion_targets_empty_when_no_source_has_them():
    f = FusionEngine()
    rs = f.update(_ev("network"))
    assert rs.targets == []


def test_fusion_ignores_targets_of_absent_source():
    f = FusionEngine()
    rs = f.update(_ev("camera", presence=False,
                      targets=(Target(id=1, x=0.0, y=0.0),)))
    assert rs.targets == []


def test_posture_only_target_allowed():
    # camera gives posture without position (no homography yet)
    t = Target(id=1, x=None, y=None, posture="lying", confidence=0.9)
    assert t.to_dict()["x"] is None and t.to_dict()["posture"] == "lying"
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_targets.py -q`
Expected: FAIL — `ImportError: cannot import name 'Target'`.

- [ ] **Step 3: Implement `Target` + `SensingEvent.targets`** in `backend/wavr/events.py`

```python
@dataclass(frozen=True)
class Target:
    """One tracked person. Room-local frame: meters, origin = room's top-left
    on the house map, x right / y down. x/y None = source knows posture but
    not position (e.g. camera without homography)."""
    id: int
    x: float | None
    y: float | None
    z: float | None = None
    posture: str | None = None      # open vocab: standing/sitting/lying/walking/...
    velocity: float | None = None   # m/s, magnitude
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
```

Add to `SensingEvent` as the LAST field: `targets: tuple = ()`, and make `to_dict()` emit a list: `d = asdict(self); d["targets"] = [t if isinstance(t, dict) else t for t in d["targets"]]` — note `asdict` already converts nested dataclasses to dicts; only the tuple→list conversion is needed: `d["targets"] = list(d["targets"])`.

- [ ] **Step 4: Implement `RoomState.targets`** in `backend/wavr/roomstate.py`: add `targets: list = field(default_factory=list)` after `sources`.

- [ ] **Step 5: Implement fusion pass-through** in `backend/wavr/fusion.py` `_fuse`, after the source loop:

```python
        best_targets: list = []
        best_w = -1.0
        for modality, e in events.items():
            if e.presence and e.targets:
                w = self._weights.get(modality, 0.5)
                if w > best_w:
                    best_w = w
                    best_targets = [t.to_dict() for t in e.targets]
```

and pass `targets=best_targets` into the `RoomState(...)` constructor.

- [ ] **Step 6: Full suite green**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: all pass (110 old + 6 new). If an old test constructs SensingEvent positionally with exactly 8 args it still works (targets defaults).

- [ ] **Step 7: Commit** — `git commit -m "feat: Target schema — per-person x/y/posture on events, best-source pass-through fusion"`

---

### Task 2: Simulator emits walking targets (radar data today, $0)

**Files:**
- Modify: `backend/wavr/sources/simulated.py`
- Test: extend `backend/tests/test_simulated_source.py`

**Interfaces:**
- Consumes: `Target` from Task 1.
- Produces: `SimulatedSource._make` attaches deterministic targets to `wifi_csi` and `camera` events when present: one person walking an elliptical path inside a 4×3 m room, posture cycling by phase.

- [ ] **Step 1: Failing test** (append to `test_simulated_source.py`)

```python
def test_sim_emits_walking_target_when_present():
    src = SimulatedSource(interval=0)
    ev = src._make("sala", "wifi_csi", idx=1)   # phase 1 → present ((1%7)<4)
    assert len(ev.targets) == 1
    t = ev.targets[0]
    assert 0.0 <= t.x <= 4.0 and 0.0 <= t.y <= 3.0
    assert t.posture in ("standing", "sitting", "walking")


def test_sim_no_targets_when_absent_or_network():
    src = SimulatedSource(interval=0)
    assert src._make("sala", "wifi_csi", idx=4).targets == ()   # phase 4 → absent
    assert src._make("casa", "network", idx=0).targets == ()    # house-level: never
```

- [ ] **Step 2: Run — expect FAIL** (`targets == ()` on present event).

- [ ] **Step 3: Implement** in `_make`, before constructing the event:

```python
        targets = ()
        if present and modality in ("wifi_csi", "camera"):
            # Deterministic ellipse walk inside a 4x3 m room; posture cycles.
            px = round(2.0 + 1.6 * math.sin(phase / 4.0), 2)
            py = round(1.5 + 1.1 * math.cos(phase / 4.0), 2)
            posture = ("walking", "standing", "sitting")[(phase // 5) % 3]
            speed = 0.5 if posture == "walking" else 0.0
            targets = (Target(id=1, x=px, y=py, posture=posture,
                              velocity=speed, confidence=conf),)
```

(import `Target` from `wavr.events`; pass `targets=targets` to the SensingEvent.)

- [ ] **Step 4: Suite green.** Run full pytest as always.
- [ ] **Step 5: Commit** — `git commit -m "feat: simulator emits deterministic walking target with posture"`

---

### Task 3: House map — config-driven floor plan + GET /api/house

**Files:**
- Create: `backend/wavr/housemap.py`
- Modify: `backend/wavr/config.py` (add `house_map: str` ← `WAVR_HOUSE_MAP`, default `""`)
- Modify: `backend/wavr/app.py` (route)
- Test: `backend/tests/test_housemap.py` (new)

**Interfaces:**
- Produces: `load_house_map(path: str) -> dict` — reads JSON `{"rooms": [{"name": str, "x": float, "y": float, "w": float, "h": float}]}` (meters, house frame); on empty path / missing file / bad JSON returns `DEFAULT_MAP` (never raises).
- Produces: `DEFAULT_MAP` matching the simulator rooms:
  `{"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}, {"name": "quarto", "x": 4.2, "y": 0, "w": 3.5, "h": 3}, {"name": "quintal", "x": 0, "y": 3.2, "w": 7.7, "h": 2.5}]}`
- Produces: `GET /api/house` → the map dict (read-only, no auth needed beyond the app's global loopback posture; same trust level as /api/state).

- [ ] **Step 1: Failing tests** (`test_housemap.py`)

```python
import json
from wavr.housemap import load_house_map, DEFAULT_MAP


def test_missing_path_returns_default():
    assert load_house_map("") == DEFAULT_MAP
    assert load_house_map("nope/does-not-exist.json") == DEFAULT_MAP


def test_valid_file_loads(tmp_path):
    p = tmp_path / "house.json"
    m = {"rooms": [{"name": "lab", "x": 0, "y": 0, "w": 5, "h": 4}]}
    p.write_text(json.dumps(m), encoding="utf-8")
    assert load_house_map(str(p)) == m


def test_garbage_file_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_house_map(str(p)) == DEFAULT_MAP
```

And in `backend/tests/test_app.py` style (use the existing app test fixture pattern with its in-memory stores):

```python
def test_get_house_returns_rooms(client):
    r = client.get("/api/house")
    assert r.status_code == 200
    assert any(room["name"] == "sala" for room in r.json()["rooms"])
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement `backend/wavr/housemap.py`**

```python
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

DEFAULT_MAP = {
    "rooms": [
        {"name": "sala",    "x": 0.0, "y": 0.0, "w": 4.0, "h": 3.0},
        {"name": "quarto",  "x": 4.2, "y": 0.0, "w": 3.5, "h": 3.0},
        {"name": "quintal", "x": 0.0, "y": 3.2, "w": 7.7, "h": 2.5},
    ]
}


def load_house_map(path: str) -> dict:
    """User's floor plan from JSON; DEFAULT_MAP on any problem (never raises)."""
    if not path:
        return DEFAULT_MAP
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("rooms"), list):
            return m
        log.warning("house map %s malformed (no rooms list); using default", path)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("house map %s unreadable (%s); using default", path, exc)
    return DEFAULT_MAP
```

- [ ] **Step 4: Wire config + route.** config.py: `house_map=os.getenv("WAVR_HOUSE_MAP", "")`. app.py inside `create_app`: `_house = load_house_map(cfg.house_map)` and

```python
    @app.get("/api/house")
    async def house():
        return _house
```

- [ ] **Step 5: Suite green. Commit** — `git commit -m "feat: config-driven house map + GET /api/house"`

---

### Task 4: mmWave LD2450 source — pure parser + injectable transport

**Files:**
- Create: `backend/wavr/sources/mmwave.py`
- Modify: `backend/wavr/config.py` (`mmwave_port` ← `WAVR_MMWAVE_PORT` default `""`, `mmwave_room` ← `WAVR_MMWAVE_ROOM` default `"sala"`)
- Modify: `backend/wavr/app.py` `_default_sources` (register `mmwave` ONLY when `cfg.mmwave_port` non-empty, enabled=True — passive local serial, no frames, safe always-on)
- Modify: `backend/pyproject.toml` (add `mmwave = ["pyserial>=3.5"]` optional extra)
- Test: `backend/tests/test_mmwave_source.py` (new)

**Interfaces:**
- Consumes: `Target`, `SensingEvent` from Task 1.
- Produces: `parse_ld2450_frame(frame: bytes) -> list[Target]` — pure function, one 30-byte LD2450 UART frame → 0..3 targets (mm → meters, sign-magnitude decode).
- Produces: `MmWaveSource(room: str, port: str, frames=None, interval: float = 0.2)` with `async def events()` yielding `SensingEvent(modality="mmwave", ...)`; `frames` is an injectable async generator of raw `bytes` frames (default = lazy pyserial reader). presence = any target decoded; posture = `"walking"` if `abs(velocity) > 0.25` else None.
- Produces: fusion weight — add `"mmwave": 0.9` to `DEFAULT_WEIGHTS` in fusion.py (position-precise, below camera, above wifi_csi).

**LD2450 protocol (HLK-LD2450, 256000 baud 8N1):** each report frame is 30 bytes:
header `AA FF 03 00` + 3 target slots × 8 bytes + tail `55 CC`. Slot = `x:int16le, y:int16le, speed:int16le, resolution:uint16le`, all-zero slot = no target. Sign-magnitude encoding (NOT two's complement): for x and speed, `raw & 0x8000` set → value `= raw & 0x7FFF`, clear → value `= -raw`; y same rule. Units: x/y mm, speed cm/s. (Same decode as the ESPHome `ld2450` component — verify against a real device at bring-up.)

- [ ] **Step 1: Failing tests** (`test_mmwave_source.py`)

```python
import asyncio
import struct

import pytest

from wavr.sources.mmwave import MmWaveSource, parse_ld2450_frame


def _slot(x_mm, y_mm, speed_cms):
    def enc(v):                      # sign-magnitude int16
        return (0x8000 | v) if v >= 0 else (-v)
    return struct.pack("<HHHH", enc(x_mm), enc(y_mm), enc(speed_cms), 320)


def _frame(*slots):
    body = b"".join(slots) + b"\x00" * 8 * (3 - len(slots))
    return b"\xaa\xff\x03\x00" + body + b"\x55\xcc"


def test_parse_one_target_mm_to_meters():
    ts = parse_ld2450_frame(_frame(_slot(1500, 2000, 0)))
    assert len(ts) == 1
    assert ts[0].x == pytest.approx(1.5) and ts[0].y == pytest.approx(2.0)
    assert ts[0].posture is None                     # not moving


def test_parse_negative_x_and_walking():
    ts = parse_ld2450_frame(_frame(_slot(-800, 1000, 60)))   # 0.6 m/s
    assert ts[0].x == pytest.approx(-0.8)
    assert ts[0].velocity == pytest.approx(0.6)
    assert ts[0].posture == "walking"


def test_parse_empty_and_garbage():
    assert parse_ld2450_frame(_frame()) == []
    assert parse_ld2450_frame(b"\x00" * 30) == []    # bad header
    assert parse_ld2450_frame(b"\xaa\xff\x03\x00" + b"\x01" * 10) == []  # short


@pytest.mark.asyncio
async def test_source_emits_presence_from_injected_frames():
    async def fake_frames():
        yield _frame(_slot(1000, 1000, 0))
        yield _frame()                               # everyone left

    src = MmWaveSource(room="sala", port="", frames=fake_frames(), interval=0)
    gen = src.events()
    e1 = await asyncio.wait_for(anext(gen), 1)
    assert e1.presence is True and e1.modality == "mmwave" and len(e1.targets) == 1
    e2 = await asyncio.wait_for(anext(gen), 1)
    assert e2.presence is False and e2.targets == ()
    await gen.aclose()
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement `backend/wavr/sources/mmwave.py`**

```python
from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timezone
from typing import AsyncIterator

from wavr.events import SensingEvent, Target

log = logging.getLogger(__name__)

_HEADER = b"\xaa\xff\x03\x00"
_TAIL = b"\x55\xcc"
_WALK_MS = 0.25          # |velocity| above this = "walking"


def _signmag(raw: int) -> int:
    return (raw & 0x7FFF) if raw & 0x8000 else -raw


def parse_ld2450_frame(frame: bytes) -> list[Target]:
    """One 30-byte LD2450 report frame -> decoded targets (meters, m/s)."""
    if len(frame) != 30 or not frame.startswith(_HEADER) or not frame.endswith(_TAIL):
        return []
    out: list[Target] = []
    for i in range(3):
        slot = frame[4 + i * 8: 12 + i * 8]
        if slot == b"\x00" * 8:
            continue
        rx, ry, rs, _res = struct.unpack("<HHHH", slot)
        vel = _signmag(rs) / 100.0                  # cm/s -> m/s
        out.append(Target(
            id=i + 1,
            x=_signmag(rx) / 1000.0,               # mm -> m
            y=_signmag(ry) / 1000.0,
            velocity=abs(vel),
            posture="walking" if abs(vel) > _WALK_MS else None,
            confidence=0.9,
        ))
    return out


async def _serial_frames(port: str) -> AsyncIterator[bytes]:
    """Default transport: read LD2450 frames from a local serial port.
    pyserial is a lazy optional dep ([mmwave] extra)."""
    import serial                                    # lazy: optional [mmwave] extra

    def _read_frame(s) -> bytes:
        buf = b""
        while True:
            buf += s.read(64)
            i = buf.find(_HEADER)
            if i >= 0 and len(buf) >= i + 30:
                return buf[i: i + 30]
            if len(buf) > 4096:
                buf = buf[-64:]

    s = serial.Serial(port, 256000, timeout=1)
    try:
        while True:
            yield await asyncio.to_thread(_read_frame, s)
    finally:
        s.close()


class MmWaveSource:
    """Room-level position radar from an HLK-LD2450 (serial today; the frames
    seam takes any async byte-frame generator — TCP/MQTT transports later)."""

    def __init__(self, room: str, port: str, frames: AsyncIterator[bytes] | None = None,
                 interval: float = 0.2):
        self._room = room
        self._port = port
        self._frames = frames
        self._interval = interval

    async def events(self) -> AsyncIterator[SensingEvent]:
        frames = self._frames if self._frames is not None else _serial_frames(self._port)
        async for raw in frames:
            targets = tuple(parse_ld2450_frame(raw))
            speed = max((t.velocity or 0.0 for t in targets), default=0.0)
            yield SensingEvent(
                room=self._room, modality="mmwave",
                presence=bool(targets), motion=speed,
                breathing_bpm=None, heart_bpm=None,
                confidence=0.9 if targets else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
                targets=targets,
            )
            if self._interval:
                await asyncio.sleep(self._interval)
```

- [ ] **Step 4: Wire weight + config + registration.** fusion.py `DEFAULT_WEIGHTS["mmwave"] = 0.9`. config.py new fields. app.py `_default_sources`: append `("mmwave", lambda: MmWaveSource(cfg.mmwave_room, cfg.mmwave_port), True)` only `if cfg.mmwave_port`. pyproject: `[project.optional-dependencies] mmwave = ["pyserial>=3.5"]`.

- [ ] **Step 5: Suite green. Commit** — `git commit -m "feat: MmWaveSource — LD2450 parser + injectable transport, x/y targets ([mmwave] extra)"`

---

### Task 5: RuView pose passthrough

**Files:**
- Modify: `backend/wavr/events.py` (`normalize_ruview`)
- Test: extend `backend/tests/test_events.py`

**Interfaces:**
- Consumes: `Target` from Task 1.
- Produces: `normalize_ruview` reads an OPTIONAL `raw["targets"]` list — items shaped `{"id": int, "x": float, "y": float, "z": float?, "posture": str?, "velocity": float?, "confidence": float?}` (meters; tolerant: skip non-dict items and items without numeric x/y unless posture-only) → `SensingEvent.targets`. Absent/malformed key → `()` exactly as today.

- [ ] **Step 1: Failing tests** (append to `test_events.py`)

```python
def test_normalize_ruview_reads_optional_targets():
    raw = {"classification": {"presence": True, "confidence": 0.8},
           "features": {"motion_band_power": 2.0},
           "targets": [{"id": 1, "x": 1.2, "y": 0.8, "posture": "standing"},
                       {"junk": True},          # tolerated, skipped
                       "not-a-dict"]}
    e = normalize_ruview(raw, room="sala")
    assert len(e.targets) == 1
    assert e.targets[0].x == 1.2 and e.targets[0].posture == "standing"


def test_normalize_ruview_no_targets_key_unchanged():
    e = normalize_ruview({"classification": {"presence": True}}, room="sala")
    assert e.targets == ()
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `normalize_ruview`, before the return:

```python
    targets = []
    for i, t in enumerate(raw.get("targets") or []):
        if not isinstance(t, dict):
            continue
        x, y = t.get("x"), t.get("y")
        posture = t.get("posture")
        has_pos = isinstance(x, (int, float)) and isinstance(y, (int, float))
        if not has_pos and not isinstance(posture, str):
            continue
        targets.append(Target(
            id=int(t.get("id", i + 1)),
            x=float(x) if has_pos else None,
            y=float(y) if has_pos else None,
            z=_f(t.get("z")),
            posture=posture if isinstance(posture, str) else None,
            velocity=_f(t.get("velocity")),
            confidence=float(t.get("confidence", 0.5)),
        ))
```

and pass `targets=tuple(targets)` into the SensingEvent.

- [ ] **Step 4: Suite green. Commit** — `git commit -m "feat: normalize_ruview passes through optional CSI pose targets"`

---

### Task 6: Camera posture — YOLO-pose keypoints → standing/sitting/lying

**Files:**
- Modify: `backend/wavr/sources/camera.py`
- Test: extend `backend/tests/test_camera_source.py`

**Interfaces:**
- Consumes: `Target` from Task 1.
- Produces: `classify_posture(keypoints: list[tuple[float, float]]) -> str | None` — pure function over COCO-17 keypoints in image pixels `(x, y)`; uses shoulders (idx 5,6), hips (11,12), knees (13,14), ankles (15,16). Rules, in order: torso axis (mid-shoulder→mid-hip) more horizontal than vertical (`abs(dx) > abs(dy)`) → `"lying"`; vertical hip→knee drop < 32% of torso length → `"sitting"`; else `"standing"`. Missing/zero-confidence needed keypoints → `None`.
- Produces: `yolo_pose_detect(frame, confidence: float) -> list[Target]` — lazy-loads `yolo11n-pose.pt` via the same `_model()`-style cached loader pattern as detection (own `_POSE_MODEL` global + the existing lock/release discipline); returns posture-only targets (`x=None, y=None` — no homography yet), confidence = box conf.
- Produces: `CameraSource(..., pose: bool = False, pose_detect=None)` — when `pose=True`, each detection cycle also runs `pose_detect` (injectable; default `yolo_pose_detect`) and attaches its targets to the emitted event. Default False: existing behavior byte-identical.

- [ ] **Step 1: Failing tests** (append to `test_camera_source.py`; follow that file's existing fake-frames/fake-detect patterns)

```python
from wavr.sources.camera import classify_posture

# COCO-17 indices: 5,6 shoulders / 11,12 hips / 13,14 knees / 15,16 ankles
def _kp(shoulder_y, hip_y, knee_y, x=100.0, dx=0.0):
    kps = [(0.0, 0.0)] * 17
    kps[5] = (x, shoulder_y); kps[6] = (x + 10, shoulder_y)
    kps[11] = (x + dx, hip_y); kps[12] = (x + dx + 10, hip_y)
    kps[13] = (x + dx, knee_y); kps[14] = (x + dx + 10, knee_y)
    kps[15] = (x + dx, knee_y + 80); kps[16] = (x + dx + 10, knee_y + 80)
    return kps


def test_posture_standing():
    assert classify_posture(_kp(100, 300, 450)) == "standing"   # big hip->knee drop


def test_posture_sitting():
    assert classify_posture(_kp(100, 300, 330)) == "sitting"    # knees near hip level


def test_posture_lying():
    assert classify_posture(_kp(200, 210, 215, dx=300)) == "lying"  # torso horizontal


def test_posture_missing_keypoints_none():
    assert classify_posture([(0.0, 0.0)] * 17) is None


@pytest.mark.asyncio
async def test_camera_pose_mode_attaches_targets():
    async def fake_frames():
        yield "frame1"

    def fake_detect(frame, confidence):
        return True, 0.9

    def fake_pose(frame, confidence):
        return [Target(id=1, x=None, y=None, posture="sitting", confidence=0.9)]

    src = CameraSource(room="quarto", url="rtsp://x", interval=0,
                       frames=fake_frames(), detect=fake_detect,
                       pose=True, pose_detect=fake_pose)
    ev = await asyncio.wait_for(anext(src.events()), 1)
    assert ev.targets and ev.targets[0].posture == "sitting"
```

(Adapt constructor kwargs to the REAL CameraSource signature — read the file first; keep its keep-alive semantics intact.)

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `classify_posture`** (pure, top of camera.py near other helpers)

```python
def classify_posture(keypoints) -> str | None:
    """COCO-17 pixel keypoints -> coarse posture. Pure heuristic, no ML."""
    def mid(a, b):
        (ax, ay), (bx, by) = keypoints[a], keypoints[b]
        if (ax, ay) == (0.0, 0.0) or (bx, by) == (0.0, 0.0):
            return None
        return ((ax + bx) / 2, (ay + by) / 2)

    sh, hip, knee = mid(5, 6), mid(11, 12), mid(13, 14)
    if sh is None or hip is None or knee is None:
        return None
    dx, dy = hip[0] - sh[0], hip[1] - sh[1]
    torso = (dx * dx + dy * dy) ** 0.5
    if torso == 0:
        return None
    if abs(dx) > abs(dy):
        return "lying"
    if (knee[1] - hip[1]) < 0.32 * torso:
        return "sitting"
    return "standing"
```

- [ ] **Step 4: Implement `yolo_pose_detect`** mirroring the existing `yolo_detect` lazy/cached/locked pattern with its own `_POSE_MODEL` global (model name `"yolo11n-pose.pt"`); for each detected person box above `confidence`, take `result.keypoints.xy[i]` as the 17 `(x, y)` pairs and produce `Target(id=i+1, x=None, y=None, posture=classify_posture(kps), confidence=box_conf)`. Hook `_POSE_MODEL` into the same `release_model()` VRAM-release path as the detect model.

- [ ] **Step 5: Wire `pose`/`pose_detect` kwargs** into CameraSource `__init__` + the detect cycle (attach targets to the emitted SensingEvent; `pose=False` default → zero behavior change). Camera add-form/API `pose` flag is NOT exposed yet (YAGNI — env-free, off by default; enabling comes with real-camera bring-up).

- [ ] **Step 6: Suite green. Commit** — `git commit -m "feat: camera posture — YOLO-pose keypoints to standing/sitting/lying (opt-in, lazy)"`

---

### Task 7: Radar view — top-down house map with live targets

**Files:**
- Modify: `frontend/index.html`

**Interfaces:**
- Consumes: `GET /api/house` (Task 3); `RoomState.targets` (Task 1) arriving via the existing WS/history flow; `SimulatorProvider` (demo mode).
- Produces: a `#radar` section between the header strip and `.rooms`: an SVG rendering every mapped room as a rectangle (house meters → viewBox units, label = room name, fill tint = occupied), and per room its current targets as dots at `(room.x + t.x, room.y + t.y)` with a posture glyph; posture-only targets (x/y null) render as a glyph pinned at the room center. Demo mode: `SimulatorProvider.stateFor` emits the same deterministic walking target as the backend sim (ellipse walk + posture cycle) so the public demo shows the radar moving.

- [ ] **Step 1: Backend sanity first.** Run the suite; then run the server (`scripts/wavr.ps1` or uvicorn directly) with `sim` toggled ON via the dashboard and confirm `curl http://127.0.0.1:8000/api/state` shows `"targets": [...]` with x/y for sala/quarto.

- [ ] **Step 2: Implement the radar section.**

Markup after the narrate block:

```html
<div id="radarWrap" class="radar"><h3 id="radar-h">Radar</h3>
  <svg id="radar" role="img" aria-labelledby="radar-h" preserveAspectRatio="xMidYMid meet"></svg>
</div>
```

CSS (match existing tokens): `.radar{padding:12px 24px;border-bottom:1px solid var(--line);} #radar{width:100%;max-width:760px;display:block;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);}` room rect: `fill:none;stroke:var(--line)` → occupied: `fill:rgba(61,181,74,.08);stroke:var(--accent)`; target dot: `fill:var(--accent)` circle r=0.12 (meter units) + `<title>` tooltip; posture glyph = small text label under the dot (`em pé` / `sentado` / `deitado` / `andando`) at font-size .28 units, `fill:var(--muted)`.

JS essentials:

```js
async function renderRadar(){
  let map;
  if(MODE==="live"){ try{ map = await (await fetch(location.origin+"/api/house")).json(); }catch{} }
  map = map || {rooms:[{name:"sala",x:0,y:0,w:4,h:3},{name:"quarto",x:4.2,y:0,w:3.5,h:3},
                       {name:"quintal",x:0,y:3.2,w:7.7,h:2.5}]};   // demo default = backend DEFAULT_MAP
  const svg = document.getElementById("radar");
  const W = Math.max(...map.rooms.map(r=>r.x+r.w)), H = Math.max(...map.rooms.map(r=>r.y+r.h));
  svg.setAttribute("viewBox", `-0.2 -0.2 ${W+0.4} ${H+0.4}`);
  const roomIdx = {};
  map.rooms.forEach(r=>{ /* create <g> with rect + name label; roomIdx[r.name]={r, layer:<g for dots>} */ });
  radarUpdate = (rs)=>{               // called from handle(rs)
    const e = roomIdx[rs.room]; if(!e) return;
    e.rect.classList.toggle("occ", !!rs.occupied);
    e.layer.replaceChildren(...(rs.targets||[]).map(t=> dot(e.r, t)));
  };
}
```

`dot(room, t)`: circle at `(room.x + (t.x ?? room.w/2), room.y + (t.y ?? room.h/2))` clamped into the room rect, plus the posture label. Wire into the existing pipeline: `const handle = (rs)=>{ upsert(rs); pushTimeline(rs); updateHouse(rs); radarUpdate?.(rs); };`

`SimulatorProvider.stateFor`: for rooms with `wifi_csi`/`camera` when present, add `targets:[{id:1, x:+(2.0+1.6*Math.sin(tick/4)).toFixed(2), y:+(1.5+1.1*Math.cos(tick/4)).toFixed(2), posture:["walking","standing","sitting"][Math.floor(tick/5)%3], confidence:0.9}]` (same math as the Python sim), else `targets:[]`.

- [ ] **Step 3: Verify live with Playwright.** Server on with sim ON: navigate `http://127.0.0.1:8000`, assert the radar SVG exists, has 3 room rects, and ≥1 circle appears within 3s; screenshot. Toggle sim OFF → dots disappear (network-only has no targets).

- [ ] **Step 4: Verify Plano B demo.** Open `frontend/index.html` via `file://` or the Pages preview: mode=demo, radar renders rooms + a moving dot with zero network requests to localhost (check console/network).

- [ ] **Step 5: Impeccable pass (MANDATORY pre-deploy, per project rule):** run `/polish` on `frontend/index.html`, then `/audit` — contrast of dot/labels on the dark surface, focus/aria on the new section, reduced-motion (dots may jump, no transition needed).

- [ ] **Step 6: Deploy Pages + commit.** `npx wrangler pages deploy frontend --project-name wavr`; verify the deployed URL shows the demo radar. `git commit -m "feat: radar view — top-down house map with live position + posture targets"`

---

### Task 8: Docs — hardware shopping list + bring-up path

**Files:**
- Modify: `docs/deploy/bring-up-and-expansion.md` (new section "Radar de posição — hardware")
- Modify: `PRODUCT.md` (one paragraph: position/posture capability, honest about v1 fusion)

**Interfaces:** none (docs).

- [ ] **Step 1: Write the hardware section** with EXACTLY this content shape (prices as of 2026-07, AliExpress/Amazon-IE ballpark — mark as estimates):

- **Tier R0 — radar de 1 cômodo, sem solda (~€15-20):** 1× HLK-LD2450 (~€10-15) + adaptador USB-TTL CP2102/CH340 (~€3-5) + 4 jumpers fêmea-fêmea (5V/GND/TX/RX — atenção: LD2450 usa UART 256000 baud). Liga DIRETO no PC: `WAVR_MMWAVE_PORT=COM3`, `WAVR_MMWAVE_ROOM=sala`, `pip install -e backend[mmwave]`, restart → pontos no radar. Zero ESP32, zero firmware.
- **Tier R1 — cômodo remoto (futuro, +€6-9/cômodo):** LD2450 + ESP32 baratinho; transporte TCP/MQTT é um `frames` generator novo — a classe e o parser NÃO mudam (seam já pronto).
- **Tier R2 — o experimento CSI (RuView, ~€25):** 2× ESP32-S3; quando os frames do RuView tiverem pose/targets, `normalize_ruview` já os aceita (passthrough pronto). Tratar como pesquisa, não como entregável.
- **Tier R3 — postura pelas câmeras que JÁ EXISTEM (€0):** Tapo C210 → `pip install -e backend[camera]` (~5GB, torch CUDA) + ligar `pose=True` no bring-up da câmera → "sentado/em pé/deitado" no radar (sem posição x/y — homografia é follow-up).
- Calibração (documentar honesto): x/y do LD2450 são no frame DO SENSOR (montado na parede, olhando pro cômodo). V1 assume sensor no canto-origem olhando pro +y; offset/rotação por cômodo = follow-up pequeno quando o hardware chegar.

- [ ] **Step 2: Commit** — `git commit -m "docs: position-radar hardware tiers + bring-up path"`

---

## Definition of Done
- [ ] `Target` flows SensingEvent → fusion (best-source pass-through) → RoomState → WS, all optional/backward-compatible; suite green throughout.
- [ ] Simulator produces deterministic walking targets — the radar view moves TODAY with zero hardware, live and in the public demo.
- [ ] `GET /api/house` serves the config-driven floor plan (env `WAVR_HOUSE_MAP`, safe default).
- [ ] `MmWaveSource` fully tested against the documented LD2450 protocol; registers only when `WAVR_MMWAVE_PORT` is set; pyserial stays a lazy `[mmwave]` extra.
- [ ] `normalize_ruview` accepts optional pose targets; camera pose mode (`classify_posture` + `yolo_pose_detect`) tested pure/mocked, off by default, VRAM discipline preserved.
- [ ] Radar SVG in the dashboard: rooms, occupancy tint, position dots, posture labels; posture-only targets pinned at room center; Playwright-verified live + demo; Impeccable pass done; Pages redeployed.
- [ ] MQTT payloads unchanged (no targets); no new external egress; no frames/keypoint images ever persisted.
- [ ] Docs: hardware tiers R0-R3 with the exact env vars + install commands for the day money arrives.

## Next (fora deste plano)
- Homografia da câmera (posição x/y a partir do YOLO) e calibração offset/rotação do mmWave por cômodo.
- Associação de tracks multi-fonte (fundir alvos de mmWave + câmera no MESMO cômodo) — pesquisa, Sub-plano E.
- "fallen" detection (lying + fora da cama/sofá + duração) — o caso de uso segurança-de-verdade em cima do que este plano entrega.
- **Casa 3D com paredes (pedido do Augusto 2026-07-02):** evoluir a vista de radar pra 3D. O seam já
  existe — `house.json`/`GET /api/house` é a fonte do formato da casa; extensão natural do schema:
  `wall_height` (default 2.6), aberturas/portas por parede, e futuramente polígonos em vez de retângulos.
  Duas rotas de render: (a) **isométrico SVG/CSS-transform** — zero dependência, mantém o dashboard
  single-file, paredes extrudadas semi-transparentes, dots viram pinos com sombra no chão (recomendado
  primeiro); (b) **Three.js** — 3D real com órbita/zoom (quebra a regra zero-dep; avaliar). Junto:
  **editor de casa in-app** (desenhar/ajustar cômodos pelo dashboard, persistir via API no lugar de
  editar JSON na mão) — mesmo padrão do camera-config (SQLite + CRUD + UI live-only). Candidato a
  Sub-plano F.
