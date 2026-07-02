# Wavr — Surface Camadas 3+4 in the dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the (backend-only) away mode + AI narration visible in the dashboard — a **house home/away indicator** (computed client-side from the RoomState stream, works in both live and demo) and a **"Narrar" button** (live-only) that calls `POST /api/narrate` and shows the AI summary. Pure frontend; no backend change.

**Architecture:** `frontend/index.html` already receives per-room `RoomState` via WS (live) or the simulator (demo). The house indicator derives home/away = "any tracked room occupied" from those — no new endpoint. The Narrar button POSTs to the existing `/api/narrate` (needs the `X-Wavr-Local` header) and renders the returned `{narration}` text, handling 503 (not configured) / 502 (backend error) gracefully. Narrar is live-only (hidden in Plano B, like the other backend controls); the house indicator shows in both modes.

**Tech Stack:** Single-file HTML/CSS/JS, existing dark token system.

## Global Constraints

- Platform Windows 11; run from `C:\IA\wavr`.
- PRIVACY: the house indicator is computed from RoomState the dashboard already has — no new data. The Narrar button is live-only and hidden in Plano B (`MODE!=="live"`), so the public demo never calls `/api/narrate` (which is the cloud egress). Verify Plano B shows NO Narrar button and makes NO `/api/narrate` request.
- Reuse the existing dark tokens (`--bg/--surface/--ink/--muted/--accent/--warn/--line`), the `post()` CSRF helper pattern (`X-Wavr-Local: 1`), the `MODE` gate, and the `:focus-visible` / reduced-motion conventions already in the file.
- Only `frontend/index.html` touched (this is a static change — no pytest). `fusion.py`/backend untouched.
- Files < 500 lines; DRY.

**Branch:** `frontend-surface-c3c4` off `master` (all layers merged).

**Existing structure in `frontend/index.html`:**
- `header` with `<h1>Wavr</h1>` + `#mode` badge.
- `#controls` (live-only) + `#cameras` (live-only) sections.
- `MODE` = `"live"` iff localhost; `renderControls()` / `renderCameras()` are live-only and use a `post(url, body)` helper that adds `X-Wavr-Local`.
- The data path: `handle(rs)` → `upsert(rs)` + `pushTimeline(rs)` for each RoomState; `cards` dict holds per-room card elements. RoomState has `{room, occupied, confidence, ...}`.

---

### Task 1: House indicator + Narrar button (frontend only)

**Files:**
- Modify: `frontend/index.html`

**Interfaces:**
- Consumes: the RoomState stream (both providers), `POST /api/narrate` (live).
- Produces: a header house-state badge updated on every RoomState; a live-only "Narrar" button + narration panel.

- [ ] **Step 1: House indicator markup** — in `frontend/index.html`, add a house badge to the header. Change the header to:

```html
<header><h1>Wavr</h1>
  <div class="hdr-right">
    <span id="house" class="house" aria-live="polite"></span>
    <div class="mode" id="mode"></div>
  </div>
</header>
```

- [ ] **Step 2: Narration section markup** — after `#cameras` (before `<main>`), add a live-only narration block:

```html
<div id="narrate" class="narrate" hidden>
  <button id="narrateBtn" class="ctl">Narrar</button>
  <p id="narrateOut" class="narrate-out" aria-live="polite"></p>
</div>
```

- [ ] **Step 3: CSS** — in the `<style>` block (near `.mode`/`.controls`), add:

```css
  .hdr-right{display:flex;align-items:center;gap:14px;}
  .house{font-size:.8rem;font-weight:600;padding:4px 12px;border-radius:999px;border:1px solid var(--line);}
  .house.home{color:var(--accent);border-color:var(--accent);}
  .house.away{color:var(--muted);}
  .narrate:not([hidden]){display:flex;align-items:flex-start;gap:14px;padding:12px 24px;border-bottom:1px solid var(--line);}
  .narrate-out{margin:0;font-size:.9rem;color:var(--ink);max-width:70ch;line-height:1.5;}
  .narrate-out.muted{color:var(--muted);}
```

- [ ] **Step 4: House indicator JS** — track per-room occupancy and update the badge on every RoomState. Add near the top of the render logic (after `MODE` is set), a house-state updater, and call it inside `handle(rs)`:

```javascript
const houseEl = document.getElementById("house");
const roomOcc = {};
function updateHouse(rs){
  roomOcc[rs.room] = !!rs.occupied;
  const home = Object.values(roomOcc).some(Boolean);
  houseEl.textContent = home ? "casa: alguém em casa" : "casa: vazia";
  houseEl.className = "house " + (home ? "home" : "away");
}
```

Then, in the existing `handle` function (currently `const handle = (rs)=>{ upsert(rs); pushTimeline(rs); };`), add `updateHouse(rs);`:

```javascript
const handle = (rs)=>{ upsert(rs); pushTimeline(rs); updateHouse(rs); };
```

- [ ] **Step 5: Narrar button JS** — live-only, calls `POST /api/narrate`. Add a `renderNarrate()` gated on live and call it next to `renderControls()`/`renderCameras()`:

```javascript
function renderNarrate(){
  if(MODE!=="live") return;                 // narration hits the backend/cloud; never in Plano B
  document.getElementById("narrate").hidden = false;
  const btn = document.getElementById("narrateBtn");
  const out = document.getElementById("narrateOut");
  btn.onclick = async ()=>{
    btn.disabled = true; out.className = "narrate-out muted"; out.textContent = "gerando resumo…";
    try{
      const r = await fetch(location.origin+"/api/narrate",{method:"POST",headers:{"X-Wavr-Local":"1"}});
      if(r.status===503){ out.textContent = "narração não configurada (defina GEMINI_API_KEY + WAVR_NARRATE_ENABLED)"; }
      else if(!r.ok){ out.textContent = "erro ao gerar narração"; }
      else { const d = await r.json(); out.className = "narrate-out"; out.textContent = d.narration || "(sem resposta)"; }
    }catch{ out.textContent = "falha de conexão"; }
    finally{ btn.disabled = false; }
  };
}
renderNarrate();
```

- [ ] **Step 6: Sanity check + commit**

This is static HTML — no pytest. Sanity-check the JS parses (`node --check` on a temp copy, or careful eyeball). Confirm: house badge updates on RoomState (both modes); Narrar section is `hidden` unless `MODE==="live"`; Narrar uses the `X-Wavr-Local` header; 503/502 handled.

```powershell
git add frontend/index.html
git commit -m "feat: dashboard surfaces house home/away + Narrar (AI narration) button"
```

> The Impeccable design pass (`/polish` + `/audit`), Playwright verification (live: house badge + Narrar works; Plano B: no Narrar button, no /api/narrate call, house badge still shows), and Cloudflare redeploy are run by the controller in the main thread after this task — NOT by the implementer.

---

## Definition of Done
- [ ] House home/away badge in the header, updated on every RoomState (any-room-occupied = home), works in both live and demo.
- [ ] Live-only "Narrar" button calls `POST /api/narrate` with the CSRF header, renders the narration, handles 503 (unconfigured) and 502/errors gracefully.
- [ ] Narrar is hidden in Plano B; the public demo makes NO `/api/narrate` request (privacy: the cloud egress is never reachable from the demo).
- [ ] Dark tokens reused; reduced-motion/focus conventions respected; only `frontend/index.html` changed.

## Next
Deploy Fase 1 (Dockerize) — separate plan.
