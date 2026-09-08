// ==========================================================================
// cameras.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Camera management (Plano A / live only) ----
async function renderCameras(){
  if(MODE!=="live") return;                 // live-only; never in Plano B
  document.getElementById("cameras").hidden = false;
  const camFb = document.getElementById("camFb");
  const post = (url,body)=> WavrAPI.fetch(url, {method: "POST", json: body});
  const putJson = (url,body)=> WavrAPI.fetch(url, {method: "PUT", json: body});
  const del = (url)=> WavrAPI.fetch(url, {method: "DELETE"});
  // ---- PTZ d-pad (A4.3): ONVIF pan/tilt/zoom over the SAME require-local channel. Built per
  // camera only when GET /api/ptz/{name}/capabilities returns {ptz:true}. Hold-to-move with a
  // ~1s keepalive so the server's 2s auto-stop only fires when the client actually goes silent.
  // textContent-only (XSS-safe); same-origin fetch only; 44px targets + aria labels.
  const PTZ_KEEPALIVE_MS = 1000;
  const PTZ_KEYVEC = {ArrowUp:{pan:0,tilt:1}, ArrowDown:{pan:0,tilt:-1},
                      ArrowLeft:{pan:-1,tilt:0}, ArrowRight:{pan:1,tilt:0}};
  let ptzCleanups = [];   // stopMove()s of live pads; drained before refresh() rebuilds the list
  let calibCleanups = []; // object-URL revokers of open calibration panels; drained on rebuild
  const errText = async (r)=>{ try{ return r ? ((await r.json()).detail || "") : ""; }catch{ return ""; } };
  function buildPtzPad(name){
    const enc = encodeURIComponent(name);
    const wrap = document.createElement("div");
    wrap.className = "ptz-wrap";
    wrap.setAttribute("role","group");
    wrap.setAttribute("aria-label", WavrT("Pan, tilt and zoom for {name}", {name: name}));
    let timer = null, active = false;          // one hold at a time; guard re-entrancy
    const sendMove = v => post(`/api/ptz/${enc}/move`,
      {pan:v.pan||0, tilt:v.tilt||0, zoom:v.zoom||0}).catch(()=>{});   // LAN may blip; keepalive re-syncs
    const sendStop = () => post(`/api/ptz/${enc}/stop`, {}).catch(()=>{});
    function startMove(v){
      if(active) return; active = true;
      sendMove(v); timer = setInterval(()=> sendMove(v), PTZ_KEEPALIVE_MS);
    }
    function stopMove(){
      if(timer){ clearInterval(timer); timer = null; }
      if(active){ active = false; sendStop(); }
    }
    ptzCleanups.push(stopMove);   // if the list rebuilds mid-hold, refresh() clears our keepalive
    // 3x3 d-pad: ▲ ◀ ■(stop) ▶ ▼
    const pad = document.createElement("div"); pad.className = "ptz-pad";
    // The aria label is translated HERE, as a literal: this array is rebuilt on every
    // buildPtzPad() call, so WavrT() at the definition is the render site — and a literal
    // is the only form the catalogue's completeness check can see.
    const dirs = [
      {cls:"ptz-btn-up",    glyph:"▲", aria:WavrT("Tilt up"),    vec:{pan:0,tilt:1}},
      {cls:"ptz-btn-left",  glyph:"◀", aria:WavrT("Pan left"),   vec:{pan:-1,tilt:0}},
      {cls:"ptz-btn-right", glyph:"▶", aria:WavrT("Pan right"),  vec:{pan:1,tilt:0}},
      {cls:"ptz-btn-down",  glyph:"▼", aria:WavrT("Tilt down"),  vec:{pan:0,tilt:-1}},
    ];
    dirs.forEach(d=>{
      const b = document.createElement("button");
      b.type = "button"; b.className = d.cls; b.textContent = d.glyph;
      b.setAttribute("aria-label", d.aria);
      b.addEventListener("pointerdown", e=>{ e.preventDefault();
        try{ b.setPointerCapture(e.pointerId); }catch{} startMove(d.vec); });
      ["pointerup","pointerleave","pointercancel","blur"].forEach(ev=>
        b.addEventListener(ev, stopMove));
      pad.appendChild(b);
    });
    const stopBtn = document.createElement("button");
    stopBtn.type = "button"; stopBtn.className = "ptz-btn-stop"; stopBtn.textContent = "■";
    stopBtn.setAttribute("aria-label", WavrT("Stop"));
    stopBtn.addEventListener("click", stopMove);
    pad.appendChild(stopBtn);
    // Keyboard: arrow keys move while any pad button is focused (keydown auto-repeat is a no-op
    // because startMove guards on `active`); keyup stops. preventDefault keeps the page from scrolling.
    pad.addEventListener("keydown", e=>{ const v = PTZ_KEYVEC[e.key];
      if(v){ e.preventDefault(); startMove(v); } });
    pad.addEventListener("keyup", e=>{ if(PTZ_KEYVEC[e.key]){ e.preventDefault(); stopMove(); } });
    wrap.appendChild(pad);
    // Aux column: zoom +/- (hold-to-move) and a presets menu.
    const aux = document.createElement("div"); aux.className = "ptz-aux";
    const zoom = document.createElement("div"); zoom.className = "ptz-zoom";
    [{glyph:"+", aria:WavrT("Zoom in"), vec:{zoom:0.5}}, {glyph:"−", aria:WavrT("Zoom out"), vec:{zoom:-0.5}}]
      .forEach(z=>{
        const b = document.createElement("button");
        b.type = "button"; b.textContent = z.glyph; b.setAttribute("aria-label", z.aria);
        b.addEventListener("pointerdown", e=>{ e.preventDefault();
          try{ b.setPointerCapture(e.pointerId); }catch{} startMove(z.vec); });
        ["pointerup","pointerleave","pointercancel","blur"].forEach(ev=>
          b.addEventListener(ev, stopMove));
        zoom.appendChild(b);
      });
    aux.appendChild(zoom);
    const sel = document.createElement("select");
    sel.className = "ptz-presets"; sel.hidden = true;
    sel.setAttribute("aria-label", WavrT("Presets for {name}", {name: name}));
    const ph = document.createElement("option"); ph.value = ""; ph.textContent = WavrT("Presets…");
    sel.appendChild(ph);
    sel.onchange = ()=>{ if(!sel.value) return;
      post(`/api/ptz/${enc}/preset/${encodeURIComponent(sel.value)}`, {}).catch(()=>{});
      sel.value = ""; };
    (async ()=>{
      let presets; try{ presets = await (await WavrAPI.fetch(`/api/ptz/${enc}/presets`)).json(); }catch{ return; }
      if(!Array.isArray(presets) || !presets.length) return;
      presets.forEach(p=>{ if(!p || !p.token) return;
        const o = document.createElement("option");
        o.value = p.token; o.textContent = p.name || p.token;   // textContent: preset names are camera-supplied
        sel.appendChild(o); });
      sel.hidden = false;
    })();
    aux.appendChild(sel);
    wrap.appendChild(aux);
    return wrap;
  }
  // ---- Spec A: per-camera person-localization calibration panel -------------------------
  // Writes PUT /api/cameras/{name}/calibration (the endpoints the localization phase added).
  // Two composable calibrations: a monocular MOUNT prior (immediate approximate estimate) and
  // an accurate 4-POINT homography (solved SERVER-SIDE from image<->floor correspondences —
  // the client never sends a matrix). Marking uses a TRANSIENT still the operator loads
  // LOCALLY: an object URL held in the browser, revoked on clear/rebuild, NEVER uploaded or
  // persisted (ADR-0002). All DOM via createElement + textContent (XSS-safe).
  function buildCalibPanel(name, liveness){
    const enc = encodeURIComponent(name);
    const wrap = document.createElement("div");
    wrap.className = "calib-wrap";
    wrap.setAttribute("role","group");
    wrap.setAttribute("aria-label", WavrT("Localization calibration for {name}", {name: name}));
    const status = document.createElement("p"); status.className = "calib-status";
    status.textContent = WavrT("Loading calibration…");
    const fb = document.createElement("p"); fb.className = "calib-fb"; fb.setAttribute("role","status");
    const setFb = (ok, msg)=>{ fb.className = "calib-fb " + (ok?"ok":"err"); fb.textContent = msg; };
    wrap.appendChild(status);

    // -- Section 1: monocular mount prior (approximate immediate estimate) --
    const s1 = document.createElement("div"); s1.className = "calib-sec";
    const h1 = document.createElement("h4"); h1.textContent = WavrT("Quick estimate (mount position)");
    const p1 = document.createElement("p"); p1.className = "hint";
    p1.textContent = WavrT("Approximate: drop the camera on the map and give its height and downward tilt. "
      + "The person is placed on the floor right away as a hazy dot — an estimate, not exact.");
    s1.appendChild(h1); s1.appendChild(p1);
    const grid = document.createElement("div"); grid.className = "calib-grid";
    // Same rule as the PTZ pad above: the label is translated at the definition, which is
    // inside this per-render builder, so the literal is both the render site and the thing
    // the catalogue's completeness check can actually see.
    const mountFields = [
      {k:"pos_x",    label:WavrT("X on map (m)")},
      {k:"pos_y",    label:WavrT("Y on map (m)")},
      {k:"height",   label:WavrT("Height (m)")},
      {k:"tilt_deg", label:WavrT("Down tilt (°)")},
      {k:"yaw_deg",  label:WavrT("Facing (°)")},
      {k:"hfov_deg", label:WavrT("Field of view (°)")},
    ];
    const mountInputs = {};
    mountFields.forEach(f=>{
      const l = document.createElement("label");
      const span = document.createElement("span"); span.textContent = f.label;
      const inp = document.createElement("input"); inp.type = "number"; inp.step = "0.1";
      inp.setAttribute("aria-label", WavrT("{label} for {name}", {label: f.label, name: name}));
      mountInputs[f.k] = inp; l.appendChild(span); l.appendChild(inp); grid.appendChild(l);
    });
    s1.appendChild(grid);
    const b1 = document.createElement("div"); b1.className = "calib-btns";
    const saveMount = document.createElement("button"); saveMount.type = "button";
    saveMount.textContent = WavrT("Save estimate");
    b1.appendChild(saveMount); s1.appendChild(b1);
    wrap.appendChild(s1);
    saveMount.onclick = async ()=>{
      const mount = {};
      for(const k in mountInputs){
        const v = parseFloat(mountInputs[k].value);
        if(!isFinite(v)){ setFb(false, WavrT("fill every mount field with a number")); return; }
        mount[k] = v;
      }
      let r; try{ r = await putJson(`/api/cameras/${enc}/calibration`, {mount}); }catch{}
      if(r && r.ok){ setFb(true, WavrT("estimate saved — re-enable the camera to apply")); loadStatus(); }
      else { setFb(false, (await errText(r)) || WavrT("couldn't save the estimate")); }
    };

    // -- Section 2: accurate 4-point homography --
    const s2 = document.createElement("div"); s2.className = "calib-sec";
    const h2 = document.createElement("h4"); h2.textContent = WavrT("Precise (mark 4 floor points)");
    const p2 = document.createElement("p"); p2.className = "hint";
    p2.textContent = WavrT("Accurate: load a still from this camera (stays on this device, never uploaded), "
      + "then click 4+ points on the floor and type where each is on your map, in metres. "
      + "Use the camera's full frame at its stream resolution for the best fit.");
    s2.appendChild(h2); s2.appendChild(p2);
    const resGrid = document.createElement("div"); resGrid.className = "calib-grid";
    const mkRes = (label, val)=>{   // `label` arrives already translated (a WavrT literal below)
      const l = document.createElement("label");
      const s = document.createElement("span"); s.textContent = label;
      const inp = document.createElement("input"); inp.type = "number"; inp.step = "1"; inp.min = "1";
      inp.value = val; inp.setAttribute("aria-label", WavrT("{label} for {name}", {label: label, name: name}));
      l.appendChild(s); l.appendChild(inp); resGrid.appendChild(l); return inp;
    };
    const imgWInp = mkRes(WavrT("Stream width (px)"), "1920");
    const imgHInp = mkRes(WavrT("Stream height (px)"), "1080");
    s2.appendChild(resGrid);
    const fileBtns = document.createElement("div"); fileBtns.className = "calib-btns";
    const fileInput = document.createElement("input"); fileInput.type = "file"; fileInput.accept = "image/*";
    fileInput.style.display = "none"; fileInput.setAttribute("aria-hidden","true");
    const loadBtn = document.createElement("button"); loadBtn.type = "button"; loadBtn.textContent = WavrT("Load still…");
    const clearImgBtn = document.createElement("button"); clearImgBtn.type = "button"; clearImgBtn.textContent = WavrT("Clear still");
    fileBtns.appendChild(loadBtn); fileBtns.appendChild(clearImgBtn); fileBtns.appendChild(fileInput);
    s2.appendChild(fileBtns);
    const stage = document.createElement("div"); stage.className = "calib-stage";
    stage.setAttribute("role","application");
    stage.setAttribute("aria-label", WavrT("Mark floor points for {name}", {name: name}));
    s2.appendChild(stage);
    let stageImg = null, objUrl = null;
    const revoke = ()=>{ if(objUrl){ URL.revokeObjectURL(objUrl); objUrl = null; } };
    calibCleanups.push(revoke);
    loadBtn.onclick = ()=> fileInput.click();
    fileInput.onchange = ()=>{
      const f = fileInput.files && fileInput.files[0]; if(!f) return;
      revoke(); objUrl = URL.createObjectURL(f);
      if(!stageImg){ stageImg = document.createElement("img"); stageImg.alt = "";
        stage.insertBefore(stageImg, stage.firstChild); }
      stageImg.src = objUrl;
      fileInput.value = "";   // frame lives ONLY in the object URL; nothing written to disk
    };
    clearImgBtn.onclick = ()=>{ revoke(); if(stageImg){ stageImg.remove(); stageImg = null; } };
    const pts = [];
    const ptsList = document.createElement("div"); ptsList.className = "calib-pts";
    s2.appendChild(ptsList);
    const renumber = ()=> pts.forEach((p,i)=>{ p.mark.textContent = String(i+1); p.idx.textContent = String(i+1); });
    stage.addEventListener("pointerdown", (e)=>{
      const rect = stage.getBoundingClientRect();
      if(rect.width<=0 || rect.height<=0) return;
      const fx = Math.min(Math.max((e.clientX-rect.left)/rect.width, 0), 1);
      const fy = Math.min(Math.max((e.clientY-rect.top)/rect.height, 0), 1);
      const mark = document.createElement("div"); mark.className = "calib-mark";
      mark.style.left = (fx*100)+"%"; mark.style.top = (fy*100)+"%";
      stage.appendChild(mark);
      const row = document.createElement("div"); row.className = "calib-pt";
      const idx = document.createElement("span"); idx.className = "idx";
      const arrow = document.createElement("span"); arrow.textContent = WavrT("→ floor");
      const inX = document.createElement("input"); inX.type = "number"; inX.step = "0.1";
      inX.placeholder = WavrT("x m"); inX.setAttribute("aria-label",WavrT("floor x metres"));
      const inY = document.createElement("input"); inY.type = "number"; inY.step = "0.1";
      inY.placeholder = WavrT("y m"); inY.setAttribute("aria-label",WavrT("floor y metres"));
      const rmpt = document.createElement("button"); rmpt.type = "button"; rmpt.className = "rmpt";
      rmpt.textContent = "✕"; rmpt.setAttribute("aria-label",WavrT("remove point"));
      row.appendChild(idx); row.appendChild(arrow); row.appendChild(inX); row.appendChild(inY); row.appendChild(rmpt);
      ptsList.appendChild(row);
      const p = {fx, fy, mark, idx, inX, inY, row};
      pts.push(p);
      rmpt.onclick = ()=>{ const i = pts.indexOf(p); if(i>=0) pts.splice(i,1); mark.remove(); row.remove(); renumber(); };
      renumber();
    });
    const b2 = document.createElement("div"); b2.className = "calib-btns";
    const saveH = document.createElement("button"); saveH.type = "button"; saveH.textContent = WavrT("Save precise calibration");
    const clearPts = document.createElement("button"); clearPts.type = "button"; clearPts.textContent = WavrT("Clear points");
    b2.appendChild(saveH); b2.appendChild(clearPts);
    s2.appendChild(b2);
    wrap.appendChild(s2);
    clearPts.onclick = ()=>{ pts.splice(0).forEach(p=>{ p.mark.remove(); p.row.remove(); }); };
    saveH.onclick = async ()=>{
      const imgW = parseInt(imgWInp.value,10), imgH = parseInt(imgHInp.value,10);
      if(!(imgW>0 && imgH>0)){ setFb(false, WavrT("set the stream width and height in pixels")); return; }
      const image_points = [], floor_points = [];
      for(const p of pts){
        const x = parseFloat(p.inX.value), y = parseFloat(p.inY.value);
        if(!isFinite(x) || !isFinite(y)) continue;   // skip points with no floor coord yet
        image_points.push([p.fx*imgW, p.fy*imgH]);
        floor_points.push([x, y]);
      }
      if(image_points.length < 4){ setFb(false, WavrT("need 4+ points with a floor position filled in")); return; }
      let r; try{ r = await putJson(`/api/cameras/${enc}/calibration`,
        {image_points, floor_points, img_w:imgW, img_h:imgH}); }catch{}
      if(r && r.ok){ setFb(true, WavrT("precise calibration saved — re-enable the camera to apply")); loadStatus(); }
      else { setFb(false, (await errText(r)) || WavrT("couldn't save (points may be collinear/coincident)")); }
    };

    // -- Section 3: GUIDED WALK-TO-CALIBRATE (Spec A, the standard setup flow) --
    // The person WALKS to known floor spots (room centre, each corner); at each spot the
    // camera detects their FEET PIXEL (bottom-centre of the YOLO box) and the wizard pairs
    // it with that spot's known metre coordinate. 4+ pairs -> an accurate homography solved
    // SERVER-SIDE (PUT /calibration). No snapshot is ever taken — the body IS the marker
    // (ADR-0002): GET calib-sample returns ONLY a pixel coordinate + image dims, never a
    // frame. All DOM via createElement + textContent (XSS-safe).
    const s3 = document.createElement("div"); s3.className = "calib-sec";
    const h3 = document.createElement("h4"); h3.textContent = WavrT("Calibrate by walking (recommended)");
    const p3 = document.createElement("p"); p3.className = "hint";
    p3.textContent = WavrT("Most accurate and no snapshot needed: Wavr guides you to a few known spots "
      + "(the room centre and each corner). Stand on each, facing the camera, and capture — your "
      + "feet mark the floor. No image is taken or saved; only the point where you stand is used.");
    s3.appendChild(h3); s3.appendChild(p3);

    const walkStart = document.createElement("div"); walkStart.className = "calib-btns";
    const startWalk = document.createElement("button"); startWalk.type = "button";
    startWalk.textContent = WavrT("Calibrate by walking");
    walkStart.appendChild(startWalk); s3.appendChild(walkStart);

    // Camera-disabled gate: a guided walk can only ever see the walker while this camera's
    // source is actually running, which SourceManager only spawns while Wavr's monitoring
    // ('running') is on (see calib-session's "MAY run the camera" comment in app.py) — Wavr
    // NEVER turns monitoring on for the operator. This surfaces that honestly BEFORE the
    // button is pressed (GET /api/system, read-only), instead of only after a failed start.
    // `liveness` (the snapshot from the camera list this panel was opened from) seeds a
    // non-blocking hint for privacy/offline right away; the async check below is the
    // authoritative gate and can disable the button outright.
    const gate = document.createElement("p"); gate.className = "hint"; gate.hidden = true;
    gate.setAttribute("role","status");
    s3.insertBefore(gate, walkStart);
    if(liveness === "privacy"){
      gate.hidden = false; gate.style.color = "var(--dim)";
      gate.textContent = WavrT("This camera's lens is covered right now (privacy mode) — Wavr won't see you walk until it's uncovered.");
    } else if(liveness === "offline"){
      gate.hidden = false; gate.style.color = "var(--warn)";
      gate.textContent = WavrT("This camera isn't sending frames right now — check its connection before calibrating.");
    }
    (async ()=>{
      let sys; try{ sys = await (await WavrAPI.fetch("/api/system")).json(); }catch{ return; }
      if(!(sys && sys.running)){
        gate.hidden = false; gate.style.color = "var(--warn)";
        gate.textContent = WavrT("Monitoring is off, so this camera can't turn on to calibrate — "
          + "turn Wavr's monitoring on first, then come back. Wavr never enables it for you.");
        startWalk.disabled = true;
      }
    })();

    // Live wizard sub-panel (hidden until started).
    const wiz = document.createElement("div"); wiz.className = "calib-sec"; wiz.hidden = true;
    wiz.style.borderTop = "0"; wiz.style.paddingTop = "0";
    // Mini floor plan: the sequence of KNOWN spots to scale (centre + corners), so "stand
    // here" is a point on a drawn plan, not only a sentence. Pure geometry from calib-spots
    // (floor metres) -- no frame, no photo (ADR-0002).
    const walkmap = document.createElementNS(SVG_NS, "svg");
    walkmap.setAttribute("class", "calib-walkmap");
    walkmap.setAttribute("viewBox", "0 0 100 100");
    walkmap.setAttribute("role", "img");
    walkmap.setAttribute("aria-label", WavrT("Known floor spots to walk to for {name}", {name: name}));
    const live = document.createElement("p"); live.className = "calib-status";
    live.setAttribute("role","status"); live.setAttribute("aria-live","polite");
    const step = document.createElement("p"); step.className = "hint"; step.style.color = "var(--text)";
    const prog = document.createElement("p"); prog.className = "hint";
    wiz.appendChild(walkmap); wiz.appendChild(live); wiz.appendChild(step); wiz.appendChild(prog);
    const wizBtns = document.createElement("div"); wizBtns.className = "calib-btns";
    const capBtn = document.createElement("button"); capBtn.type = "button"; capBtn.textContent = WavrT("Capture this spot");
    const retryBtn = document.createElement("button"); retryBtn.type = "button"; retryBtn.textContent = WavrT("Retry last spot");
    const solveBtn = document.createElement("button"); solveBtn.type = "button"; solveBtn.textContent = WavrT("Solve & save");
    const cancelWalk = document.createElement("button"); cancelWalk.type = "button"; cancelWalk.className = "danger"; cancelWalk.textContent = WavrT("Cancel");
    wizBtns.appendChild(capBtn); wizBtns.appendChild(retryBtn); wizBtns.appendChild(solveBtn); wizBtns.appendChild(cancelWalk);
    wiz.appendChild(wizBtns); s3.appendChild(wiz);
    wrap.appendChild(s3);

    // Wizard state: SERVER-authoritative (the backend's CalibSession, POST calib-capture/
    // calib-retry, is the single source of truth -- ADR-0002 coordinate-only). The browser
    // only mirrors spots/spotIdx/state for rendering; it never pairs a feet-pixel itself.
    // `room` (from calib-spots) is what links this walk to the 3D maquette (linkRoom()) so
    // the room visibly lights up while it's being calibrated.
    const walk = { on:false, room:null, spots:[], spotIdx:0, state:"walking", pollId:null, seen:false };
    const stopPoll = ()=>{ if(walk.pollId){ clearInterval(walk.pollId); walk.pollId = null; } };
    // Redraw the mini plan: room outline from the corner spots, a numbered dot per spot
    // (centre + each corner) -- dim = pending, dimmed accent = captured, full accent = the
    // CURRENT target (walk.spotIdx, mirrored from the server's session state -- capture is
    // sequential, never skips ahead).
    const renderWalkMap = ()=>{
      while(walkmap.firstChild) walkmap.firstChild.remove();
      const spots = walk.spots; if(!spots.length) return;
      const corners = spots.filter(s=>s.label !== "centre");
      const xs = (corners.length ? corners : spots).map(s=>s.x);
      const ys = (corners.length ? corners : spots).map(s=>s.y);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const minY = Math.min(...ys), maxY = Math.max(...ys);
      const spanX = Math.max(maxX - minX, 0.5), spanY = Math.max(maxY - minY, 0.5);
      const pad = 16;
      const sx = (x)=> pad + (x - minX) / spanX * (100 - 2*pad);
      const sy = (y)=> pad + (y - minY) / spanY * (100 - 2*pad);
      if(corners.length >= 3){
        const poly = document.createElementNS(SVG_NS, "polygon");
        poly.setAttribute("class", "wm-room");
        poly.setAttribute("points", corners.map(s=>`${sx(s.x)},${sy(s.y)}`).join(" "));
        walkmap.appendChild(poly);
      }
      spots.forEach((s,i)=>{
        const done = i < walk.spotIdx, cur = i === walk.spotIdx;
        const c = document.createElementNS(SVG_NS, "circle");
        c.setAttribute("class", "wm-spot" + (done ? " done" : cur ? " cur" : ""));
        c.setAttribute("cx", sx(s.x)); c.setAttribute("cy", sy(s.y)); c.setAttribute("r", cur ? 4.5 : 3.2);
        const t = document.createElementNS(SVG_NS, "title");
        // The spot's own label is DATA (the backend's corner naming); only the state it is
        // in is a sentence, so the label goes into a slot rather than being glued to one.
        const spotName = s.label === "centre" ? WavrT("centre") : s.label;
        t.textContent = done ? WavrT("{spot} — captured", {spot: spotName})
                      : cur  ? WavrT("{spot} — walk here now", {spot: spotName})
                             : spotName;
        c.appendChild(t);
        walkmap.appendChild(c);
        const lbl = document.createElementNS(SVG_NS, "text");
        lbl.setAttribute("class", "wm-label");
        lbl.setAttribute("x", sx(s.x)); lbl.setAttribute("y", sy(s.y) - 6);
        lbl.textContent = String(i+1);
        walkmap.appendChild(lbl);
      });
    };
    const renderWalk = ()=>{
      renderWalkMap();
      const spot = walk.spots[walk.spotIdx];
      if(spot){
        const label = spot.label === "centre" ? WavrT("the CENTRE of the room")
          // The one distance this product puts on screen, and it was the one
          // `WavrFmt.metres` had no call site for: `toFixed(1)` writes an
          // English decimal point inside a Portuguese sentence, and the unit
          // was welded into the sentence so a reader who thinks in feet was
          // told metres. The formatter carries the unit, so the sentence does
          // not — which is also why the key changed.
          : WavrT("the corner near x={x}, y={y}",
                  {x: WavrFmt.metres(spot.x), y: WavrFmt.metres(spot.y)});
        step.textContent = WavrT("Step {i} of {n}: stand on {spot}, facing the camera, then Capture.",
          {i: walk.spotIdx+1, n: walk.spots.length, spot: label});
      } else {
        step.textContent = WavrT("All spots captured. Press Solve & save.");
      }
      prog.textContent = WavrT("{i} of {n} spots captured — every spot is needed to solve (Wavr guides you to each in turn).",
        {i: walk.spotIdx, n: walk.spots.length});
      capBtn.disabled = !spot;
      retryBtn.disabled = walk.spotIdx === 0;
      solveBtn.disabled = walk.state !== "ready";
    };
    const setLive = (present)=>{
      walk.seen = present;
      live.textContent = present ? WavrT("● Person detected — you're in view.")
                                 : WavrT("○ No person seen — step into the camera's view.");
      live.style.color = present ? "var(--accent)" : "var(--dim)";
    };
    // Ending the session ALWAYS stops the camera again (cameras off by default) + clears
    // the in-memory sample. Registered in calibCleanups so a list rebuild also ends it.
    const endWalk = async ()=>{
      stopPoll(); walk.on = false;
      try{ await post(`/api/cameras/${enc}/calib-session`, {active:false}); }catch{}
      try{ linkRoom(walk.room, false); }catch{}   // maquette: stop highlighting this room
      wiz.hidden = true; startWalk.disabled = false;
    };
    calibCleanups.push(()=>{ stopPoll(); if(walk.on){ walk.on=false;
      // best-effort session close on teardown (fire-and-forget; page is tearing down)
      try{ post(`/api/cameras/${enc}/calib-session`, {active:false}); }catch{}
      try{ linkRoom(walk.room, false); }catch{} } });

    startWalk.onclick = async ()=>{
      startWalk.disabled = true;
      let r; try{ r = await post(`/api/cameras/${enc}/calib-session`, {active:true}); }catch{}
      if(!(r && r.ok)){ startWalk.disabled = false; setFb(false, (await errText(r)) || WavrT("couldn't start calibration")); return; }
      let sp; try{ sp = await (await WavrAPI.fetch(`/api/cameras/${enc}/calib-spots`)).json(); }catch{}
      const spots = (sp && Array.isArray(sp.spots)) ? sp.spots : [];
      if(spots.length < 4){ await endWalk(); startWalk.disabled = false;
        setFb(false, WavrT("this camera's room needs a 4+ corner floor plan first (edit the map)")); return; }
      const js = r.json ? await r.json().catch(()=>({})) : {};
      walk.on = true; walk.room = (sp && sp.room) || null; walk.spots = spots;
      walk.spotIdx = 0; walk.state = "walking";
      wiz.hidden = false; setFb(true, "");
      if(js && js.active === false){ setFb(false, WavrT("monitoring is off — turn Wavr on so the camera can see you")); }
      setLive(false); renderWalk();
      try{ linkRoom(walk.room, true); }catch{}   // maquette: this room is now being calibrated
      // Live person indicator: poll the feet-pixel sample (coordinate only, never a frame).
      walk.pollId = setInterval(async ()=>{
        let s; try{ s = await (await WavrAPI.fetch(`/api/cameras/${enc}/calib-sample`)).json(); }catch{ return; }
        setLive(!!(s && s.person));
      }, 700);
    };

    // Capture/retry are now driven SERVER-SIDE (POST calib-capture/calib-retry): the backend's
    // CalibSession pairs the room's current known floor spot with the camera's latest feet
    // pixel itself (coordinate-only, ADR-0002) and returns the walk's new state -- the browser
    // no longer tracks pairs locally, it just mirrors spot_idx/state for rendering.
    capBtn.onclick = async ()=>{
      let r; try{ r = await post(`/api/cameras/${enc}/calib-capture`, {}); }catch{}
      if(!(r && r.ok)){
        setFb(false, (await errText(r)) || WavrT("no person detected — stand where the camera can see you, then capture"));
        return;
      }
      const js = await r.json().catch(()=>null); if(!js) return;
      walk.spotIdx = js.spot_idx; walk.state = js.state;
      setFb(true, WavrT("captured spot {n} — the room's fit just got a little more accurate", {n: walk.spotIdx}));
      renderWalk();
      try{ linkRoom(walk.room, true); }catch{}   // maquette: pulse again, this capture just corrected it
    };

    retryBtn.onclick = async ()=>{
      let r; try{ r = await post(`/api/cameras/${enc}/calib-retry`, {}); }catch{}
      if(!(r && r.ok)){ setFb(false, (await errText(r)) || WavrT("nothing to retry yet")); return; }
      const js = await r.json().catch(()=>null); if(!js) return;
      walk.spotIdx = js.spot_idx; walk.state = js.state;
      setFb(true, WavrT("back to spot {n} — try again", {n: walk.spotIdx+1}));
      renderWalk();
    };

    solveBtn.onclick = async ()=>{
      if(walk.state !== "ready"){ setFb(false, WavrT("capture every spot first")); return; }
      // use_session:true -- the server solves from THIS session's own captured pairs
      // (calib_refine.solve_progressive), merged with any prior walk's stored points for the
      // same camera, so accuracy is cumulative across walks, not just this one.
      let r; try{ r = await putJson(`/api/cameras/${enc}/calibration`, {use_session:true}); }catch{}
      if(r && r.ok){
        const js = await r.json().catch(()=>null);
        const qPct = (js && typeof js.quality === "number" && isFinite(js.quality))
          ? Math.round(js.quality*100) : null;
        await endWalk();
        // Two whole clauses, not a label glued to a number: the quality reading is a
        // sentence of its own, so a language that words it differently still can.
        const head = qPct != null ? WavrT("calibrated by walking — quality {pct}%.", {pct: qPct})
                                  : WavrT("calibrated by walking.");
        setFb(true, head + " " + WavrT("Accurate placement is on — re-enable the camera to apply. "
          + "Calibrate again any time: each walk adds more points and refines the fit further."));
        loadStatus();
      } else { setFb(false, (await errText(r)) || WavrT("couldn't solve (walk again — spots may be too close or in a line)")); }
    };

    cancelWalk.onclick = async ()=>{ await endWalk(); setFb(true, WavrT("calibration walk cancelled — camera turned back off")); };

    // -- Footer: remove all calibration + feedback line --
    const foot = document.createElement("div"); foot.className = "calib-btns";
    const rmCal = document.createElement("button"); rmCal.type = "button"; rmCal.className = "danger";
    rmCal.textContent = WavrT("Remove all calibration");
    rmCal.onclick = async ()=>{
      let r; try{ r = await del(`/api/cameras/${enc}/calibration`); }catch{}
      if(r && r.ok){ setFb(true, WavrT("calibration removed — camera reverts to room-centred")); loadStatus(); }
      else setFb(false, WavrT("couldn't remove calibration"));
    };
    foot.appendChild(rmCal);
    wrap.appendChild(foot);
    wrap.appendChild(fb);

    async function loadStatus(){
      let c;
      try{ c = await (await WavrAPI.fetch(`/api/cameras/${enc}/calibration`)).json(); }
      catch{ status.textContent = WavrT("Couldn't read calibration."); return; }
      if(c && c.mount) for(const k in mountInputs) if(c.mount[k] != null) mountInputs[k].value = c.mount[k];
      // Position-quality hint: for a homography this is the REAL measured value
      // (localize.homography_quality, from calib_refine's reprojection residual over ALL
      // accumulated correspondences, not a flat constant) -- it rises as more walks add more
      // points (progressive refinement), so this number is honest to watch improve over time.
      // A mount estimate has no per-camera measurement, so it stays the flat Q_MONOCULAR
      // fallback (0.45) exactly, same as backend localize.py.
      let lead;
      if(c && c.homography){
        const qPct = (typeof c.quality === "number" && isFinite(c.quality)) ? Math.round(c.quality*100) : null;
        lead = (qPct != null
          ? WavrT("Calibrated (precise, by walking or points) — quality {pct}%.", {pct: qPct})
          : WavrT("Calibrated (precise, by walking or points) — quality unknown.")) + " ";
      }
      else if(c && c.mount) lead = WavrT("Estimate active (approximate) — quality 45%.") + " ";
      else lead = WavrT("Not calibrated — this camera reports its room only.") + " ";
      status.textContent = "";
      const b = document.createElement("b"); b.textContent = lead;
      status.appendChild(b);
      status.appendChild(document.createTextNode(
        c && c.localizes ? WavrT("People are placed on the map.")
                         : WavrT("Add an estimate or 4 points to place people on the map.")));
    }
    loadStatus();
    // toggle-off calls this before removing the panel: revoke the still object-URL AND
    // stop any live walk poll + end the sampling session (so a collapsed panel never
    // leaves the camera running or a poll ticking).
    wrap._cleanup = ()=>{ revoke(); stopPoll();
      if(walk.on){ walk.on = false; try{ post(`/api/cameras/${enc}/calib-session`, {active:false}); }catch{}
        try{ linkRoom(walk.room, false); }catch{} } };
    return wrap;
  }

  async function refresh(){
    ptzCleanups.splice(0).forEach(fn=>{ try{ fn(); }catch{} });   // stop any live hold before rebuild
    calibCleanups.splice(0).forEach(fn=>{ try{ fn(); }catch{} }); // revoke any open still object-URLs
    let cams; try{ cams = await (await fetch(location.origin+"/api/cameras")).json(); }catch{ return; }
    window.__wavrCameras?.(cams);   // Stage-2 hook: Detected excludes already-configured cameras
    // PTZ is default-OFF: only probe per-camera capabilities when the global feature flag is on.
    let ptzFeature = false;
    try{ const s = await (await fetch(location.origin+"/api/status")).json();
         ptzFeature = !!(s.features && s.features.ptz); }catch{}
    const list = document.getElementById("camList");
    // Teaches rather than terminates. This is the one moment somebody is
    // looking straight at a feature they have not used, and "no camera
    // configured" spends it telling them what they can already see.
    list.textContent = "";
    if(!cams.length){
      // Built node by node rather than as one markup blob: the lead and the paragraph are
      // each a WHOLE sentence, so each is one catalogue key and neither is a fragment.
      const emptyBox = document.createElement("div"); emptyBox.className = "empty";
      const emptyLead = document.createElement("b");
      emptyLead.textContent = WavrT("No cameras yet");
      emptyBox.appendChild(emptyLead);
      emptyBox.appendChild(document.createElement("br"));
      emptyBox.appendChild(document.createTextNode(WavrT(
        "A camera is what lets Wavr count people in a room rather than only know somebody is "
        + "there — and, once calibrated, place them within it. Frames are never stored; Wavr "
        + "asks each one \"is there a person in this picture?\" and discards it.")));
      list.appendChild(emptyBox);
    }
    cams.forEach(c=>{
      const row = document.createElement("div"); row.className="cam-row";
      const label = document.createElement("span");
      const nameEl = document.createElement("b"); nameEl.textContent = c.name;
      const confEl = document.createElement("span"); confEl.className = "conf"; confEl.textContent = c.rtsp_url;
      label.appendChild(nameEl);
      label.appendChild(document.createTextNode(` · ${c.room} · `));
      label.appendChild(confEl);
      // Liveness honesty (fake-presence-on-disconnect fix): surface an OFFLINE
      // camera so its room's decayed reading is never mistaken for a confirmed
      // empty. c.liveness is a fixed backend enum ('live'|'offline'|'privacy'|
      // 'unknown') — set via textContent + inline style (XSS-safe, never innerHTML).
      if(c.liveness === "offline"){
        const live = document.createElement("span");
        live.textContent = WavrT(" · offline");
        live.style.color = "#d98b3a";
        live.style.fontWeight = "600";
        live.title = WavrT("No frames arriving — this room's presence can't be confirmed");
        label.appendChild(live);
      } else if(c.liveness === "privacy"){
        // Tapo privacy mode (RTSP session opened but produced no frames): NOT an
        // error. Calm blue (--zone), distinct from the amber 'offline' warning above —
        // the covered lens is the privacy-first stance made physical, not a fault.
        const priv = document.createElement("span");
        priv.textContent = WavrT(" · privacy mode");
        priv.style.color = "#6ea8fe";
        priv.style.fontWeight = "600";
        priv.title = WavrT("Lens is covered (privacy mode) — no video, this is expected");
        label.appendChild(priv);
      }
      row.appendChild(label);
      const rm = document.createElement("button"); rm.className="rm"; rm.textContent=WavrT("Remove");
      rm.onclick = async ()=>{
        let r;
        try{ r = await WavrAPI.fetch(`/api/cameras/${encodeURIComponent(c.name)}`, {method: "DELETE"}); }catch{}
        // Item 4: visible result — success collapses the row (refresh); failure shows why, not silence.
        if(r && r.ok){ actionFeedback(camFb, true); refresh(); }
        else { actionFeedback(camFb, false, WavrT("couldn't remove the camera")); }
      };
      // Spec A: per-camera localization calibration. Toggles a panel (estimate + 4-point
      // homography + roadmap auto-calibrate) right below the row.
      const cal = document.createElement("button"); cal.className = "rm"; cal.type = "button";
      cal.textContent = WavrT("Calibrate"); cal.setAttribute("aria-expanded","false");
      let calPanel = null;
      cal.onclick = ()=>{
        if(calPanel){ try{ calPanel._cleanup?.(); }catch{} calPanel.remove(); calPanel = null;
          cal.setAttribute("aria-expanded","false"); return; }
        calPanel = buildCalibPanel(c.name, c.liveness); row.after(calPanel);
        cal.setAttribute("aria-expanded","true");
      };
      row.appendChild(cal);
      // Somewhere else asked for this camera's calibration panel (a camera just
      // added from a discovery, or the Coverage tile). The list is rendered
      // asynchronously, so the request is left as a name and claimed by whichever
      // row turns out to be that camera. Single-use: cleared on claim, so a later
      // refresh does not reopen it under the operator.
      if(window.__wavrCalibrateNext === c.name){
        window.__wavrCalibrateNext = null;
        cal.click();
        setTimeout(()=>{ try{ cal.scrollIntoView({block:"center"}); }catch{} }, 0);
      }
      row.appendChild(rm); list.appendChild(row);
      // Lazily probe PTZ capability for this camera; if the ONVIF camera exposes PTZ, drop the
      // d-pad in on its own line just below the row. Hidden entirely when ptz:false / offline.
      if(ptzFeature){
        (async ()=>{
          let cap; try{ cap = await (await WavrAPI.fetch(`/api/ptz/${encodeURIComponent(c.name)}/capabilities`)).json(); }catch{ return; }
          if(cap && cap.ptz === true && row.isConnected){ row.after(buildPtzPad(c.name)); }
        })();
      }
    });
  }
  document.getElementById("camForm").onsubmit = async (e)=>{
    e.preventDefault();
    const f = e.target;
    let r;
    try{
      r = await post("/api/cameras", {
        name: f.name.value, room: f.room.value, rtsp_url: f.rtsp_url.value,
        confidence: parseFloat(f.confidence.value),
      });
    }catch{}
    if(r && r.ok){
      f.reset(); f.confidence.value = "0.4"; refresh();
      actionFeedback(camFb, true);
    } else {
      actionFeedback(camFb, false, WavrT("couldn't add the camera"));
    }
  };
  window.__wavrCamerasRefresh = refresh;   // Stage-2 hook: the guided form (rung 2) re-syncs the list
  refresh();
}
renderCameras();

