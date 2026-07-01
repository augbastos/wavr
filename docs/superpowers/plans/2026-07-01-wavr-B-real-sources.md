# Wavr Sub-plan B — Real Sources (network + WiFi CSI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two real `SensorSource` implementations — `NetworkSource` (house-level presence via `arp -a` + ping sweep of the LAN) and `RuViewSource` (WiFi CSI presence/vitals via a WebSocket client) — that plug into the existing fusion pipeline unchanged, plus a concurrency test proving multiple sources run without starving each other.

**Architecture:** Both sources implement the existing `SensorSource` Protocol (`events() -> AsyncIterator[SensingEvent]`) and are registered in `create_app` via the existing `SourceManager`. All I/O (subprocess for ARP/ping, WebSocket client) is injected behind a callable seam so every source is fully unit-testable with fakes — no physical ESP32, RuView container, or live LAN required to build or test. Real data still never leaves the LAN; nothing here touches Plano B (the public browser demo).

**Tech Stack:** Python 3.11+, asyncio, `websockets` (WS client; already present transitively via `uvicorn[standard]`, made explicit here), stdlib `subprocess`/`asyncio.create_subprocess_exec` for ARP/ping. No scapy, no admin, no Npcap.

## Global Constraints

- **Platform:** Windows 11, PowerShell. Venv at `C:\IA\wavr\.venv`. All shell commands are PowerShell. Python interpreter: `C:\IA\wavr\.venv\Scripts\python.exe`.
- **Python:** 3.11+.
- **Canonical event shape — EXACT:** `{"room": str, "modality": str, "presence": bool, "motion": float, "breathing_bpm": float|None, "heart_bpm": float|None, "confidence": float, "ts": str}`; `ts` = ISO-8601 UTC with `+00:00` offset (`datetime.now(timezone.utc).isoformat()`). `modality` ∈ `{"wifi_csi","network","camera","sim"}`.
- **`confidence` is the modality's OWN confidence 0..1** — NOT the fusion weight. Fusion already weights `network` at 0.5 and `wifi_csi` at 0.85 (`fusion.py` `DEFAULT_WEIGHTS`); **do not touch `fusion.py`** in this sub-plan.
- **`network` is a house-level signal** — it emits `room="casa"` (the pseudo-room), never a specific room. It does not corroborate room-level presence.
- **Privacy:** real data never leaves the LAN. Sources persist only derived `RoomState` (already enforced in `storage.py`); never store raw frames/CSI/MACs beyond the in-memory presence decision. No source may reach the public Plano B build.
- **RuView WS port is 3000** — `ws://localhost:3000/ws/sensing` (not 8765). RuView pose model (`--load-rvf`) is out of scope; only presence + vitals from the sensing stream.
- **Mockable I/O — no hardware:** the ESP32/RuView container and live LAN are NOT assumed running. Every source takes an injected I/O callable; tests use fakes. The default (real) I/O path is thin and tested by monkeypatching the subprocess / WS client. Live validation against real hardware is a manual step, out of this plan's automated scope.
- **TDD discipline:** every code task: write failing test → run it, watch it fail *for the right reason* → minimal implementation → run, watch it pass → commit. Files < 500 lines. DRY, YAGNI.
- **Reuse, don't reinvent:** `normalize_ruview(raw: dict, room: str) -> SensingEvent` already exists in `events.py` and maps the RuView frame to the canonical event (sets `modality="wifi_csi"`). RuViewSource MUST use it, not re-map fields.

