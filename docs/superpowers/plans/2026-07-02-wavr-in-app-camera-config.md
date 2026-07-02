# Wavr — In-app camera configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Manage cameras at runtime from the dashboard — add / list / remove any RTSP camera through the UI, with no code or env change — so any future camera is configurable in-app. Persist camera *definitions* (never an ON state, never frames); cameras still always boot OFF and are toggled on consciously.

**Architecture:** A `CameraStore` (SQLite table in the existing db file, separate from `Storage`'s RoomState concern) holds camera definitions. `create_app` loads them at startup and registers each as a boot-OFF source; new CRUD endpoints (`GET/POST/DELETE /api/cameras`, CSRF+loopback guarded) add/remove them at runtime via `SourceManager.register`/`unregister`. The hardcoded `camera_quarto`/`camera_quintal` defaults are removed — cameras are now fully store-driven. The dashboard gains a camera-management section (live mode only; hidden in Plano B). All I/O mock-tested; no real camera/GPU.

**Tech Stack:** Python 3.11+, FastAPI, sqlite3, asyncio. Frontend single-file HTML/JS.

## Global Constraints

- Platform Windows 11; interpreter `C:\IA\wavr\.venv\Scripts\python.exe`; run from `C:\IA\wavr`.
- SAFETY unchanged: cameras register `enabled=False` (boot OFF); NO ON state persisted — the store holds the *definition* only. Toggling on stays a conscious runtime action via the existing `POST /api/sources/{name}/toggle`. Disable still hard-releases RTSP (Sub-plan C + hardening).
- PRIVACY: only camera *definitions* persist (name/room/rtsp_url/confidence) — never frames. Nothing reaches the public Plano B; the camera-management UI is live-mode only (hidden when `MODE!=="live"`). RTSP URLs contain creds — the `GET` response MUST mask the password; the store keeps the full URL (loopback-only, same exposure class as the current `.env`).
- CSRF+loopback: all state-changing camera endpoints require the `X-Wavr-Local` header (reuse `require_local`) and inherit the loopback bind + Host allowlist.
- `fusion.py` untouched; canonical event shape unchanged; `CameraSource` unchanged (this plan only changes how cameras are *configured/registered*, not how they detect). Files < 500 lines. TDD, DRY, YAGNI.
- **Decisions locked (author):** storage = SQLite table `cameras` (not JSON) — reuses the db file, transactional. No edit endpoint in v1 (edit = delete + re-add). `GET` masks creds. Hardcoded camera defaults removed; `cam_quarto_url`/`cam_quintal_url` config fields deleted (now dead); `cam_interval` (global) + `cam_confidence` (default threshold when the add-form omits one) kept.

**Branch:** `in-app-camera-config` off `master` (A+B+C+hardening merged).

**Existing interfaces:**
- `SourceManager.register(name, factory, enabled=True)` (runtime-safe: spawns if enabled+running), `set_enabled`, `status()`; `_kill(name)` cancels+awaits a task. **No `unregister` yet** — Task 2 adds it.
- `create_app(sources=None, storage=None, hub=None, fusion=None)` — registers `_default_sources(cfg)` unless `sources` given; `require_local` CSRF dep; `_storage = Storage(cfg.db_path)`.
- `CameraSource(room, rtsp_url="", frames=None, detect=None, interval=0.5, confidence=0.0)`.
- Frontend: `renderControls()` (live-only; sets `#controls`), `post(url, body)` helper (adds `X-Wavr-Local`), `MODE` (`"live"` iff localhost).

---

### Task 1: `CameraStore` — SQLite persistence of camera definitions

**Files:**
- Create: `backend/wavr/camera_store.py`
- Create: `backend/tests/test_camera_store.py`

**Interfaces:**
- Produces: `CameraStore(path: str = "wavr.db")` with `add(name, room, rtsp_url, confidence)` (raises `sqlite3.IntegrityError` on duplicate name), `list() -> list[dict]` (sorted by name; each `{name, room, rtsp_url, confidence}`), `get(name) -> dict | None`, `delete(name) -> bool` (True if a row was removed), `close()`.

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_camera_store.py`

```python
import sqlite3
import pytest
from wavr.camera_store import CameraStore

def _store(tmp_path):
    return CameraStore(str(tmp_path / "t.db"))

def test_add_and_list(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://u:p@10.0.0.5/s1", 0.5)
    s.add("cam_quarto", "quarto", "rtsp://u:p@10.0.0.6/s1", 0.4)
    rows = s.list()
    assert [r["name"] for r in rows] == ["cam_quarto", "cam_sala"]   # sorted
    assert rows[1] == {"name": "cam_sala", "room": "sala",
                       "rtsp_url": "rtsp://u:p@10.0.0.5/s1", "confidence": 0.5}

def test_duplicate_name_raises(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://x", 0.5)
    with pytest.raises(sqlite3.IntegrityError):
        s.add("cam_sala", "sala", "rtsp://y", 0.5)

def test_get_and_delete(tmp_path):
    s = _store(tmp_path)
    s.add("cam_sala", "sala", "rtsp://x", 0.5)
    assert s.get("cam_sala")["room"] == "sala"
    assert s.get("missing") is None
    assert s.delete("cam_sala") is True
    assert s.delete("cam_sala") is False   # already gone
    assert s.get("cam_sala") is None

def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "t.db")
    CameraStore(p).add("cam_sala", "sala", "rtsp://x", 0.5)
    assert CameraStore(p).get("cam_sala") is not None   # survived reopen
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'wavr.camera_store'`.

- [ ] **Step 3: Implement** — create `backend/wavr/camera_store.py`

```python
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    name       TEXT PRIMARY KEY,
    room       TEXT NOT NULL,
    rtsp_url   TEXT NOT NULL,
    confidence REAL NOT NULL
);
"""


class CameraStore:
    """Persisted camera DEFINITIONS (name/room/rtsp_url/confidence). Never stores an
    ON state — cameras always boot OFF; this is configuration, not runtime state.
    Never stores frames. Shares the sqlite file with Storage but owns its own table."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, name: str, room: str, rtsp_url: str, confidence: float) -> None:
        self._conn.execute(
            "INSERT INTO cameras (name, room, rtsp_url, confidence) VALUES (?, ?, ?, ?)",
            (name, room, rtsp_url, confidence),
        )
        self._conn.commit()

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence FROM cameras ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> dict | None:
        r = self._conn.execute(
            "SELECT name, room, rtsp_url, confidence FROM cameras WHERE name = ?", (name,)
        ).fetchone()
        return dict(r) if r else None

    def delete(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM cameras WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_store.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (expect 64 + 4 new).

```powershell
git add backend/wavr/camera_store.py backend/tests/test_camera_store.py
git commit -m "feat: CameraStore — SQLite persistence of camera definitions"
```

---

### Task 2: `SourceManager.unregister(name)`

**Files:**
- Modify: `backend/wavr/sourcemanager.py`
- Modify: `backend/tests/test_sourcemanager.py`

**Interfaces:**
- Produces: `async def unregister(self, name: str) -> None` — kills the task if running (`await self._kill(name)`) then removes `name` from `_factories` and `_enabled`. Raises `KeyError` if `name` is unknown. After it, `status()` no longer lists the source.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_sourcemanager.py`

```python
async def test_unregister_removes_source_and_kills_task():
    async def on_event(ev):
        pass
    mgr = SourceManager(on_event)
    mgr.register("cam_x", lambda: FakeSource(), True)   # FakeSource = existing infinite fixture
    await mgr.start()
    assert any(s["name"] == "cam_x" and s["active"] for s in mgr.status()["sources"])
    await mgr.unregister("cam_x")
    names = [s["name"] for s in mgr.status()["sources"]]
    assert "cam_x" not in names                          # gone from the roster
    with pytest.raises(KeyError):
        await mgr.unregister("cam_x")                     # already gone
```

(If the file lacks `import pytest`, add it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sourcemanager.py -q`
Expected: FAIL — `AttributeError: 'SourceManager' object has no attribute 'unregister'`.

- [ ] **Step 3: Implement** — add to `backend/wavr/sourcemanager.py` (after `set_enabled`)

```python
    async def unregister(self, name: str) -> None:
        """Kill the source's task if running and remove it from the roster. Used by
        the in-app camera CRUD to drop a camera at runtime."""
        if name not in self._factories:
            raise KeyError(name)
        await self._kill(name)
        self._factories.pop(name, None)
        self._enabled.pop(name, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_sourcemanager.py -q`
Expected: PASS (existing + new).

- [ ] **Step 5: Full suite + commit**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q`.

```powershell
git add backend/wavr/sourcemanager.py backend/tests/test_sourcemanager.py
git commit -m "feat: SourceManager.unregister — drop a source at runtime"
```

---

### Task 3: Camera CRUD API + startup load + remove hardcoded camera defaults

**Files:**
- Modify: `backend/wavr/app.py`
- Modify: `backend/wavr/config.py` (drop dead `cam_quarto_url`/`cam_quintal_url`)
- Modify: `backend/tests/test_config.py` (drop their assertions)
- Modify: `backend/tests/test_sources_concurrency.py` (cameras no longer in defaults)
- Create: `backend/tests/test_camera_api.py`

**Interfaces:**
- Consumes: `CameraStore` (Task 1), `SourceManager.register`/`unregister` (Task 2), `CameraSource`.
- Produces: `create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None)` builds a `CameraStore`, registers each persisted camera as a boot-OFF source at startup, and exposes: `GET /api/cameras` (list, creds masked), `POST /api/cameras` (add + register), `DELETE /api/cameras/{name}` (remove + unregister). `_default_sources` no longer includes cameras.

- [ ] **Step 1: Write the failing tests** — create `backend/tests/test_camera_api.py`

```python
import pytest
from fastapi.testclient import TestClient
from wavr.app import create_app
from wavr.camera_store import CameraStore

def _client(tmp_path, seed=None):
    store = CameraStore(str(tmp_path / "cams.db"))
    if seed:
        for c in seed:
            store.add(**c)
    app = create_app(
        sources=[],                       # no default sources -> isolate camera behavior
        camera_store=store,
    )
    return TestClient(app, headers={"X-Wavr-Local": "1"})

def test_post_adds_camera_as_boot_off_source(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/cameras", json={"name": "cam_sala", "room": "sala",
                                         "rtsp_url": "rtsp://u:pw@10.0.0.5/s1", "confidence": 0.5})
        assert r.status_code == 200
        sysrc = {s["name"]: s for s in c.get("/api/system").json()["sources"]}
        assert "cam_sala" in sysrc
        assert sysrc["cam_sala"]["enabled"] is False       # SAFETY: boots OFF

def test_get_masks_credentials(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_sala", "room": "sala",
                                  "rtsp_url": "rtsp://user:secret@10.0.0.5/s1", "confidence": 0.5}]) as c:
        [cam] = c.get("/api/cameras").json()
        assert "secret" not in cam["rtsp_url"]             # password never echoed
        assert cam["name"] == "cam_sala" and cam["room"] == "sala"

def test_persisted_cameras_registered_on_startup(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" in sysrc                            # loaded from store at boot

def test_delete_removes_camera(tmp_path):
    with _client(tmp_path, seed=[{"name": "cam_q", "room": "quarto",
                                  "rtsp_url": "rtsp://x", "confidence": 0.4}]) as c:
        assert c.delete("/api/cameras/cam_q").status_code == 200
        sysrc = {s["name"] for s in c.get("/api/system").json()["sources"]}
        assert "cam_q" not in sysrc
        assert c.delete("/api/cameras/cam_q").status_code == 404   # already gone

def test_duplicate_name_rejected(tmp_path):
    with _client(tmp_path) as c:
        body = {"name": "cam_sala", "room": "sala", "rtsp_url": "rtsp://x", "confidence": 0.5}
        assert c.post("/api/cameras", json=body).status_code == 200
        assert c.post("/api/cameras", json=body).status_code == 409   # duplicate

def test_camera_endpoints_require_local_header(tmp_path):
    store = CameraStore(str(tmp_path / "c.db"))
    with TestClient(create_app(sources=[], camera_store=store)) as c:   # no X-Wavr-Local
        r = c.post("/api/cameras", json={"name": "x", "room": "r",
                                         "rtsp_url": "rtsp://x", "confidence": 0.5})
        assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_api.py -q`
Expected: FAIL — `create_app()` has no `camera_store` kwarg / no `/api/cameras` routes.

- [ ] **Step 3: Implement** — modify `backend/wavr/app.py`

Add import: `import sqlite3` (top) and `from wavr.camera_store import CameraStore`.

Add a credential-mask helper and a camera-factory helper (module level, above `create_app`):

```python
def _mask_rtsp(url: str) -> str:
    """Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rtsp://user:***@host."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


def _camera_factory(cam: dict, cfg):
    return lambda: CameraSource(cam["room"], cam["rtsp_url"],
                                interval=cfg.cam_interval, confidence=cam["confidence"])
```

Remove the two `camera_quarto`/`camera_quintal` entries from `_default_sources` (leave network/ruview/sim).

In `create_app`, add the `camera_store` param and build/load it (after `manager` registration of default sources, before `lifespan`):

```python
def create_app(sources=None, storage=None, hub=None, fusion=None, camera_store=None) -> FastAPI:
    cfg = load_config()
    ...
    manager = SourceManager(_ingest)
    for name, factory, enabled in (sources if sources is not None else _default_sources(cfg)):
        manager.register(name, factory, enabled)

    _cameras = camera_store or CameraStore(cfg.db_path)
    for cam in _cameras.list():                       # persisted cameras -> boot-OFF sources
        manager.register(cam["name"], _camera_factory(cam, cfg), False)
    ...
```

Add the endpoints (near the other routes):

```python
    @app.get("/api/cameras")
    async def cameras():
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @app.post("/api/cameras")
    async def add_camera(
        name: str = Body(...), room: str = Body(...),
        rtsp_url: str = Body(...), confidence: float = Body(cfg.cam_confidence),
        _=Depends(require_local),
    ):
        name = name.strip()
        if not name or not room.strip() or not rtsp_url.strip():
            raise HTTPException(status_code=400, detail="name, room, rtsp_url are required")
        if name in {s["name"] for s in manager.status()["sources"]}:
            raise HTTPException(status_code=409, detail=f"source name in use: {name}")
        try:
            _cameras.add(name, room.strip(), rtsp_url.strip(), confidence)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"camera exists: {name}")
        manager.register(name, _camera_factory(_cameras.get(name), cfg), False)  # boots OFF
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]

    @app.delete("/api/cameras/{name}")
    async def delete_camera(name: str, _=Depends(require_local)):
        if not _cameras.delete(name):
            raise HTTPException(status_code=404, detail=f"unknown camera: {name}")
        try:
            await manager.unregister(name)
        except KeyError:
            pass   # not registered (e.g. removed before a restart re-registered it)
        return [{**cam, "rtsp_url": _mask_rtsp(cam["rtsp_url"])} for cam in _cameras.list()]
```

> Note: `POST` registers the camera `enabled=False` even while the manager is running — `SourceManager.register` only spawns when `enabled and _running`, so a boot-OFF camera is never started by registration. Enabling stays a separate conscious `POST /api/sources/{name}/toggle`.

- [ ] **Step 4: Drop dead config** — modify `backend/wavr/config.py` and `backend/tests/test_config.py`

Remove `cam_quarto_url` and `cam_quintal_url` from the `Config` dataclass and their `os.getenv` lines in `load_config()` (they're now unused — cameras come from the store). KEEP `cam_interval` and `cam_confidence`. Remove the two corresponding assertions from `test_config.py`'s camera-defaults test (keep the `cam_interval`/`cam_confidence` assertions).

- [ ] **Step 5: Update the defaults test** — modify `backend/tests/test_sources_concurrency.py`

The `test_default_sources_lists_network_ruview_sim` test asserts the exact default set. Cameras are no longer defaults — update the expected dict to `{"network": True, "ruview": True, "sim": False}` (remove the two camera entries). Keep it a strict equality check.

- [ ] **Step 6: Run tests to verify they pass**

Run: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests/test_camera_api.py backend/tests/test_config.py backend/tests/test_sources_concurrency.py backend/tests/test_app.py -q`
Expected: PASS. Then full suite: `C:\IA\wavr\.venv\Scripts\python.exe -m pytest backend/tests -q` (all green).

- [ ] **Step 7: Commit**

```powershell
git add backend/wavr/app.py backend/wavr/config.py backend/tests/test_camera_api.py backend/tests/test_config.py backend/tests/test_sources_concurrency.py
git commit -m "feat: /api/cameras CRUD + startup load; cameras store-driven (drop hardcoded defaults)"
```

---

### Task 4: Dashboard camera-management UI + Impeccable pass + Plano B redeploy

**Files:**
- Modify: `frontend/index.html`

**Interfaces:**
- Consumes: `GET/POST/DELETE /api/cameras` (Task 3), existing `post()` helper, `MODE`.
- Produces: a live-mode-only "Câmeras" section — an add form (name, room, RTSP URL, confidence) and a list of configured cameras with a Remove button each. Hidden entirely when `MODE!=="live"` (never in Plano B).

- [ ] **Step 1: Add the camera section markup** — in `frontend/index.html`, after the `#controls` div (before `<main>`), add:

```html
<div id="cameras" class="cams" hidden>
  <h3 id="cams-h">Câmeras</h3>
  <form id="camForm" class="cam-form" aria-labelledby="cams-h">
    <input name="name" placeholder="nome (ex: cam_sala)" required aria-label="nome da câmera">
    <input name="room" placeholder="cômodo (ex: sala)" required aria-label="cômodo">
    <input name="rtsp_url" placeholder="rtsp://user:senha@ip:554/stream" required aria-label="URL RTSP">
    <input name="confidence" type="number" step="0.05" min="0" max="1" value="0.4" aria-label="confiança mínima">
    <button type="submit" class="ctl">Adicionar</button>
  </form>
  <div id="camList" class="cam-list"></div>
</div>
```

- [ ] **Step 2: Add CSS** — in the `<style>` block, near `.controls`:

```css
  .cams:not([hidden]){padding:12px 24px;border-bottom:1px solid var(--line);}
  .cam-form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;}
  .cam-form input{background:var(--surface);color:var(--ink);border:1px solid var(--line);
                  border-radius:8px;padding:7px 10px;font-size:.82rem;}
  .cam-form input[name="rtsp_url"]{flex:1;min-width:220px;}
  .cam-list{display:flex;flex-direction:column;gap:6px;}
  .cam-row{display:flex;justify-content:space-between;align-items:center;gap:12px;
           font-size:.82rem;padding:6px 0;border-top:1px dashed var(--line);}
  .cam-row .rm{background:none;border:1px solid var(--line);color:var(--muted);
               border-radius:8px;padding:4px 10px;cursor:pointer;font-size:.75rem;}
  .cam-row .rm:hover{border-color:var(--warn);color:var(--warn);}
```

- [ ] **Step 3: Add the JS** — inside the `renderControls()` live-only path (or a sibling `renderCameras()` called when `MODE==="live"`), after the existing controls wiring. Use a `renderCameras()` function and call it from the live branch:

```javascript
async function renderCameras(){
  if(MODE!=="live") return;                 // live-only; never in Plano B
  document.getElementById("cameras").hidden = false;
  const post = (url,body)=> fetch(location.origin+url,{method:"POST",
    headers:{"Content-Type":"application/json","X-Wavr-Local":"1"}, body:JSON.stringify(body)});
  async function refresh(){
    let cams; try{ cams = await (await fetch(location.origin+"/api/cameras")).json(); }catch{ return; }
    const list = document.getElementById("camList");
    list.innerHTML = cams.length ? "" : `<div class="empty">nenhuma câmera configurada</div>`;
    cams.forEach(c=>{
      const row = document.createElement("div"); row.className="cam-row";
      row.innerHTML = `<span><b>${c.name}</b> · ${c.room} · <span class="conf">${c.rtsp_url}</span></span>`;
      const rm = document.createElement("button"); rm.className="rm"; rm.textContent="Remover";
      rm.onclick = async ()=>{ await fetch(location.origin+`/api/cameras/${encodeURIComponent(c.name)}`,
        {method:"DELETE", headers:{"X-Wavr-Local":"1"}}).catch(()=>{}); refresh(); };
      row.appendChild(rm); list.appendChild(row);
    });
  }
  document.getElementById("camForm").onsubmit = async (e)=>{
    e.preventDefault();
    const f = e.target;
    await post("/api/cameras", {
      name: f.name.value, room: f.room.value, rtsp_url: f.rtsp_url.value,
      confidence: parseFloat(f.confidence.value),
    }).catch(()=>{});
    f.reset(); f.confidence.value = "0.4"; refresh();
  };
  refresh();
}
renderCameras();
```

(Add the `renderCameras()` call next to the existing `renderControls()` call.)

- [ ] **Step 4: Mandatory Impeccable design pass (project rule)**

Run `/impeccable polish frontend/index.html` then `/impeccable audit frontend/index.html`. Fix contrast/spacing/hierarchy findings on the new camera section (the form inputs, list rows, remove buttons must match the dark token system and meet WCAG AA — placeholder contrast, focus states, touch targets). Do NOT restyle the existing dashboard beyond consistency fixes the audit flags.

- [ ] **Step 5: Verify live + Plano B (Playwright MCP)**

Start the backend (`C:\IA\wavr\.venv\Scripts\python.exe -m uvicorn wavr.app:create_app --factory --port 8000` from `backend/`, background). With the Playwright MCP browser:
- **Live** (`http://127.0.0.1:8000/`): the Câmeras section is visible; add a camera via the form → it appears in the list AND as a boot-OFF toggle in the control panel (`sim`-style); Remove → it disappears from both. Screenshot to `.superpowers/sdd/task-4-cameras.png`.
- **Plano B** (open the committed `frontend/index.html` as a non-localhost origin via a `data:` URL, as in Sub-plan A/C): confirm the Câmeras section is HIDDEN (`#cameras` stays `hidden`), mode label shows demo, and `browser_network_requests` shows ZERO requests to localhost/`/api/cameras`.
Kill the server + close the browser when done.

- [ ] **Step 6: Redeploy Plano B**

```powershell
npx wrangler pages deploy frontend --project-name wavr --commit-dirty=true
```
Then confirm the deployed `*.pages.dev` URL still shows demo mode with the camera section hidden and no localhost/API calls (Playwright, as in Sub-plan C).

- [ ] **Step 7: Commit**

```powershell
git add frontend/index.html
git commit -m "feat: in-app camera management UI (live-only) + Impeccable polish; redeploy Plano B"
```

---

## Definition of Done
- [ ] Cameras are added/listed/removed from the dashboard at runtime; each new camera registers as a boot-OFF source with no code/env change; removal unregisters + kills any task.
- [ ] Camera definitions persist in SQLite and are re-registered (boot-OFF) on restart; no ON state ever persists.
- [ ] `GET /api/cameras` masks RTSP credentials; all mutations require `X-Wavr-Local` + loopback.
- [ ] Hardcoded `camera_quarto`/`camera_quintal` removed; `_default_sources` is network/ruview/sim only; dead `cam_*_url` config removed.
- [ ] Camera UI is live-only, hidden in Plano B; Impeccable pass done; Plano B redeployed and verified credential-free + camera-section-hidden.
- [ ] `fusion.py`/`CameraSource` untouched; full suite green.

## Next
Live hardware bring-up: add a real camera in-app, toggle it on, confirm detection on the RTX 3060. The same in-app pattern generalizes to other sources later (a RuView add-form, etc.) via the SensorSource seam.