**Repo root for all paths:** `C:\IA\wavr\` (git repo; Sub-plan A merged to `master`). Work on a new branch `sub-plan-b-real-sources` off `master`.

**Existing interfaces this plan consumes (do not redefine):**
- `wavr.events.SensingEvent` — frozen dataclass, canonical shape above; `.to_dict()`.
- `wavr.events.normalize_ruview(raw: dict, room: str) -> SensingEvent`.
- `wavr.sources.base.SensorSource` — `Protocol` with `events(self) -> AsyncIterator[SensingEvent]`.
- `wavr.sourcemanager.SourceManager.register(name: str, factory: Callable[[], object], enabled: bool = True)`; runs one task per enabled source, calls `agen.aclose()` on teardown.
- `wavr.app.create_app(sources=None, ...)` — `sources` is a list of `(name, factory, enabled)` tuples; default is `[("sim", lambda: SimulatedSource(interval=cfg.sim_interval), True)]`.
- `wavr.config.load_config() -> Config` — dataclass; add fields here.

---

### Task 1: `parse_arp_table` — pure ARP-output parser + config fields

**Files:**
- Create: `backend/wavr/sources/network.py` (parser only this task)
- Create: `backend/tests/test_network_source.py` (parser tests this task)
- Modify: `backend/wavr/config.py` (add network config fields)
- Modify: `backend/tests/test_config.py` (assert new defaults)

**Interfaces:**
- Consumes: nothing new.
- Produces: `parse_arp_table(arp_output: str) -> set[str]` — extracts every MAC address from raw `arp -a` output, normalized to lowercase colon-separated form (`aa:bb:cc:dd:ee:ff`), regardless of Windows `-` or Unix `:` separators. `Config` gains `net_known_macs: set[str]` (lowercased colon-form), `net_interval: float`, `net_grace: int`, `ruview_url: str`, `ruview_room: str`, `ruview_reconnect: float`.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_network_source.py`

```python
from wavr.sources.network import parse_arp_table

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           AA-BB-CC-DD-EE-FF     dynamic
  192.168.0.23          11-22-33-44-55-66     dynamic
  192.168.0.255         ff-ff-ff-ff-ff-ff     static
"""

def test_parse_arp_table_extracts_normalized_macs():
    macs = parse_arp_table(WINDOWS_ARP)
    assert "aa:bb:cc:dd:ee:ff" in macs
    assert "11:22:33:44:55:66" in macs
    assert "ff:ff:ff:ff:ff:ff" in macs
    assert len(macs) == 3

def test_parse_arp_table_handles_colon_form_and_empty():
    assert parse_arp_table("host 0a:1b:2c:3d:4e:5f ok") == {"0a:1b:2c:3d:4e:5f"}
    assert parse_arp_table("") == set()
    assert parse_arp_table("no macs here 12-34") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.sources.network'` (or `ImportError` for `parse_arp_table`).

- [ ] **Step 3: Write minimal implementation** — create `backend/wavr/sources/network.py`

```python
from __future__ import annotations

import re

# Matches a MAC with either "-" (Windows arp) or ":" (Unix) separators.
_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


def parse_arp_table(arp_output: str) -> set[str]:
    """Extract every MAC from raw `arp -a` output, normalized to lowercase
    colon form. Separator-agnostic (Windows uses '-', Unix ':')."""
    macs = set()
    for m in _MAC_RE.findall(arp_output):
        macs.add(m.replace("-", ":").lower())
    return macs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Add config fields** — modify `backend/wavr/config.py`

Add these fields to the `Config` dataclass (after `fusion_threshold`):

```python
    net_known_macs: set[str]
    net_interval: float
    net_grace: int
    ruview_url: str
    ruview_room: str
    ruview_reconnect: float
```

And in `load_config()`, add to the returned `Config(...)`:

```python
        net_known_macs={
            m.strip().replace("-", ":").lower()
            for m in os.getenv("WAVR_NET_MACS", "").split(",")
            if m.strip()
        },
        net_interval=float(os.getenv("WAVR_NET_INTERVAL", "15.0")),
        net_grace=int(os.getenv("WAVR_NET_GRACE", "2")),
        ruview_url=os.getenv("WAVR_RUVIEW_URL", "ws://localhost:3000/ws/sensing"),
        ruview_room=os.getenv("WAVR_RUVIEW_ROOM", "sala"),
        ruview_reconnect=float(os.getenv("WAVR_RUVIEW_RECONNECT", "3.0")),
```

- [ ] **Step 6: Update config test** — modify `backend/tests/test_config.py`

Add a test (adapt to the file's existing style — it likely calls `load_config()` and asserts defaults):

```python
def test_config_has_source_b_defaults(monkeypatch):
    for var in ("WAVR_NET_MACS", "WAVR_NET_INTERVAL", "WAVR_NET_GRACE",
                "WAVR_RUVIEW_URL", "WAVR_RUVIEW_ROOM", "WAVR_RUVIEW_RECONNECT"):
        monkeypatch.delenv(var, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_known_macs == set()
    assert cfg.net_interval == 15.0
    assert cfg.net_grace == 2
    assert cfg.ruview_url == "ws://localhost:3000/ws/sensing"
    assert cfg.ruview_room == "sala"
    assert cfg.ruview_reconnect == 3.0
```

- [ ] **Step 7: Run the affected tests**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py backend/tests/test_config.py -q`
Expected: PASS (all).

- [ ] **Step 8: Commit**

```powershell
git add backend/wavr/sources/network.py backend/tests/test_network_source.py backend/wavr/config.py backend/tests/test_config.py
git commit -m "feat: arp-table parser + Sub-plan B config fields"
```

---

### Task 2: `NetworkSource` — async house-level presence source

**Files:**
- Modify: `backend/wavr/sources/network.py` (add `NetworkSource` + real scan fn)
- Modify: `backend/tests/test_network_source.py` (source tests)

**Interfaces:**
- Consumes: `parse_arp_table` (Task 1); `SensingEvent` (`events.py`); `Config` fields `net_known_macs`, `net_interval`, `net_grace`.
- Produces: `NetworkSource(known_macs: set[str], scan: Callable[[], Awaitable[set[str]]] | None = None, room: str = "casa", interval: float = 15.0, grace: int = 2, present_confidence: float = 0.8)` implementing `events() -> AsyncIterator[SensingEvent]`. Emits `modality="network"`, `room="casa"`, `motion=0.0`, `breathing_bpm=None`, `heart_bpm=None`. `presence=True` when any known MAC is in the scanned set; stays `True` for up to `grace` consecutive misses (debounce). `confidence = present_confidence` when present else `0.0`. Also produces module fn `arp_scan(known_macs: set[str]) -> Awaitable[set[str]]` (the default real scan: ping-sweep to warm the ARP cache, then parse `arp -a`).

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_network_source.py`

```python
import asyncio
import pytest
from wavr.sources.network import NetworkSource

KNOWN = {"aa:bb:cc:dd:ee:ff"}

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

async def test_network_source_present_when_known_mac_seen():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff", "00:00:00:00:00:01"}
    src = NetworkSource(KNOWN, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.room == "casa"
    assert ev.modality == "network"
    assert ev.presence is True
    assert ev.confidence == 0.8
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.motion == 0.0
    assert ev.ts.endswith("+00:00")

async def test_network_source_absent_when_no_known_mac():
    async def scan():
        return {"00:00:00:00:00:02"}
    src = NetworkSource(KNOWN, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is False
    assert ev.confidence == 0.0

async def test_network_source_grace_holds_presence_across_misses():
    seq = [ {"aa:bb:cc:dd:ee:ff"}, set(), set(), set() ]  # seen, miss, miss, miss
    calls = {"i": 0}
    async def scan():
        i = calls["i"]; calls["i"] += 1
        return seq[min(i, len(seq) - 1)]
    src = NetworkSource(KNOWN, scan=scan, interval=0, grace=2)
    evs = await _first_n(src, 4)
    # tick0 seen -> present; tick1 miss#1 -> still present (grace); tick2 miss#2 -> still present; tick3 miss#3 -> absent
    assert [e.presence for e in evs] == [True, True, True, False]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py -q`
Expected: FAIL — `ImportError: cannot import name 'NetworkSource'`.

- [ ] **Step 3: Write minimal implementation** — append to `backend/wavr/sources/network.py`

Add `import asyncio`, `import contextlib`, `import ipaddress`, `import socket`, `from datetime import datetime, timezone`, `from typing import AsyncIterator, Awaitable, Callable`, and `from wavr.events import SensingEvent` to the top of the file (alongside the existing `import re`), then append:

```python
def _local_ipv4() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the outbound iface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


async def arp_scan() -> set[str]:
    """Default real scan: ping-sweep the local /24 to warm the ARP cache, then
    parse `arp -a`, returning every MAC currently on the LAN. Best-effort — a
    failed ping never raises. Windows-flavored ping flags (`-n 1 -w 200`)."""
    ip = _local_ipv4()
    if ip:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        async def ping(addr: str) -> None:
            with contextlib.suppress(Exception):
                await _run("ping", "-n", "1", "-w", "200", addr)
        await asyncio.gather(*(ping(str(h)) for h in net.hosts()))
    return parse_arp_table(await _run("arp", "-a"))


class NetworkSource:
    """House-level presence from the LAN. Emits room='casa', modality='network'.
    Presence = any known MAC seen; debounced by `grace` consecutive misses so a
    phone briefly dropping off ARP doesn't flap the state."""

    def __init__(self, known_macs: set[str],
                 scan: Callable[[], Awaitable[set[str]]] | None = None,
                 room: str = "casa", interval: float = 15.0,
                 grace: int = 2, present_confidence: float = 0.8):
        self._known = {m.replace("-", ":").lower() for m in known_macs}
        self._scan = scan or arp_scan
        self._room = room
        self._interval = interval
        self._grace = grace
        self._conf = present_confidence
        self._missed = grace + 1  # start "absent" until first sighting

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            try:
                seen = await self._scan()
            except Exception:
                seen = set()
            if self._known & seen:
                self._missed = 0
            else:
                self._missed += 1
            present = self._missed <= self._grace
            yield SensingEvent(
                room=self._room, modality="network", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._conf if present else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
            )
            if self._interval:
                await asyncio.sleep(self._interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py -q`
Expected: PASS (all — 2 parser + 3 source).

- [ ] **Step 5: Add a test for the real scan wiring (monkeypatched subprocess)** — append to `backend/tests/test_network_source.py`

```python
async def test_arp_scan_parses_real_command_output(monkeypatch):
    from wavr.sources import network
    async def fake_run(*args):
        if args[0] == "ping":
            return ""
        return "iface\n  192.168.0.1  AA-BB-CC-DD-EE-FF  dynamic\n"
    monkeypatch.setattr(network, "_run", fake_run)
    monkeypatch.setattr(network, "_local_ipv4", lambda: None)  # skip the ping sweep
    macs = await network.arp_scan()
    assert "aa:bb:cc:dd:ee:ff" in macs
```

- [ ] **Step 6: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_network_source.py -q`
Expected: PASS (6 passed).

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/sources/network.py backend/tests/test_network_source.py
git commit -m "feat: NetworkSource — house-level presence via arp+ping (mockable scan)"
```

---

### Task 3: `RuViewSource` — WiFi CSI WebSocket client source

**Files:**
- Create: `backend/wavr/sources/ruview.py`
- Create: `backend/tests/test_ruview_source.py`
- Modify: `backend/pyproject.toml` (make `websockets` an explicit dependency)

**Interfaces:**
- Consumes: `normalize_ruview(raw: dict, room: str)` (`events.py`); `SensingEvent`; `Config` fields `ruview_url`, `ruview_room`, `ruview_reconnect`.
- Produces: `RuViewSource(url: str, room: str = "sala", connect: Callable[[str], AsyncIterator[dict]] | None = None, reconnect_delay: float = 3.0)` implementing `events() -> AsyncIterator[SensingEvent]`. Connects to the WS, reads JSON frames, keeps only frames with `type == "sensing_update"`, maps each via `normalize_ruview(frame, room)`, and yields. On disconnect/error it sleeps `reconnect_delay` and reconnects — it never propagates a connection error out of `events()` (a dead RuView container must not crash the SourceManager). The `connect` seam yields raw dict frames for one connection lifetime; the default wires the real `websockets` client.

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_ruview_source.py`

```python
import pytest
from wavr.sources.ruview import RuViewSource

FRAME = {
    "type": "sensing_update",
    "classification": {"presence": True, "confidence": 0.43},
    "features": {"motion_band_power": 9.7758},
    "vital_signs": {"breathing_rate_bpm": 9.707, "heart_rate_bpm": 46.22},
    "timestamp": 1782924055.636,
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

async def test_ruview_yields_wifi_csi_events_from_frames():
    async def connect(url):
        for f in (FRAME, FRAME):
            yield f
    src = RuViewSource("ws://x", room="quarto", connect=connect)
    evs = await _first_n(src, 2)
    assert all(e.modality == "wifi_csi" and e.room == "quarto" for e in evs)
    assert evs[0].breathing_bpm == 9.707 and evs[0].confidence == 0.43

async def test_ruview_skips_non_sensing_frames():
    async def connect(url):
        yield {"type": "hello"}          # ignored
        yield FRAME                       # yielded
    src = RuViewSource("ws://x", room="sala", connect=connect)
    [ev] = await _first_n(src, 1)
    assert ev.modality == "wifi_csi"

async def test_ruview_reconnects_after_a_dropped_connection():
    calls = {"n": 0}
    async def connect(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("boom")   # first connection fails
        yield FRAME                          # second connection succeeds
    src = RuViewSource("ws://x", room="sala", connect=connect, reconnect_delay=0)
    [ev] = await _first_n(src, 1)
    assert calls["n"] == 2                   # it retried
    assert ev.presence is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_ruview_source.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.sources.ruview'`.

- [ ] **Step 3: Write minimal implementation** — create `backend/wavr/sources/ruview.py`

```python
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent, normalize_ruview


async def _default_connect(url: str) -> AsyncIterator[dict]:
    """Real WS client: connect and yield decoded JSON frames for the life of the
    connection. `websockets` is a WS client library (present via uvicorn[standard],
    declared explicitly in pyproject)."""
    import websockets  # local import so the module loads even if unused in tests

    async with websockets.connect(url) as ws:
        async for raw in ws:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue


class RuViewSource:
    """WiFi CSI presence + vitals from a RuView sensing WebSocket. Reconnects
    forever on drop so a missing/rebooting container never crashes the manager.
    Maps each 'sensing_update' frame via the shared normalize_ruview()."""

    def __init__(self, url: str, room: str = "sala",
                 connect: Callable[[str], AsyncIterator[dict]] | None = None,
                 reconnect_delay: float = 3.0):
        self._url = url
        self._room = room
        self._connect = connect or _default_connect
        self._delay = reconnect_delay

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            try:
                async for frame in self._connect(self._url):
                    if frame.get("type") == "sensing_update":
                        yield normalize_ruview(frame, self._room)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # connection error: fall through to the reconnect sleep
            if self._delay:
                await asyncio.sleep(self._delay)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_ruview_source.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Declare `websockets` explicitly** — modify `backend/pyproject.toml`

Change the `dependencies` list to add `websockets`:

```toml
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "python-dotenv>=1.0",
  "websockets>=12",
]
```

Then reinstall so the dep is resolved:

```powershell
C:\IA\wavr\.venv\Scripts\python.exe -m pip install -e backend[dev]
```

- [ ] **Step 6: Run test to verify it still passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_ruview_source.py -q`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/sources/ruview.py backend/tests/test_ruview_source.py backend/pyproject.toml
git commit -m "feat: RuViewSource — WiFi CSI over WS with auto-reconnect (mockable connect)"
```

---

### Task 4: Wire real sources into `create_app` + concurrency test

**Files:**
- Modify: `backend/wavr/app.py` (default source list; add a `_default_sources(cfg)` helper)
- Create: `backend/tests/test_sources_concurrency.py`

**Interfaces:**
- Consumes: `NetworkSource` (Task 2), `RuViewSource` (Task 3), `SimulatedSource` (existing), `SourceManager` (existing), `create_app` (existing).
- Produces: `create_app`'s default `sources` becomes `network` (enabled) + `ruview` (enabled) + `sim` (disabled) built from `cfg`. Existing `test_app.py` is unaffected — it passes an explicit `sources=` list to `build_client`, so it does not observe the default.

- [ ] **Step 1: Write the failing concurrency test** — create `backend/tests/test_sources_concurrency.py`

```python
import asyncio
import pytest
from wavr.sourcemanager import SourceManager


class _FakeSource:
    """Emits `label` events on a fixed cadence; `delay` lets one source be slow."""
    def __init__(self, label, delay=0.0):
        self._label = label
        self._delay = delay
    async def events(self):
        while True:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._label


async def test_slow_source_does_not_starve_fast_ones():
    got = []
    async def on_event(ev):
        got.append(ev)
    mgr = SourceManager(on_event)
    mgr.register("fast", lambda: _FakeSource("fast", delay=0.001), True)
    mgr.register("slow", lambda: _FakeSource("slow", delay=0.05), True)
    mgr.register("sim", lambda: _FakeSource("sim", delay=0.001), True)
    await mgr.start()
    await asyncio.sleep(0.1)
    await mgr.stop()
    # All three ran concurrently; the slow one didn't block the fast ones.
    assert "fast" in got and "sim" in got and "slow" in got
    # Fast sources produced many more events than the slow one in the same window.
    assert got.count("fast") > got.count("slow") * 3
    # Global stop cancelled every task.
    assert all(not s["active"] for s in mgr.status()["sources"])
```

- [ ] **Step 2: Run test to verify it fails or passes for the right reason**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sources_concurrency.py -q`
Expected: PASS (this validates the *existing* SourceManager's concurrency — it is a regression guard for the multi-source guarantee, not new production code). If it FAILS, stop: the manager has a real concurrency defect to fix before wiring more sources. Report it.

- [ ] **Step 3: Add the `_default_sources` helper + use it** — modify `backend/wavr/app.py`

Add these imports near the existing source import:

```python
from wavr.sources.simulated import SimulatedSource
from wavr.sources.network import NetworkSource
from wavr.sources.ruview import RuViewSource
```

Add a module-level helper (above `create_app`):

```python
def _default_sources(cfg):
    """Plano A real-source set: network always-on ($0), ruview always-on (harmless
    reconnect loop when the container is absent), sim off by default (toggle it on
    from the dashboard to populate the view when no real data is flowing)."""
    return [
        ("network", lambda: NetworkSource(
            cfg.net_known_macs, interval=cfg.net_interval, grace=cfg.net_grace), True),
        ("ruview", lambda: RuViewSource(
            cfg.ruview_url, room=cfg.ruview_room, reconnect_delay=cfg.ruview_reconnect), True),
        ("sim", lambda: SimulatedSource(interval=cfg.sim_interval), False),
    ]
```

Change the default-source line in `create_app` from:

```python
    for name, factory, enabled in (sources or [("sim", lambda: SimulatedSource(interval=cfg.sim_interval), True)]):
        manager.register(name, factory, enabled)
```

to:

```python
    for name, factory, enabled in (sources if sources is not None else _default_sources(cfg)):
        manager.register(name, factory, enabled)
```

- [ ] **Step 4: Add a test that the default wiring lists the three sources** — append to `backend/tests/test_sources_concurrency.py`

Test the `_default_sources` helper directly (no lifespan, no TestClient) so the assertion never triggers the real `arp -a` / ping sweep or a live WS connect — the factories are inspected, not called:

```python
def test_default_sources_lists_network_ruview_sim(monkeypatch):
    monkeypatch.delenv("WAVR_NET_MACS", raising=False)
    from wavr.config import load_config
    from wavr.app import _default_sources
    srcs = _default_sources(load_config())
    enabled = {name: en for name, factory, en in srcs}
    assert enabled == {"network": True, "ruview": True, "sim": False}
```

- [ ] **Step 5: Run the affected tests + full suite**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sources_concurrency.py backend/tests/test_app.py -q`
Expected: PASS (all).
Then the full suite:
Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`
Expected: PASS (all — Sub-plan A's 34 + the new Sub-plan B tests).

- [ ] **Step 6: Commit**

```powershell
git add backend/wavr/app.py backend/tests/test_sources_concurrency.py
git commit -m "feat: wire NetworkSource+RuViewSource into create_app; concurrency guard"
```

---

## Definition of Done (Sub-plan B)
- [ ] `NetworkSource` detects house-level presence (`room="casa"`, `modality="network"`) from `arp -a` + ping sweep, with MAC-miss debounce; fully unit-tested with an injected scan (no live LAN needed).
- [ ] `RuViewSource` yields `wifi_csi` events from a WS sensing stream via the shared `normalize_ruview`, and auto-reconnects on drop without crashing the manager; unit-tested with an injected connect (no ESP32/container needed).
- [ ] Both sources register in `create_app` by default (network + ruview enabled, sim disabled); existing `test_app.py` unaffected.
- [ ] Concurrency test proves 3 sources run without a slow one starving the fast ones, and global-off cancels all tasks.
- [ ] `fusion.py` untouched; canonical shapes unchanged; no real data path to Plano B.
- [ ] Full suite green.

## Next
Sub-plan C (CameraSource: RTSP + YOLO on the RTX 3060, boot-OFF safety toggle, RTSP release in `finally`). Live hardware validation of NetworkSource (real LAN) and RuViewSource (real ESP32/container) is a manual step once the devices are up — the seams and tests are ready for it.

## Deferred (carried from Sub-plan A final review — revisit here or in C)
- `modality` as a `Literal`/enum instead of `str` (natural now that real modalities exist).
- Per-room network granularity (network stays house-level `"casa"` — do not fold into specific rooms).
- SQLite commit-per-event is synchronous on the event loop; revisit (batch or thread executor) if real-source event volume rises.
