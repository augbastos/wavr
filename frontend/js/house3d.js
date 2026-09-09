// ==========================================================================
// house3d.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- 3D house view: lazy-load self-hosted three.js (never on the top-down-only path) ----
// Memoizes the IN-FLIGHT PROMISE (not just the settled result): rapid Top->3D->Top->3D
// toggling while the ~750KB three.js dynamic import is still pending would otherwise call
// ensureThree() multiple times before the first import() resolves, each kicking off its own
// import and (downstream, in ensure3D) its own WebGLRenderer/ResizeObserver on the same
// canvas — leaking the earlier ones. Caching the promise itself means every concurrent
// caller awaits the SAME single import.
let _threePromise = null;
function ensureThree(){
  if(!_threePromise){
    _threePromise = Promise.all([
      import("three"),
      import("three/addons/controls/OrbitControls.js"),
    ]).then(([THREE, { OrbitControls }]) => ({ THREE, OrbitControls }));
  }
  return _threePromise;
}

async function renderRadar(){
  await loadHouse();                        // sets HOUSE (+ DEMO_HOUSE fallback) and currentFloor
  // House-editor state, declared up front: renderHouseOverlay (below) reads SELECTED on every
  // render, including the very first synchronous rebuildRoomsForFloor() call further down.
  let TOOL = "select", DIRTY = false, SELECTED = null;   // SELECTED: {kind:'room'|'wall'|'feature', id}
  const HOUSE_HISTORY = [], HOUSE_REDO = [];
  const EDIT_SNAP = 0.25;
  function snap(v){ return Math.round(v/EDIT_SNAP)*EDIT_SNAP; }
  const svg = document.getElementById("radar");
  // ---- 3D view state (Floor plan/3D toggle) — 3D is the default view (Command Center §4):
  // cold-start in 3D on a first-ever visit; the user's last explicit choice persists per
  // browser via localStorage, but anything other than a stored "top" means 3D.
  const VIEW_KEY = "wavr.view.v1";
  let viewMode = "3d";   // "top" | "3d"
  try{ if(localStorage.getItem(VIEW_KEY) === "top") viewMode = "top"; }catch{}
  // Sync the static (3D-first) markup to the computed initial mode: setViewMode() only runs
  // on *changes*, so the initial button/visibility state is applied once here.
  // P5 fix 6: aria-pressed tracks the .on class (same pattern as the .fchip toggles).
  {
    const vt = document.getElementById("viewTop"), v3 = document.getElementById("view3d");
    vt.classList.toggle("on", viewMode === "top");
    vt.setAttribute("aria-pressed", viewMode === "top" ? "true" : "false");
    v3.classList.toggle("on", viewMode === "3d");
    v3.setAttribute("aria-pressed", viewMode === "3d" ? "true" : "false");
  }
  syncRadarSub();   // hoisted from the view-toggle section below; static copy assumes 3D
  svg.toggleAttribute("hidden", viewMode !== "top");
  document.getElementById("radar3d").toggleAttribute("hidden", viewMode !== "3d");
  {
    const zb0 = document.getElementById("zoneBar");
    if(zb0) zb0.hidden = (viewMode !== "top") || (MODE === "companion");
  }
  const WALL_H = 2.5;     // metres, constant wall height (no per-room height data in HOUSE v2)
  // ---- v2 floor stacking: every floor renders at its real height; the active floor is
  // solid, the rest are ghosted (see GHOST). Levels can be negative (basement) — the offset
  // is just a multiplier, no special-casing.
  const FLOOR_STACK_H = WALL_H + 0.6;                      // wall height + visual gap between floors
  const offsetForLevel = (level) => level * FLOOR_STACK_H;
  const GHOST = { floor: 0.12, wall: 0.09, edge: 0.08, feature: 0.10, personGhost: 0.20 };
  const floorGroups = new Map();                           // level -> THREE.Group (child of roomGroup)
  let cam3dFramed = false;                                 // first render3D() frames the camera; edits never re-frame
  let floor3dShown = null;                                 // which level the camera was last framed/held on
  let three3d = null;     // { renderer, scene, camera, controls, roomGroup, raf, ro } — built once, lazily
  let cancelHedDrawIfActive = () => {};   // reassigned inside initHouseEditor() when MODE==="live"
  // ---- v2 in-3D editor drag state (assigned inside initHouseEditor, read by the camera
  // tween + setViewMode so orbit is never re-enabled mid-drag / a drag never leaks a view switch)
  let hed3d = null;                                        // active 3D edit-drag descriptor | null
  let hed3dHover = null;                                   // hovered handle mesh (mouse only)
  let cancelHed3dIfActive = () => {};
  // Layer order (document order = paint order, first = bottom): true house geometry, zones,
  // room rects + dots, then the in-progress zone-drawing preview on top of everything.
  const houseLayer = document.createElementNS(SVG_NS, "g"); houseLayer.id = "houseLayer";
  const zoneLayer  = document.createElementNS(SVG_NS, "g");
  const roomsLayer = document.createElementNS(SVG_NS, "g"); roomsLayer.id = "roomsLayer";
  const drawLayer  = document.createElementNS(SVG_NS, "g");
  svg.appendChild(houseLayer); svg.appendChild(zoneLayer); svg.appendChild(roomsLayer); svg.appendChild(drawLayer);

  const roomIdx = {};
  // roomIdx only ever holds the ACTIVE floor's rooms (+ their 2D SVG DOM nodes), so a
  // RoomState for an off-floor room used to early-return from radarUpdate and never
  // reach liveTargets / the 3D ghost-marker — "someone's upstairs" silently vanished
  // in a multi-floor house. roomRectIdx is the geometry (name -> {x,y,w,h}) for EVERY
  // floor, so live target data is kept complete; only the 2D SVG draw stays floor-gated.
  const roomRectIdx = {};
  // ---- Map liveness tri-state (fake-presence-on-disconnect fix) ----
  // roomLiveness[room] mirrors /api/cameras' per-camera enum, aggregated per room
  // ('offline' if ANY covering camera is latched down by the F3 health hook). Rebuilt by
  // applyMapLiveness() from the live poll below; empty in demo/companion. mapState()
  // resolves the single state the map paints, in priority order:
  //   'occupied' — a live reading confirms presence: ALWAYS wins so real presence is
  //                never hidden by an offline sibling sensor (the backend re-fuse loop
  //                decays a dead single-camera room to unoccupied, at which point one of
  //                the states below takes over — that is how frozen presence is undone).
  //   'offline'  — not occupied AND a covering camera is down: amber, never trusted empty.
  //   'privacy'  — not occupied/offline AND a covering camera is deliberately covered
  //                (Tapo privacy mode): calm blue, NEVER amber/error — a covered camera
  //                is the privacy-first stance made physical, not a fault (no cry-wolf).
  //   'unknown'  — a camera is registered here but not running (switched off, or
  //                never started): slate, the same "Wavr cannot tell" family as
  //                'blind'. NEVER the empty base — that base means a working
  //                sensor looked and saw nobody.
  //   'empty'    — in latest, a live sensor confirms empty: the dark base (unchanged).
  //   'blind'    — never in latest, no live sensor here: 'no coverage'.
  const roomLiveness = {};
  function mapState(name){
    if(roomOcc[name]) return "occupied";              // presence always wins — never hidden
    if(roomLiveness[name] === "offline") return "offline";
    if(roomLiveness[name] === "privacy") return "privacy";
    // 'unknown' is a camera that is registered but not running — switched off,
    // or never started. The backend is explicit that this "never asserts
    // empty", and this branch did not exist, so the room fell through to
    // 'empty' below and was painted with the dark base the key above reserves
    // for a LIVE sensor confirming the room is vacant. Somebody switching a
    // camera off made the room look actively verified.
    if(roomLiveness[name] === "unknown") return "unknown";
    if(name in roomOcc) return "empty";               // live sensor confirmed empty
    return "blind";                                   // no live sensor here
  }
  // Paint the 2D SVG rect for one room. Exactly one state class is applied (empty => none,
  // keeping the existing dark base); replaces the old per-frame `.occ` toggle.
  function paintRoom2D(name){
    const e = roomIdx[name]; if(!e) return;
    const st = mapState(name), cl = e.rect.classList;
    cl.toggle("occ", st === "occupied");
    cl.toggle("off", st === "offline");
    cl.toggle("privacy", st === "privacy");
    cl.toggle("blind", st === "blind");
    cl.toggle("unknown", st === "unknown");
  }

  // ---- v2 floor -> v1-compatible rect adapter (bounding box of each room polygon) ----
  // Feeds the existing rect-based room rendering / dot placement / zone hit-test unchanged;
  // the true polygon/wall/feature geometry is drawn separately by renderHouseOverlay() below.
  function floorFor(level){ return HOUSE.floors.find(f => f.level === level) || HOUSE.floors[0] || null; }
  function rectsFor(f){
    if(!f) return [];
    return (f.rooms||[]).map(r=>{
      const poly = r.polygon || [];
      const xs = poly.map(p=>p[0]), ys = poly.map(p=>p[1]);
      const x = xs.length ? Math.min(...xs) : 0, y = ys.length ? Math.min(...ys) : 0;
      const w = xs.length ? Math.max(...xs) - x : 0, h = ys.length ? Math.max(...ys) - y : 0;
      return {name: r.name, x, y, w, h, polygon: poly};
    });
  }
  function renderHouseOverlay(f){
    houseLayer.textContent = "";
    if(!f) return;
    (f.rooms||[]).forEach(r=>{
      if(!r.polygon || r.polygon.length < 3) return;
      const poly = document.createElementNS(SVG_NS, "polygon");
      poly.setAttribute("points", r.polygon.map(p=>p[0]+","+p[1]).join(" "));
      poly.setAttribute("class", "room-poly" + (SELECTED && SELECTED.kind==="room" && SELECTED.id===r.id ? " selected" : ""));
      if(r.name) poly.dataset.name = r.name;
      if(r.id) poly.dataset.id = r.id;
      poly.onclick = ()=>{ if(!r.id) return; selectHouseEl("room", r.id); };
      houseLayer.appendChild(poly);
    });
    (f.walls||[]).forEach(w=>{
      if(!w.a || !w.b) return;
      const ln = document.createElementNS(SVG_NS, "line");
      ln.setAttribute("x1", w.a[0]); ln.setAttribute("y1", w.a[1]);
      ln.setAttribute("x2", w.b[0]); ln.setAttribute("y2", w.b[1]);
      ln.setAttribute("class", "wall" + (SELECTED && SELECTED.kind==="wall" && SELECTED.id===w.id ? " selected" : ""));
      if(w.id) ln.dataset.id = w.id;
      ln.onclick = ()=>{ if(!w.id) return; selectHouseEl("wall", w.id); };
      houseLayer.appendChild(ln);
    });
    (f.features||[]).forEach(ft=>{
      if(!ft.at) return;
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", ft.at[0]); c.setAttribute("cy", ft.at[1]);
      c.setAttribute("r", 0.15);
      c.setAttribute("class", "feat feat-"+ft.type + (SELECTED && SELECTED.kind==="feature" && SELECTED.id===ft.id ? " selected" : ""));
      if(ft.id) c.dataset.id = ft.id;
      c.onclick = ()=>{ if(!ft.id) return; selectHouseEl("feature", ft.id); };
      houseLayer.appendChild(c);
    });
    // A9 bed/rest zones (wavr.fall_detect): READ-ONLY visual round-trip -- drawn so a zone
    // saved from another browser/device (or hand-edited house.json) is at least visible
    // here, but not selectable/editable via this overlay (creation/deletion goes through
    // the "Edit zones" rectangle tool above, 2D top-view only, same as every local zone).
    (f.zones||[]).forEach(z=>{
      if(!z.polygon || z.polygon.length < 3 || z.kind !== "rest") return;
      const poly = document.createElementNS(SVG_NS, "polygon");
      poly.setAttribute("points", z.polygon.map(p=>p[0]+","+p[1]).join(" "));
      poly.setAttribute("class", "zone-rest-poly");
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = WavrT("{zone} -- protects from fall alerts",
                                { zone: z.name || WavrT("bed/rest zone") });
      poly.appendChild(title);
      houseLayer.appendChild(poly);
    });
  }
  function rebuildRoomsForFloor(){
    const f = floorFor(currentFloor);
    const rects = rectsFor(f);
    roomsLayer.textContent = "";
    for(const k in roomIdx) delete roomIdx[k];
    // All-floor geometry, refreshed here (floor switch / house edit / boot — never
    // per-frame, cost is O(total rooms)). Feeds radarUpdate's off-floor liveTargets.
    for(const k in roomRectIdx) delete roomRectIdx[k];
    (HOUSE.floors || []).forEach(fl => rectsFor(fl).forEach(r => { roomRectIdx[r.name] = r; }));
    const W = rects.length ? Math.max(...rects.map(r=>r.x+r.w)) : 7.7;
    const H = rects.length ? Math.max(...rects.map(r=>r.y+r.h)) : 5.7;
    // Bottom margin is larger than the other sides as a second safety net (on top of the
    // dot()-level label flip/clamp above) for posture-caption footprint near room edges.
    const viewBoxW = W + 0.4;
    svg.setAttribute("viewBox", `-0.2 -0.2 ${viewBoxW} ${H+0.8}`);
    // Rendered CSS px per 1 viewBox metre at the CURRENT screen size — used to keep the
    // room label (and, via radarPxPerM, the posture caption in placeDot()) at a legible
    // real pixel size instead of shrinking on a phone-sized #radar.
    const svgW = svg.getBoundingClientRect().width || svg.clientWidth || 340;
    radarPxPerM = (svgW / viewBoxW) || 40;
    rects.forEach(r=>{
      const g = document.createElementNS(SVG_NS, "g");
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("class", "radar-room");
      rect.dataset.room = r.name;   // §6 cross-highlight: 2D room participates via data-room
      rect.setAttribute("x", r.x); rect.setAttribute("y", r.y);
      rect.setAttribute("width", r.w); rect.setAttribute("height", r.h);
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("class", "radar-roomlabel");
      label.setAttribute("x", r.x + 0.15); label.setAttribute("y", r.y + 0.35);
      label.style.fontSize = (11 / radarPxPerM) + "px";
      label.textContent = r.name;
      const layer = document.createElementNS(SVG_NS, "g");
      g.appendChild(rect); g.appendChild(label); g.appendChild(layer);
      roomsLayer.appendChild(g);
      roomIdx[r.name] = {r, rect, layer, dots: new Map()};
    });
    // Re-apply the liveness tri-state to the freshly built rects so a floor switch never
    // drops an offline/blind room back to the plain base until the next WS/poll tick.
    Object.keys(roomIdx).forEach(paintRoom2D);
    renderHouseOverlay(f);
  }

  // ---- 3D house view (self-hosted three.js): singleton renderer, built once on first
  // entry into 3D mode; never recreated on subsequent toggles (avoids leaking WebGL
  // contexts on repeated toggling). Only the render loop starts/stops.
  //
  // Memoizes the IN-FLIGHT PROMISE (not just `three3d`, the settled result): the old
  // `if(three3d) return three3d` guard ran BEFORE the `await ensureThree()` below, so rapid
  // Top->3D->Top->3D toggling during that pending import could call ensure3D() again while
  // the first call was still awaiting — constructing a second WebGLRenderer/ResizeObserver
  // on the same canvas and leaking the first. Caching the promise means every concurrent
  // caller awaits the SAME construction.
  let _ensure3DPromise = null;
  function ensure3D(){
    if(three3d) return Promise.resolve(three3d);
    if(!_ensure3DPromise){
      _ensure3DPromise = (async () => {
        try {
          const { THREE, OrbitControls } = await ensureThree();
          const canvas = document.getElementById("radar3d");
          const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
          renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
          const scene = new THREE.Scene();
          // Read --surface from the live token instead of hardcoding, so scene bg/fog can
          // never drift from the design system; #141920 stays as the parse-failure fallback.
          let surface = "#141920";
          try{
            const v = getComputedStyle(document.documentElement).getPropertyValue("--surface").trim();
            if(v) surface = v;
          }catch{}
          const surfaceColor = new THREE.Color(surface);
          scene.background = surfaceColor;
          scene.fog = new THREE.Fog(surfaceColor, 14, 42);
          const camera = new THREE.PerspectiveCamera(45, 4/3, 0.1, 200);
          const controls = new OrbitControls(camera, renderer.domElement);
          controls.enableDamping = true;
          controls.dampingFactor = 0.08;
          controls.minDistance = 2;
          controls.maxDistance = 60;
          controls.maxPolarAngle = Math.PI/2 - 0.03;   // camera can't dip below the floor plane
          // Mobile scroll-trap fix: one-finger touch does nothing here (falls through to
          // OrbitControls' default/NONE state), so the page can scroll past #radar3d via
          // its own touch-action:pan-y; two fingers still orbit+zoom in one gesture.
          controls.touches.ONE = null;
          controls.touches.TWO = THREE.TOUCH.DOLLY_ROTATE;
          scene.add(new THREE.AmbientLight(0x8fa3ad, 0.7));
          const sun = new THREE.DirectionalLight(0xfff2df, 1.4);
          sun.position.set(8, 12, 6);
          scene.add(sun);
          const rim = new THREE.DirectionalLight(0x3db54a, 0.18);   // subtle brand-green fill light
          rim.position.set(-6, 4, -8);
          scene.add(rim);
          const roomGroup = new THREE.Group(); scene.add(roomGroup);
          // Live PEOPLE markers: own group (never touched by render3D()'s roomGroup clear), so
          // per-frame live updates don't need to wait for/interact with floor-mesh rebuilds.
          // Geometry + material are built ONCE here and reused by every marker on every frame
          // (never disposed) — updatePeople3D() below only adds/removes Mesh instances that
          // reference these same shared buffers, so there's nothing to leak.
          const peopleGroup = new THREE.Group(); scene.add(peopleGroup);
          const personGeo = new THREE.CylinderGeometry(0.15, 0.15, 0.3, 12);
          const personMat = new THREE.MeshStandardMaterial({ color: 0x3db54a });
          // Ghost-floor person marker material (v2 floor stack): "someone's upstairs" reads
          // without competing with the active floor. Shared, built once, never disposed.
          const personMatGhost = new THREE.MeshStandardMaterial({
            color: 0x3db54a, opacity: GHOST.personGhost, transparent: true, depthWrite: false });
          // Estimated-position marker (monocular prior): semi-transparent so an APPROXIMATE
          // spot reads softer than a calibrated (homography) one on the active floor. Shared,
          // built once, never disposed — same discipline as the other person materials.
          const personMatEst = new THREE.MeshStandardMaterial({
            color: 0x3db54a, opacity: 0.5, transparent: true, depthWrite: false });
          // ---- v2 in-3D editor: handle gizmos + drag previews. All geometries/materials
          // here are SHARED (built once, never disposed) — rebuildHandles3D()/the drag
          // machine only add/remove Mesh wrappers, exactly the peopleGroup discipline.
          const handleGroup = new THREE.Group(); scene.add(handleGroup);
          const hVertGeo = new THREE.SphereGeometry(0.09, 16, 12);
          const hHitGeo  = new THREE.SphereGeometry(0.22, 8, 6);       // oversized invisible hit target (touch)
          const hEdgeGeo = new THREE.BoxGeometry(0.13, 0.05, 0.13);    // flattened diamond via 45° Y-rotation
          const hVertMat  = new THREE.MeshBasicMaterial({ color: 0xEAECEE });   // --text
          const hEdgeMat  = new THREE.MeshBasicMaterial({ color: 0x9AA4AD });   // --dim (secondary handles)
          const hHoverMat = new THREE.MeshBasicMaterial({ color: 0x3db54a });   // --accent
          const hDragMat  = new THREE.MeshBasicMaterial({ color: 0xe8a13a });   // --warn
          const hHitMat   = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
          // Preview outline (dashed amber loop) — preallocated buffer, updated in place per
          // pointermove (no per-frame allocation beyond computeLineDistances' own array).
          const plGeo = new THREE.BufferGeometry();
          plGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(64 * 3), 3));
          plGeo.setDrawRange(0, 0);
          const previewLine = new THREE.Line(plGeo,
            new THREE.LineDashedMaterial({ color: 0xe8a13a, dashSize: 0.18, gapSize: 0.12 }));
          previewLine.visible = false; previewLine.renderOrder = 12; previewLine.frustumCulled = false;
          scene.add(previewLine);
          // Preview wall quad (same 2-triangle shape as addWallSegment3D, amber @ .55)
          const pwGeo = new THREE.BufferGeometry();
          pwGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6 * 3), 3));
          const previewWall = new THREE.Mesh(pwGeo, new THREE.MeshBasicMaterial({
            color: 0xe8a13a, transparent: true, opacity: 0.55, side: THREE.DoubleSide, depthWrite: false }));
          previewWall.visible = false; previewWall.frustumCulled = false;
          scene.add(previewWall);
          // Snap confirmation ring: fades in at the exact snapped point while dragging, so
          // the 0.25 m grid is FELT rather than assumed.
          const snapRing = new THREE.Mesh(new THREE.RingGeometry(0.05, 0.09, 24),
            new THREE.MeshBasicMaterial({ color: 0xe8a13a, side: THREE.DoubleSide, depthWrite: false }));
          snapRing.rotation.x = -Math.PI / 2; snapRing.visible = false; snapRing.renderOrder = 12;
          scene.add(snapRing);
          // Ground + grid just under floor level, for depth/scale reference while orbiting. Built
          // once here (this construction only ever runs once — see the promise-cache guard
          // above) and added directly to scene, not roomGroup, so the per-floor clear in render3D()
          // never touches them and they don't need to be recreated (and leaked) on every call.
          const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400),
            new THREE.MeshLambertMaterial({ color: 0x11161b }));
          ground.rotation.x = -Math.PI/2; ground.position.y = -0.02;
          scene.add(ground);
          const grid = new THREE.GridHelper(400, 200, 0x2a323b, 0x1c2229);
          grid.position.y = -0.01;
          scene.add(grid);
          const ro = new ResizeObserver(() => resize3D());
          ro.observe(canvas);
          // §6 cross-highlight, canvas → DOM: click/tap a room floor selects it (same path
          // as clicking its room card). A real orbit-drag (pointer moved >6px) is ignored,
          // so OrbitControls interaction is untouched. Raycasts are scoped to the ACTIVE
          // floor's group (v2 floor stack) — ghost floors are view-only, a click can never
          // land on the wrong floor. Only floor plates carry userData.room.
          let _downPt = null;
          canvas.addEventListener("pointerdown", (e)=>{ _downPt = { x: e.clientX, y: e.clientY }; });
          canvas.addEventListener("pointerup", (e)=>{
            const d = _downPt; _downPt = null;
            // Audit H2: the live-mode house editor's pointerup (same canvas; registered at
            // init, i.e. BEFORE this post-async-import handler) marks any event it consumed
            // (drag commit, stairs placement, select-tool hit). A consumed edit click must
            // not ALSO drive __wavrSelectRoom's presence-card select + scroll + focus —
            // editing never focus-steals. Demo/companion never register the editor, so the
            // flag stays unset there and cross-highlight behaves exactly as before.
            if(e.__wavrHedConsumed) return;
            if(!d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) return;
            const r = canvas.getBoundingClientRect();
            if(!r.width || !r.height) return;
            const ndc = new THREE.Vector2(
              ((e.clientX - r.left) / r.width) * 2 - 1,
              -((e.clientY - r.top) / r.height) * 2 + 1);
            const rc = new THREE.Raycaster();
            rc.setFromCamera(ndc, camera);
            const grp = floorGroups.get(currentFloor);
            const hit = grp ? rc.intersectObjects(grp.children, true)
              .find(h => h.object.userData && h.object.userData.room) : null;
            if(hit) window.__wavrSelectRoom?.(hit.object.userData.room);
          });
          three3d = { THREE, renderer, scene, camera, controls, roomGroup,
                      peopleGroup, personGeo, personMat, personMatGhost, personMatEst,
                      handleGroup, hVertGeo, hHitGeo, hEdgeGeo,
                      hVertMat, hEdgeMat, hHoverMat, hDragMat, hHitMat,
                      previewLine, previewWall, snapRing, raf: null, ro };
          resize3D();
          return three3d;
        } catch (err) {
          // Construction failed (e.g. WebGL disabled/blocklisted and `new THREE.WebGLRenderer`
          // threw) — clear the cached promise so a later retry (context may become available,
          // or the user reloads/toggles again) isn't stuck forever on a rejected promise.
          _ensure3DPromise = null;
          throw err;
        }
      })();
    }
    return _ensure3DPromise;
  }

  function resize3D(){
    if(!three3d) return;
    const canvas = three3d.renderer.domElement;
    const w = canvas.clientWidth || 1, h = canvas.clientHeight || 1;
    three3d.renderer.setSize(w, h, false);
    three3d.camera.aspect = w / h;
    three3d.camera.updateProjectionMatrix();
  }

  // ---- §6 cross-highlight state (3D side) ----
  // room3dIdx: room name -> { mesh: floor plate, base: unlit THREE.Color, wallMat: this
  // room's shared wall material, wallBase: its unlit THREE.Color } — rebuilt by
  // render3D() alongside the meshes. hl3d: the one currently highlighted room (drives the
  // emissive boost + person-marker pulse in the render loop below).
  const room3dIdx = {};
  let hl3d = { room: null, t0: 0 };
  function startLoop3D(){
    if(!three3d || three3d.raf) return;
    const tick = () => {
      three3d.raf = requestAnimationFrame(tick);
      three3d.controls.update();
      // Highlighted room's person markers pulse gently while linked (§6). Reduced motion:
      // a static scale-up (jump-cut), no sine tween. Idle scenes with no highlight skip this.
      if(hl3d.room){
        const s = REDUCED_MOTION.matches
          ? 1.25
          : 1.18 + 0.14 * Math.sin((performance.now() - hl3d.t0) / 200);
        three3d.peopleGroup.children.forEach(m => {
          // baseScale: ghost-floor markers render at 0.6 (v2 floor stack) — the pulse
          // multiplies that base instead of stomping it back to 1.
          const b = m.userData.baseScale || 1;
          m.scale.setScalar(b * (m.userData.room === hl3d.room ? s : 1));
        });
      }
      three3d.renderer.render(three3d.scene, three3d.camera);
    };
    three3d.raf = requestAnimationFrame(tick);
  }
  function stopLoop3D(){
    if(!three3d || !three3d.raf) return;
    cancelAnimationFrame(three3d.raf);
    three3d.raf = null;
  }

  // house (x, y_meters) -> world (x, z), height on Y — explicit mapping used everywhere
  // below (floors, walls, features, camera), never a rotation trick, so nothing can
  // silently drift/mirror between the floor plates and the walls.
  function addWallSegment3D(THREE, roomGroup, mats, ax, ay, bx, by){
    const verts = new Float32Array([
      ax,0,ay,  bx,0,by,  bx,WALL_H,by,
      ax,0,ay,  bx,WALL_H,by,  ax,WALL_H,ay,
    ]);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(verts, 3));
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, mats.wall);
    roomGroup.add(mesh);
    const line = new THREE.LineSegments(new THREE.EdgesGeometry(geo), mats.edge);
    roomGroup.add(line);
    return { mesh, line };   // v2 editor: callers tag userData (roomId/wallId) for raycast hit-testing
  }

  // P4 fix 5: room-name label rendered onto its block in the 3D view. The name is drawn
  // with canvas fillText (pure pixels — untrusted room names can never become markup),
  // billboarded as a Sprite with depthTest off so it stays legible through the walls.
  function makeRoomLabel3D(THREE, name){
    const txt = String(name == null ? "" : name).trim();
    if(!txt) return null;
    // The room name, exactly as the household typed it.
    //
    // This used to re-capitalise every word, to match a CSS
    // `text-transform:capitalize` that `.radar-roomlabel` and the room card's
    // `<h2>` both carried — canvas text cannot read a CSS rule, so the rule had
    // to be replicated by hand here. That kept those surfaces agreeing, but it
    // agreed on the wrong thing: the Space subtitle listed the same rooms
    // untouched, so "Living room" and "Living Room" still met on one screen,
    // and a room somebody called "TV room" came back "Tv Room".
    //
    // The transform is gone from all three now, and the name is nobody's to
    // re-spell. If it ever returns to the CSS it has to return here too — that
    // pairing is the whole reason this comment is long.
    const disp = txt;
    const cv = document.createElement("canvas");
    let ctx = cv.getContext("2d");
    if(!ctx) return null;
    const fpx = 44, padX = 26, padY = 14;
    const font = "600 " + fpx + "px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    ctx.font = font;
    cv.width  = Math.max(2, Math.ceil(ctx.measureText(disp).width) + padX * 2);
    cv.height = fpx + padY * 2;
    ctx = cv.getContext("2d");             // resizing the canvas resets its state
    ctx.fillStyle = "rgba(11,14,18,.72)";  // --bg at ~72% — same surface language as the shell
    if(typeof ctx.roundRect === "function"){
      ctx.beginPath(); ctx.roundRect(0, 0, cv.width, cv.height, cv.height / 2); ctx.fill();
    } else ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.font = font;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#EAECEE";             // --text
    ctx.fillText(disp, cv.width / 2, cv.height / 2 + 1);
    const tex = new THREE.CanvasTexture(cv);
    const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
    const spr = new THREE.Sprite(mat);
    const hWorld = 0.5;                    // metres of on-screen billboard height
    spr.scale.set(hWorld * cv.width / cv.height, hWorld, 1);
    spr.renderOrder = 10;                  // paints after the translucent walls
    return spr;
  }

  // ---- v2 floor stack: recursive dispose for the nested floor groups. remove() alone only
  // detaches from the scene graph — it never calls dispose(), so repeated rebuilds leaked
  // geometry+material without this. Sprites share one common geometry (never disposed).
  function disposeGroupDeep(obj){
    for(let i = obj.children.length - 1; i >= 0; i--){
      const child = obj.children[i];
      disposeGroupDeep(child);
      obj.remove(child);
      if(child.isSprite){
        child.material?.map?.dispose?.();
        child.material?.dispose?.();
        continue;
      }
      child.geometry?.dispose?.();
      if(Array.isArray(child.material)) child.material.forEach(m => m.dispose());
      else child.material?.dispose?.();
    }
  }

  const FLOOR_BASE = 0x232a32;   // unlit floor — absence of green IS the signal (§4)
  const WALL_BASE = 0x2e3742;    // neutral unlit wall base (P5 fix 3)
  const MAP_WARN = 0xe8a13a;     // --warn: offline-camera floor tint (never green)
  const MAP_BLIND = 0x4a5563;    // cool slate: 'no coverage', greyed vs the dark empty base
  const MAP_PRIVACY = 0x6ea8fe;  // --zone: calm blue, deliberately-covered camera (not an error)

  // Tag a material with its two opacity states so setActiveFloor3D() can re-tint the whole
  // stack without any geometry rebuild. Ground/grid/handles are untagged → left alone.
  function tagGhostable(m, activeOpacity, ghostOpacity){
    m.userData.activeOpacity = activeOpacity;
    m.userData.ghostOpacity = ghostOpacity;
    m.userData.wasDepthWrite = m.depthWrite;
    m.userData.wasTransparent = m.transparent;
    return m;
  }

  // ---- One floor's meshes, extracted from the old single-floor render3D() body. Appends
  // into its own THREE.Group at the floor's stack height (child of roomGroup, so the
  // existing add-once/dispose lifecycle is untouched — just one level of nesting deeper).
  function buildFloor3D(THREE, f){
    const grp = new THREE.Group();
    grp.position.y = offsetForLevel(f.level);
    grp.userData.level = f.level;
    three3d.roomGroup.add(grp);
    floorGroups.set(f.level, grp);

    const mats = {
      // See-inside (dollhouse) technique: semi-transparent walls + depthWrite:false (avoids
      // z-sort flicker between overlapping panels) + a steep camera angle so far fewer
      // transparent layers stack between the camera and each room.
      wall: tagGhostable(new THREE.MeshStandardMaterial({
        color: WALL_BASE, opacity: 0.4, transparent: true, depthWrite: false, side: THREE.DoubleSide,
      }), 0.4, GHOST.wall),
      // transparent:true up front so the ghost opacity applies without a needsUpdate recompile
      edge: tagGhostable(new THREE.LineBasicMaterial({ color: 0x9fb0b8, transparent: true }), 1, GHOST.edge),
    };

    const bbox = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
    const track = (x, y) => { bbox.minX=Math.min(bbox.minX,x); bbox.minY=Math.min(bbox.minY,y);
                               bbox.maxX=Math.max(bbox.maxX,x); bbox.maxY=Math.max(bbox.maxY,y); };

    (f.rooms || []).forEach(r => {
      if(!r.polygon || r.polygon.length < 3) return;
      r.polygon.forEach(([x,y]) => track(x,y));

      // Floor plate: remap the ShapeGeometry's own vertices to (x, 0, y) directly instead of
      // a mesh.rotation.x=-PI/2 trick, so floors use the *identical* signed mapping the walls
      // use below — sidesteps a whole class of mirrored/offset-floor bugs. DoubleSide covers
      // any winding-direction flip introduced by the axis remap.
      const shape = new THREE.Shape(r.polygon.map(([x,y]) => new THREE.Vector2(x,y)));
      const floorGeo = new THREE.ShapeGeometry(shape);
      const pos = floorGeo.attributes.position;
      for(let i=0;i<pos.count;i++) pos.setXYZ(i, pos.getX(i), 0, pos.getY(i));
      pos.needsUpdate = true;
      floorGeo.computeVertexNormals();
      // Per-room material (§6): the floor tint follows THIS room's live confidence via the
      // shared colorFor() scale (see tintRoom3D). disposeGroupDeep frees these per rebuild.
      const floorMesh = new THREE.Mesh(floorGeo,
        tagGhostable(new THREE.MeshLambertMaterial({ color: FLOOR_BASE, side: THREE.DoubleSide }),
                     1, GHOST.floor));
      floorMesh.userData.room = r.name;   // data-room equivalent for the raycaster (§6)
      if(r.id) floorMesh.userData.roomId = r.id;   // v2 editor: click-select + move-grab target
      grp.add(floorMesh);
      // P5 fix 3: per-room wall material (clone keeps opacity/transparent/depthWrite/side)
      // so tintRoom3D can tint THIS room's walls alongside its floor. Re-tag after clone —
      // Material.clone() deep-copies userData in current three.js, but don't rely on it.
      const wallMat = tagGhostable(mats.wall.clone(), 0.4, GHOST.wall);
      room3dIdx[r.name] = { mesh: floorMesh, base: new THREE.Color(FLOOR_BASE),
                            wallMat, wallBase: new THREE.Color(WALL_BASE) };

      // P4 fix 5: overlay the room's NAME above its block (label sprite lives in the floor
      // group, so disposeGroupDeep frees its texture/material on every rebuild). Ghost
      // floors hide their labels entirely (unreadable at that opacity — pure noise).
      const label = makeRoomLabel3D(THREE, r.name);
      if(label){
        const cSum = r.polygon.reduce((a, [x, y]) => [a[0] + x, a[1] + y], [0, 0]);
        label.position.set(cSum[0] / r.polygon.length, WALL_H + 0.45, cSum[1] / r.polygon.length);
        grp.add(label);
      }

      for(let i=0;i<r.polygon.length;i++){
        const [ax,ay] = r.polygon[i], [bx,by] = r.polygon[(i+1)%r.polygon.length];
        const seg = addWallSegment3D(THREE, grp, { wall: wallMat, edge: mats.edge }, ax, ay, bx, by);
        if(r.id) seg.mesh.userData.roomId = r.id;   // grabbing a wall face also moves the room
      }
    });

    // f.walls: additive interior partitions not aligned to any room boundary. DEMO_HOUSE.walls
    // is [] — room-polygon edges above are the primary wall source, so the demo house (and any
    // real house that never used the wall tool) still renders full walls in 3D.
    (f.walls || []).forEach(w => {
      if(!w.a || !w.b) return;
      track(w.a[0], w.a[1]); track(w.b[0], w.b[1]);
      const seg = addWallSegment3D(THREE, grp, mats, w.a[0], w.a[1], w.b[0], w.b[1]);
      if(w.id) seg.mesh.userData.wallId = w.id;   // v2 editor: click-select + endpoint drag target
    });

    (f.features || []).forEach(ft => {
      if(!ft.at) return;
      track(ft.at[0], ft.at[1]);
      const marker = new THREE.Mesh(new THREE.BoxGeometry(0.3,0.3,0.3),
        tagGhostable(new THREE.MeshLambertMaterial({ color: ft.type==="stairs" ? 0xe0b341 : 0x38bdf8 }),
                     1, GHOST.feature));
      marker.position.set(ft.at[0], 0.15, ft.at[1]);
      if(ft.id) marker.userData.featureId = ft.id;   // v2 editor: click-select target
      grp.add(marker);
    });

    grp.userData.bbox = bbox;   // cached for cheap camera re-framing in setActiveFloor3D()
  }

  // ---- v2 camera tween (floor switch): plain vector lerp via rAF, REDUCED_MOTION-gated by
  // the caller. Controls are paused for the tween's lifetime so damping can't fight it;
  // never re-enabled mid edit-drag (hed3d guard).
  let camTweenRaf = null;
  function cancelCamTween(){
    if(camTweenRaf){ cancelAnimationFrame(camTweenRaf); camTweenRaf = null; }
    if(three3d) three3d.controls.enabled = !hed3d;
  }
  function tweenCameraTo(endPos, endTgt, ms){
    cancelCamTween();
    if(!three3d) return;
    const cam = three3d.camera, ctl = three3d.controls;
    const p0 = cam.position.clone(), t0 = ctl.target.clone();
    const start = performance.now();
    ctl.enabled = false;
    const step = () => {
      const k = Math.min((performance.now() - start) / ms, 1);
      const e = k < 0.5 ? 2*k*k : 1 - Math.pow(-2*k + 2, 2) / 2;   // easeInOutQuad
      cam.position.lerpVectors(p0, endPos, e);
      ctl.target.lerpVectors(t0, endTgt, e);
      ctl.update();
      if(k < 1){ camTweenRaf = requestAnimationFrame(step); }
      else { camTweenRaf = null; ctl.enabled = !hed3d; }
    };
    camTweenRaf = requestAnimationFrame(step);
  }

  // ---- v2 floor stack, the cheap path: re-tint opacity/visibility per floor group without
  // any geometry rebuild. Called from render3D()'s tail and #floorSelect's onchange.
  function setActiveFloor3D(level, frame, tween){
    if(!three3d || !floorGroups.size) return;
    floorGroups.forEach((grp, lvl) => {
      const active = (lvl === level);
      grp.traverse(o => {
        if(o.isSprite){ o.visible = active; return; }   // ghost floors: labels hidden entirely
        const m = o.material;
        if(!m) return;
        (Array.isArray(m) ? m : [m]).forEach(mm => {
          if(!mm.userData || mm.userData.activeOpacity == null) return;
          mm.opacity = active ? mm.userData.activeOpacity : mm.userData.ghostOpacity;
          mm.transparent = active ? mm.userData.wasTransparent : true;
          // depthWrite:false on every ghosted material so stacked translucent floors don't
          // fight over z-sort as the camera orbits; restored per-material when active.
          mm.depthWrite = active ? mm.userData.wasDepthWrite : false;
        });
      });
    });
    if(frame){
      const grp = floorGroups.get(level);
      const bbox = grp && grp.userData.bbox;
      const y0 = offsetForLevel(level);
      let cx, cz, span;
      if(bbox && isFinite(bbox.minX)){
        cx = (bbox.minX + bbox.maxX) / 2; cz = (bbox.minY + bbox.maxY) / 2;
        span = Math.max(bbox.maxX - bbox.minX, bbox.maxY - bbox.minY, 3);
      } else {
        // Empty floor (just added): keep the current horizontal framing, move to its height.
        cx = three3d.controls.target.x; cz = three3d.controls.target.z;
        span = Math.max(three3d.camera.position.distanceTo(three3d.controls.target) / 1.6, 3);
      }
      // Steep ~55-60deg down-angle (dollhouse view) — at a shallow angle the transparent
      // wall panels stack up and grey out the interior.
      const endPos = new three3d.THREE.Vector3(cx + span*0.75, y0 + span*1.15, cz + span*0.75);
      const endTgt = new three3d.THREE.Vector3(cx, y0 + 0.3, cz);
      if(tween && !REDUCED_MOTION.matches){
        tweenCameraTo(endPos, endTgt, 500);
      } else {
        cancelCamTween();
        three3d.camera.position.copy(endPos);
        three3d.controls.target.copy(endTgt);
        three3d.controls.update();
      }
    }
  }

  async function render3D(){
    // Wrapped end-to-end: if ensure3D()'s `new THREE.WebGLRenderer(...)` throws (WebGL
    // disabled/blocklisted) — or anything below it throws while building the scene — fall
    // back to the top-down view instead of leaving an unhandled rejection and a blank
    // canvas (setViewMode('top') was already called by this point to hide the SVG).
    try {
      const { THREE } = await ensure3D();
      // v2 floor stack: rebuild EVERY floor at its real height; the active one is solid,
      // the rest ghost. Dispose is recursive now (floor groups nest inside roomGroup).
      disposeGroupDeep(three3d.roomGroup);
      floorGroups.clear();
      for(const k in room3dIdx) delete room3dIdx[k];   // rebuilt with the meshes below

      HOUSE.floors.forEach(f => buildFloor3D(THREE, f));

      // Fresh meshes start unlit — re-apply the last-known occupancy tint (roomOcc/roomConf
      // are accumulated by updateHouse()) and the active cross-highlight, so a rebuild
      // never flashes back to a colorless house.
      Object.keys(room3dIdx).forEach(n => tintRoom3D(n));
      if(hl3d.room && room3dIdx[hl3d.room]) highlightRoom3D(hl3d.room, true);

      // Frame the camera only on the FIRST build or when the active floor changed since the
      // last one — an edit commit (afterHouseEdit → render3D) must never yank the camera.
      const needFrame = !cam3dFramed || floor3dShown !== currentFloor;
      setActiveFloor3D(currentFloor, needFrame, needFrame && cam3dFramed);
      floor3dShown = currentFloor;
      cam3dFramed = true;

      resize3D();
      // Start the loop here (not from setViewMode) — on the very first entry into 3D mode,
      // ensure3D() above hasn't resolved yet by the time setViewMode() runs synchronously,
      // so three3d doesn't exist there and a start attempt would silently no-op. Guarded by
      // viewMode in case the user already toggled back to "top" before this async work landed.
      if(viewMode==="3d") startLoop3D();
    } catch (err) {
      console.error("Wavr: 3D view unavailable, falling back to top-down view.", err);
      if(viewMode === "3d") setViewMode("top");   // re-shows the SVG and hides the 3D canvas
    }
    updatePeople3D();
    rebuildHandles3D();   // selection handles track the freshly rebuilt geometry
  }

  // ---- 3D people markers: live targets on EVERY floor's ground plane (v2 floor stack) ----
  // Reuses the SAME room-relative -> absolute-meters mapping as the 2D radar dots
  // (liveTargets, fed by absTargetPoint() in the live-update path below — placeDot's
  // clamp logic, mirrored). Maps room x/y (meters) -> world (x, z), person height y=0.9
  // above the room's own stack level. Markers on non-active floors render smaller/dimmer
  // (personMatGhost, no pulse) — "someone's upstairs" reads without stealing focus.
  // No-ops until the user has entered 3D at least once (three3d built lazily by ensure3D()).
  function updatePeople3D(){
    if(!three3d) return;
    const { THREE, peopleGroup, personGeo, personMat, personMatGhost, personMatEst } = three3d;
    // Clear last frame's markers. personGeo/personMat(Ghost) are shared + reused across
    // every frame (created once in ensure3D(), never disposed) — only the Mesh wrapper
    // objects are per-frame, so removing them from the group is all that's needed here.
    while(peopleGroup.children.length) peopleGroup.remove(peopleGroup.children[0]);
    (HOUSE.floors || []).forEach(f => {
      const active = f.level === currentFloor;
      const y = offsetForLevel(f.level) + 0.9;
      (f.rooms || []).forEach(r => {
        (liveTargets[r.name] || []).forEach(p => {
          // Active floor: an estimated (monocular) spot uses the softer material; a
          // calibrated/room-centred one stays solid. Ghost floors always use the ghost mat.
          const mat = active ? (p.est ? personMatEst : personMat) : personMatGhost;
          const m = new THREE.Mesh(personGeo, mat);
          m.position.set(p.x, y, p.y);
          m.userData.room = r.name;   // §6: lets the render loop pulse only the linked room's markers
          m.userData.baseScale = active ? 1 : 0.6;
          m.scale.setScalar(m.userData.baseScale);
          peopleGroup.add(m);
        });
      });
    });
  }

  // ---- §6: floor tint + cross-highlight, 3D side ----
  // tintRoom3D: ONE source of truth for the floor color — the shared colorFor() scale,
  // lerped toward the dark base so the tint reads as a lit floor, not painted plastic.
  // Cheap (a color write, no geometry), called per live frame from radarUpdate().
  function tintRoom3D(name){
    const e = room3dIdx[name];
    if(!e || !three3d) return;
    const THREE = three3d.THREE;
    // Same tri-state the 2D map paints (mapState), rendered as floor+wall tint so BOTH
    // view modes are honest about disconnect. 'empty' keeps the historic unlit base.
    const st = mapState(name);
    if(st === "occupied"){
      const conf = roomConf[name] ?? 0;
      const t = Math.min(Math.max(conf,0),1);
      const target = new THREE.Color(colorFor(conf));
      e.mesh.material.color.copy(e.base).lerp(target, 0.25 + 0.5 * t);
      // P5 fix 3: this room's walls follow the SAME scale, slightly softer, so the floor
      // stays the lead presence signal — occupied reads green, empty stays dark (§4).
      if(e.wallMat) e.wallMat.color.copy(e.wallBase).lerp(target, 0.2 + 0.45 * t);
    } else if(st === "offline"){
      // A covering camera is offline (F3 latched down): amber attention, NEVER green — the
      // room's decayed/last reading must not read as confident presence.
      const warn = new THREE.Color(MAP_WARN);
      e.mesh.material.color.copy(e.base).lerp(warn, 0.32);
      if(e.wallMat) e.wallMat.color.copy(e.wallBase).lerp(warn, 0.26);
    } else if(st === "privacy"){
      // A covering camera is deliberately covered (Tapo privacy mode): calm blue, NEVER
      // amber/warn — this is a privacy choice made physical, not a fault.
      const priv = new THREE.Color(MAP_PRIVACY);
      e.mesh.material.color.copy(e.base).lerp(priv, 0.28);
      if(e.wallMat) e.wallMat.color.copy(e.wallBase).lerp(priv, 0.22);
    } else if(st === "blind" || st === "unknown"){
      // Two ways of not knowing, deliberately the same family: 'blind' is no
      // sensor here at all, 'unknown' is a sensor that is not running. Slate
      // rather than amber, because neither is a FAULT — one is an absence and
      // the other is usually a choice — and never the dark base, which claims a
      // working sensor looked. 'unknown' is lighter, so the two are told apart
      // on inspection without either being mistaken for confirmed-empty.
      const grey = new THREE.Color(MAP_BLIND);
      const mix = st === "unknown" ? 0.30 : 0.42;
      e.mesh.material.color.copy(e.base).lerp(grey, mix);
      if(e.wallMat) e.wallMat.color.copy(e.wallBase).lerp(grey, mix * 0.72);
    } else {
      e.mesh.material.color.copy(e.base);   // 'empty' — sensor confirms empty: unlit (§4)
      if(e.wallMat) e.wallMat.color.copy(e.wallBase);
    }
  }
  // applyMapLiveness (module-level ref, set here): rebuild roomLiveness from a /api/cameras
  // payload (name+room+liveness enum ONLY — never a frame, url or credential; ADR-0002) and
  // repaint every room in BOTH views. A room is 'offline' if ANY of its cameras is down
  // (a real fault always wins). Otherwise 'live' wins over 'privacy' — a room with one
  // live camera and one covered camera is still covered by a working sensor, so 'privacy'
  // only surfaces when NO camera in the room is actually streaming.
  mapStateOf = mapState;
  // "casa" is the HOUSE-level pseudo-room, and events.py is explicit that it
  // "MUST NEVER be rendered as a room". Three other places in this file already
  // filter it out by hand; this one is published, so it filters once and the
  // callers do not have to know.
  mapRoomNames = () => [...new Set([...Object.keys(roomOcc),
                                    ...Object.keys(roomIdx)])]
                         .filter(isRealRoom);        // HOUSE_ROOM, from shared.js
  applyMapLiveness = (cams)=>{
    const next = {};
    (Array.isArray(cams) ? cams : []).forEach(c=>{
      if(!c || !c.room) return;
      const prev = next[c.room];
      if(c.liveness === "offline") next[c.room] = "offline";
      else if(prev !== "offline"){
        if(c.liveness === "live") next[c.room] = "live";
        else if(c.liveness === "privacy"){
          if(prev !== "live") next[c.room] = "privacy";
        }
        else if(prev == null) next[c.room] = "unknown";
      }
    });
    Object.keys(roomLiveness).forEach(k=>{ if(!(k in next)) delete roomLiveness[k]; });
    Object.assign(roomLiveness, next);
    Object.keys(roomIdx).forEach(paintRoom2D);
    Object.keys(room3dIdx).forEach(tintRoom3D);
  };
  // highlightRoom3D(name, on): emissive boost on the room's floor plate + person-marker
  // pulse (driven by the render loop). Assigned to the module-level ref declared next to
  // radarUpdate so the DOM-side linkRoom() below the closure can call it.
  highlightRoom3D = (name, on) => {
    if(!three3d) return;
    const e = room3dIdx[name];
    if(e) e.mesh.material.emissive.setHex(on ? 0x123a18 : 0x000000);
    if(on){
      hl3d = { room: name, t0: performance.now() };
    } else if(hl3d.room === name){
      hl3d = { room: null, t0: 0 };
      three3d.peopleGroup.children.forEach(m => m.scale.setScalar(m.userData.baseScale || 1));
    }
  };

  // ---- v2 in-3D editor: shared raycast/preview/handle plumbing (used by the 3D pointer
  // handlers wired in initHouseEditor(), live-only). All of it no-ops until 3D exists. ----

  // Screen px -> house metres on a horizontal plane at world height planeY. Returns RAW
  // (unsnapped) coords — callers snap(), because handle drags are delta-based (snapping the
  // delta, not each raw sample, is what keeps a moved rectangle rectangular).
  function screenToGroundPoint3D(evt, planeY){
    if(!three3d) return null;
    const THREE = three3d.THREE;
    const canvas = three3d.renderer.domElement;
    const r = canvas.getBoundingClientRect();
    if(!r.width || !r.height) return null;
    const ndc = new THREE.Vector2(
      ((evt.clientX - r.left) / r.width) * 2 - 1,
      -((evt.clientY - r.top) / r.height) * 2 + 1);
    const rc = new THREE.Raycaster();
    rc.setFromCamera(ndc, three3d.camera);
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -(planeY || 0));
    const out = new THREE.Vector3();
    return rc.ray.intersectPlane(plane, out) ? { x: out.x, y: out.z } : null;
  }

  function raycast3D(evt, objects){
    if(!three3d || !objects || !objects.length) return [];
    const THREE = three3d.THREE;
    const canvas = three3d.renderer.domElement;
    const r = canvas.getBoundingClientRect();
    if(!r.width || !r.height) return [];
    const ndc = new THREE.Vector2(
      ((evt.clientX - r.left) / r.width) * 2 - 1,
      -((evt.clientY - r.top) / r.height) * 2 + 1);
    const rc = new THREE.Raycaster();
    rc.setFromCamera(ndc, three3d.camera);
    return rc.intersectObjects(objects, true);
  }

  // Handle gizmos for the current SELECTED room/wall — only the selection shows handles, to
  // keep the scene legible. Corner spheres (primary) + edge diamonds (secondary) + oversized
  // invisible hit-spheres (generous touch target, small visual). Shared geo/mats → no dispose.
  function rebuildHandles3D(){
    if(!three3d) return;
    const T = three3d, hg = T.handleGroup;
    while(hg.children.length) hg.remove(hg.children[0]);
    hed3dHover = null;
    if(MODE !== "live" || !SELECTED) return;
    const f = floorFor(currentFloor); if(!f) return;
    const y = offsetForLevel(currentFloor) + WALL_H + 0.1;   // just above the wall tops
    const addHandle = (x, z, hKind, idx, geo, mat) => {
      const m = new T.THREE.Mesh(geo, mat);
      m.position.set(x, y, z);
      if(hKind === "edge") m.rotation.y = Math.PI / 4;
      m.renderOrder = 11;
      m.userData = { hKind, idx, isHandle: true };
      hg.add(m);
      const hit = new T.THREE.Mesh(T.hHitGeo, T.hHitMat);
      hit.position.set(x, y, z);
      hit.userData = { hKind, idx, isHandleHit: true, vis: m };
      hg.add(hit);
    };
    if(SELECTED.kind === "room"){
      const room = (f.rooms || []).find(r => r.id === SELECTED.id);
      if(!room || !room.polygon) return;
      room.polygon.forEach((p, i) => addHandle(p[0], p[1], "vertex", i, T.hVertGeo, T.hVertMat));
      room.polygon.forEach((p, i) => {
        const q = room.polygon[(i + 1) % room.polygon.length];
        addHandle((p[0]+q[0])/2, (p[1]+q[1])/2, "edge", i, T.hEdgeGeo, T.hEdgeMat);
      });
    } else if(SELECTED.kind === "wall"){
      const w = (f.walls || []).find(w => w.id === SELECTED.id);
      if(!w || !w.a || !w.b) return;
      addHandle(w.a[0], w.a[1], "wallEnd", 0, T.hVertGeo, T.hVertMat);
      addHandle(w.b[0], w.b[1], "wallEnd", 1, T.hVertGeo, T.hVertMat);
    }
  }

  // Reposition existing handle meshes from an in-progress polygon / wall — the per-move
  // cheap path while dragging (no rebuild).
  function positionHandles3D(g){
    if(!three3d) return;
    three3d.handleGroup.children.forEach(h => {
      const u = h.userData || {};
      if(g.poly){
        if(u.hKind === "vertex" && g.poly[u.idx]){
          h.position.x = g.poly[u.idx][0]; h.position.z = g.poly[u.idx][1];
        } else if(u.hKind === "edge"){
          const a = g.poly[u.idx], b = g.poly[(u.idx + 1) % g.poly.length];
          if(a && b){ h.position.x = (a[0]+b[0])/2; h.position.z = (a[1]+b[1])/2; }
        }
      } else if(g.a && g.b && u.hKind === "wallEnd"){
        const p = u.idx === 0 ? g.a : g.b;
        h.position.x = p[0]; h.position.z = p[1];
      }
    });
  }

  function setPreviewLine3D(pts, planeY){
    const pl = three3d.previewLine;
    const pos = pl.geometry.attributes.position;
    const n = Math.min(pts.length, 63);
    for(let i = 0; i < n; i++) pos.setXYZ(i, pts[i][0], planeY + 0.03, pts[i][1]);
    pos.setXYZ(n, pts[0][0], planeY + 0.03, pts[0][1]);   // close the loop
    pos.needsUpdate = true;
    pl.geometry.setDrawRange(0, n + 1);
    pl.computeLineDistances();                             // LineDashedMaterial requirement
    pl.visible = true;
  }
  function setPreviewWall3D(a, b, planeY){
    const pw = three3d.previewWall;
    const pos = pw.geometry.attributes.position;
    const y0 = planeY, y1 = planeY + WALL_H;
    pos.setXYZ(0, a[0], y0, a[1]); pos.setXYZ(1, b[0], y0, b[1]); pos.setXYZ(2, b[0], y1, b[1]);
    pos.setXYZ(3, a[0], y0, a[1]); pos.setXYZ(4, b[0], y1, b[1]); pos.setXYZ(5, a[0], y1, a[1]);
    pos.needsUpdate = true;
    pw.visible = true;
  }
  function setSnapRing3D(pt, planeY){
    if(!pt){ if(three3d) three3d.snapRing.visible = false; return; }
    three3d.snapRing.position.set(pt[0], planeY + 0.05, pt[1]);
    three3d.snapRing.visible = true;
  }
  function hideHedPreviews3D(){
    if(!three3d) return;
    three3d.previewLine.visible = false;
    three3d.previewWall.visible = false;
    three3d.snapRing.visible = false;
  }

  // One-finger gestures on the 3D canvas belong to edit-drags (not page scroll) whenever a
  // drawing tool is armed or something is selected in live mode — CSS touch-action gate.
  function syncHed3dArm(){
    const c = document.getElementById("radar3d");
    if(!c) return;
    c.classList.toggle("hed3d-arm",
      MODE === "live" && viewMode === "3d" && (TOOL !== "select" || !!SELECTED));
    updateEditingLabel();   // the "Editing" chip tracks the same tool/selection state
  }

  // Short English confirmation per committed edit — same aria-live channel (hedFb) sighted
  // users watch, so screen-reader users get the same signal the preview mesh gives.
  function hedSay(msg){ actionFeedback(document.getElementById("hedFb"), true, null, msg); }

  // "Editing: <floor name>" next to the floor selector — ties the toolbar to whichever
  // floor is currently opaque/active, so an edit's destination is never ambiguous.
  function syncFloorEditing(){
    buildHedRoomSelect();   // audit L1: the keyboard room picker follows the active floor
    updateEditingLabel();
  }
  // The "Editing: <floor>" chip must appear ONLY during an actual edit interaction. The
  // floor-plan editor toolbar (#houseEditor) stays permanently mounted in live mode
  // (initHouseEditor unhides it once), so its presence is NOT "editing" — showing the chip
  // whenever a floor existed made every live VIEW read as "stuck in edit mode" (core-panel
  // UX bug). Real edit signals: a house-editor drawing tool is armed or a plan element is
  // selected, OR the zone editor is toggled on. Plain viewing shows no chip — the floor
  // <select> beside it already names the active floor. The zone flag is read from the DOM
  // (#zoneEdit.on), never the `editMode` binding, which is still in its TDZ at the first
  // call site (buildFloorSelect() runs before `let editMode` is reached during init).
  function updateEditingLabel(){
    const el = document.getElementById("floorEditing");
    if(!el) return;
    const f = floorFor(currentFloor);
    const zoneEl = document.getElementById("zoneEdit");
    const zoneOn = !!(zoneEl && zoneEl.classList.contains("on"));
    const houseEditing = (TOOL !== "select") || !!SELECTED;
    const editing = zoneOn || houseEditing;
    el.hidden = (MODE !== "live") || !f || !editing;
    el.textContent = (f && editing)
      ? WavrT("Editing: {floor}", { floor: f.name || WavrT("level {n}", { n: f.level }) })
      : "";
  }

  async function renderCurrentView(){
    // P5 fix 3 (companion defect): radarUpdate's whole live path — tintRoom3D (floor+wall
    // tint), updatePeople3D and the liveTargets feed — guards on roomIdx, which only
    // rebuildRoomsForFloor() populates. On the 3D-default path it was never called, so the
    // per-frame 3D pipeline silently no-oped (masked, until now, by the walls being green
    // regardless of occupancy). Keep the SVG index in sync in BOTH views — it's cheap, the
    // SVG stays [hidden] in 3D, and entering Floor plan re-runs it at visible size as before.
    rebuildRoomsForFloor();
    if(viewMode === "3d") await render3D();
  }
  // ---- Floor selector (shown only when the house has more than one floor) ----
  function buildFloorSelect(){
    const bar = document.getElementById("floorbar");
    const sel = document.getElementById("floorSelect");
    if(!sel || !bar) return;
    sel.innerHTML = "";
    HOUSE.floors.forEach(fl=>{
      const o = document.createElement("option");
      o.value = fl.level; o.textContent = fl.name || WavrT("level {n}", { n: fl.level });
      sel.appendChild(o);
    });
    sel.value = currentFloor;
    bar.hidden = HOUSE.floors.length < 2;
    syncFloorEditing();
  }
  buildFloorSelect();
  document.getElementById("floorSelect").onchange = (e)=>{
    currentFloor = parseInt(e.target.value, 10);
    rebuildRoomsForFloor();      // 2D stays in sync (cheap; SVG may be [hidden])
    syncFloorEditing();
    rebuildHandles3D();          // selection handles follow the active floor (or vanish)
    if(viewMode === "3d"){
      if(floorGroups.size){
        // Cheap path: no geometry rebuild — re-tint ghost/active + tween the camera over.
        setActiveFloor3D(currentFloor, true, true);
        floor3dShown = currentFloor;
        updatePeople3D();        // ghost/active person materials follow the new floor
      } else {
        renderCurrentView();     // 3D never built yet (first entry still pending)
      }
    }
  };
  renderCurrentView();

  // ---- Zone editor overlay (draw/name/delete rectangles; label occupied zones) ----
  // zoneLayer/drawLayer are created above (fixed order); drawLayer stays on top so the
  // in-progress rectangle is visible while dragging, and room dots stay on top of both.
  const liveTargets = {};                 // room -> [{x,y}] absolute, latest fusion frame
  const zoneEditBtn = document.getElementById("zoneEdit");
  const zoneHintEl  = document.getElementById("zoneHint");
  const zonePanel   = document.getElementById("zonePanel");
  const zoneListEl  = document.getElementById("zoneList");
  const zoneNameForm  = document.getElementById("zoneNameForm");
  const zoneNameInput = document.getElementById("zoneNameInput");
  const zoneRestLabel = document.getElementById("zoneRestLabel");
  const zoneRestInput = document.getElementById("zoneRestInput");
  if(MODE==="companion"){ document.getElementById("zoneBar").hidden = true; }  // viewer: zones view-only (no edit)
  // A9 bed/rest zone checkbox: only meaningful where a real hub exists to persist it into
  // the housemap (wavr.fall_detect reads it server-side) -- demo has no backend to save to.
  if(zoneRestLabel) zoneRestLabel.hidden = (MODE !== "live");
  let editMode = false, pending = null, previewRect = null, dragStart = null;
  const round2 = (n) => Math.round(n*100)/100;

  function absTargetPoint(room, t){       // mirror placeDot's in-room clamp so zone hit-test matches the dot
    const r = 0.12;
    // `positioned` travels with the point because the consumers need it and
    // cannot recover it: a target with no x/y is anchored at the room's centre
    // so the 3D renderer has somewhere to put a marker, and that centre must
    // never be mistaken for a measurement. See zoneOccupied().
    const positioned = !!(t && t.x != null && t.y != null);
    let x = t.x!=null ? room.x + t.x : room.x + room.w/2;
    let y = t.y!=null ? room.y + t.y : room.y + room.h/2;
    x = Math.min(Math.max(x, room.x + r), room.x + room.w - r);
    y = Math.min(Math.max(y, room.y + r), room.y + room.h - r);
    return {x, y, positioned};
  }
  function zoneOccupied(z){
    // liveTargets now holds EVERY floor's rooms (so off-floor presence reaches 3D),
    // but zones are drawn on the ACTIVE floor only, so hit-test ONLY active-floor
    // rooms — else an upstairs target at the same x/y would falsely light a
    // ground-floor zone. Identical behaviour for a single-floor house.
    const f = floorFor(currentFloor);
    for(const room of (f && f.rooms) || []){
      const pts = liveTargets[room.name];
      if(!pts) continue;
      for(const p of pts){
        // A zone is a sub-room AREA, and this is where the room-centre anchor
        // would stop being a drawing and become a claim: an unpositioned target
        // used to light any zone covering the centre of its room -- including
        // the rest zone the server's fall detection reads. No position, no zone.
        if(!p.positioned) continue;
        if(p.x>=z.x && p.x<=z.x+z.w && p.y>=z.y && p.y<=z.y+z.h) return true;
      }
    }
    return false;
  }
  let zoneRects = [], zoneStatus = [];    // built on zone change; per-frame we only toggle .occ
  function drawZones(){
    zoneLayer.textContent = ""; zoneRects = [];
    zones.forEach((z, i) => {
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("class", "radar-zone");
      rect.setAttribute("x", z.x); rect.setAttribute("y", z.y);
      rect.setAttribute("width", z.w); rect.setAttribute("height", z.h);
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("class", "radar-zonelabel");
      label.setAttribute("x", z.x + 0.12); label.setAttribute("y", z.y + 0.3);
      label.textContent = z.name;         // textContent — no markup injection from a zone name
      zoneLayer.appendChild(rect); zoneLayer.appendChild(label);
      zoneRects[i] = rect;
    });
  }
  function renderZoneList(){
    // hidden when empty AND whenever the view isn't the 2D Floor plan — mirrors setViewMode()'s
    // rule, needed now that 3D is the initial view (zones draw on the 2D SVG only).
    zonePanel.hidden = !(zones.length || editMode) || (viewMode !== "top");
    zoneListEl.textContent = ""; zoneStatus = [];
    if(!zones.length){
      const e = document.createElement("div"); e.className = "zone-empty";
      e.textContent = editMode ? WavrT("drag over the map to create a zone")
                               : WavrT("no zones — click “Edit zones” to create one");
      zoneListEl.appendChild(e); return;
    }
    zones.forEach((z, i) => {
      const item = document.createElement("div"); item.className = "zone-item";
      const info = document.createElement("span"); info.className = "zi";
      const name = document.createElement("span"); name.textContent = z.name;
      const status = document.createElement("span");
      info.appendChild(name); info.appendChild(status);
      item.appendChild(info);
      zoneStatus[i] = status;
      if(editMode){
        const rm = document.createElement("button"); rm.type = "button"; rm.className = "rm";
        rm.textContent = WavrT("Remove");
        rm.onclick = () => { zones.splice(i,1); saveZones(zones); refreshZones(); };
        item.appendChild(rm);
      }
      zoneListEl.appendChild(item);
    });
  }
  function updateZoneOcc(){               // per-frame hot path: no DOM rebuild, just toggle occupied
    zones.forEach((z, i) => {
      const occ = zoneOccupied(z);
      if(zoneRects[i]) zoneRects[i].classList.toggle("occ", occ);
      const st = zoneStatus[i];
      if(st){ st.className = occ ? "zocc" : "zempty"; st.textContent = occ ? WavrT("• occupied") : WavrT("empty"); }
    });
  }
  function refreshZones(){ drawZones(); renderZoneList(); updateZoneOcc(); }

  function svgPoint(evt){                  // client px -> radar user-space (metres)
    const ctm = svg.getScreenCTM(); if(!ctm) return null;
    const pt = svg.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
    const p = pt.matrixTransform(ctm.inverse());
    return {x: p.x, y: p.y};
  }
  function updatePreview(a, b){
    if(!previewRect) return;
    previewRect.setAttribute("x", Math.min(a.x,b.x)); previewRect.setAttribute("y", Math.min(a.y,b.y));
    previewRect.setAttribute("width", Math.abs(b.x-a.x)); previewRect.setAttribute("height", Math.abs(b.y-a.y));
  }
  function clearPreview(){ if(previewRect){ previewRect.remove(); previewRect = null; } }
  function cancelPending(){ pending = null; clearPreview(); zoneNameForm.hidden = true; }

  svg.addEventListener("pointerdown", (evt) => {
    if(viewMode!=="top") return;
    if(!editMode) return;
    const p = svgPoint(evt); if(!p) return;
    dragStart = p; clearPreview();
    previewRect = document.createElementNS(SVG_NS, "rect");
    previewRect.setAttribute("class", "radar-draw");
    drawLayer.appendChild(previewRect); updatePreview(p, p);
    try{ svg.setPointerCapture(evt.pointerId); }catch{}
    evt.preventDefault();
  });
  svg.addEventListener("pointermove", (evt) => {
    if(viewMode!=="top") return;
    if(!editMode || !dragStart) return;
    const p = svgPoint(evt); if(p) updatePreview(dragStart, p);
  });
  svg.addEventListener("pointerup", (evt) => {
    if(viewMode!=="top") return;
    if(!editMode || !dragStart) return;
    const s = dragStart, p = svgPoint(evt); dragStart = null;
    try{ svg.releasePointerCapture(evt.pointerId); }catch{}
    if(!p){ clearPreview(); return; }
    const x = Math.min(s.x,p.x), y = Math.min(s.y,p.y), w = Math.abs(p.x-s.x), h = Math.abs(p.y-s.y);
    if(w < 0.3 || h < 0.3){ clearPreview(); return; }     // ignore stray clicks / tiny drags
    pending = {x: round2(x), y: round2(y), w: round2(w), h: round2(h)};
    updatePreview(s, p);                                   // keep the preview while naming
    zoneNameForm.hidden = false; zoneNameInput.value = ""; zoneNameInput.focus();
    if(zoneRestInput) zoneRestInput.checked = false;
  });
  // A9: persist a bed/rest zone into the REAL housemap (wavr.fall_detect.in_rest_zone
  // reads it server-side) -- reuses the existing PUT /api/house save path (saveHouseDoc,
  // defined further below in this same closure -- hoisted, so it is safe to reference
  // here even though its textual definition comes later; this only ever RUNS on a future
  // form submit, well after saveHouseDoc exists). Rectangle -> 4-point polygon on the
  // CURRENT floor, same coordinate space room polygons already use (radar user-space,
  // metres). Note: the local `zones` list (localStorage, per-browser) and the housemap
  // `zones` list (per-hub, what the fall-alert rule reads) are two SEPARATE stores kept
  // only loosely in sync -- see the frontend section of the A9 PR notes.
  function syncRestZoneToHouse(rect, name){
    const f = floorFor(currentFloor); if(!f) return;
    if(!Array.isArray(f.zones)) f.zones = [];
    f.zones.push({
      id: hedUid("z"), name, kind: "rest",
      polygon: [[rect.x, rect.y], [rect.x + rect.w, rect.y],
                [rect.x + rect.w, rect.y + rect.h], [rect.x, rect.y + rect.h]],
    });
    saveHouseDoc();
  }
  zoneNameForm.onsubmit = (e) => {
    e.preventDefault(); if(!pending) return;
    const name = ((zoneNameInput.value || "").trim() || ("zone " + (zones.length+1))).slice(0, 40);
    const isRest = !!(zoneRestInput && zoneRestInput.checked);
    zones.push({id: Date.now(), name, x: pending.x, y: pending.y, w: pending.w, h: pending.h,
               kind: isRest ? "rest" : undefined});
    if(isRest && MODE==="live") syncRestZoneToHouse(pending, name);
    cancelPending(); saveZones(zones); refreshZones();
  };
  document.getElementById("zoneCancel").onclick = cancelPending;
  zoneEditBtn.onclick = () => {
    editMode = !editMode;
    zoneEditBtn.classList.toggle("on", editMode);
    zoneEditBtn.textContent = editMode ? WavrT("Done editing") : WavrT("Edit zones");
    zoneHintEl.hidden = !editMode;
    svg.classList.toggle("editing", editMode);
    if(!editMode) cancelPending();
    refreshZones();
    updateEditingLabel();   // zone edit on/off flips the "Editing" chip too
  };
  refreshZones();

  // ---- House editor (live-only, central): draw/edit rooms/walls/stairs, multi-floor, undo/redo, save ----
  // Builds directly on the v2 HOUSE doc + the T6 rendering closures above (floorFor, rebuildRoomsForFloor,
  // buildFloorSelect, renderHouseOverlay, svgPoint, drawLayer, editMode) — no separate render path.
  function houseSnapshot(){ return JSON.parse(JSON.stringify(HOUSE)); }
  function pushHouseHistory(){
    HOUSE_HISTORY.push(houseSnapshot());
    if(HOUSE_HISTORY.length > 50) HOUSE_HISTORY.shift();
    HOUSE_REDO.length = 0;
    updateHedButtons();
  }
  function markHouseDirty(v){
    DIRTY = v;
    const d = document.getElementById("hedDirty");
    if(d) d.hidden = !v;
  }
  function afterHouseEdit(){
    if(!floorFor(currentFloor)) currentFloor = HOUSE.floors[0].level;   // e.g. undo removed the current floor
    buildFloorSelect();
    renderCurrentView();
    markHouseDirty(true);
    updateHedButtons();
  }
  function updateHedButtons(){
    const u = document.getElementById("hedUndo"), r = document.getElementById("hedRedo"),
          del = document.getElementById("hedDelete");
    if(u) u.disabled = !HOUSE_HISTORY.length;
    if(r) r.disabled = !HOUSE_REDO.length;
    if(del) del.disabled = !SELECTED;
    syncHed3dArm();   // touch-action gate tracks selection state (v2 3D editor)
    buildHedRoomSelect();   // audit L1: the keyboard room picker mirrors SELECTED
  }
  // ---- Audit L1: keyboard path to room selection ----
  // The toolbar's "Room" dropdown mirrors SELECTED both ways: picking a name selects that
  // room (switching back to the Select tool if a draw tool was armed); pointer selections,
  // edits, and floor changes rebuild it. Rooms only — walls/stairs still need a pointer.
  function buildHedRoomSelect(){
    const sel = document.getElementById("hedRoomSel");
    if(!sel) return;
    const f = floorFor(currentFloor);
    const rooms = ((f && f.rooms) || []).filter(r => r.id);
    sel.innerHTML = "";
    const o0 = document.createElement("option");
    o0.value = ""; o0.textContent = rooms.length ? WavrT("select a room…") : WavrT("no rooms yet");
    sel.appendChild(o0);
    rooms.forEach(r => {
      const o = document.createElement("option");
      o.value = r.id; o.textContent = r.name || WavrT("unnamed room");   // textContent only — names are user data
      sel.appendChild(o);
    });
    sel.disabled = !rooms.length;
    sel.value = (SELECTED && SELECTED.kind === "room" && rooms.some(r => r.id === SELECTED.id))
      ? SELECTED.id : "";
  }
  (function(){
    const sel = document.getElementById("hedRoomSel");
    if(!sel) return;
    sel.addEventListener("change", ()=>{
      if(MODE !== "live") return;                    // editor is central-only (hidden elsewhere anyway)
      const id = sel.value;
      if(!id){ if(SELECTED) selectHouseEl(SELECTED.kind, SELECTED.id); return; }   // placeholder = clear (toggle off)
      if(TOOL !== "select"){
        const b = document.querySelector('#houseEditor [data-tool="select"]');
        if(b) b.click();   // reuse the tool button's own state sync (aria-pressed, cursor, hint)
      }
      if(SELECTED && SELECTED.kind === "room" && SELECTED.id === id) return;   // already selected — selectHouseEl would toggle OFF
      selectHouseEl("room", id);
    });
  })();
  function undoHouse(){
    if(!HOUSE_HISTORY.length) return;
    HOUSE_REDO.push(houseSnapshot());
    HOUSE = HOUSE_HISTORY.pop();
    SELECTED = null;
    afterHouseEdit();
  }
  function redoHouse(){
    if(!HOUSE_REDO.length) return;
    HOUSE_HISTORY.push(houseSnapshot());
    HOUSE = HOUSE_REDO.pop();
    SELECTED = null;
    afterHouseEdit();
  }
  let hedUidN = 0;
  function hedUid(prefix){ hedUidN += 1; return prefix + "_" + Date.now().toString(36) + hedUidN; }
  function selectHouseEl(kind, id){
    if(MODE !== "live" || TOOL !== "select") return;   // belt-and-suspenders: CSS also gates click reachability
    SELECTED = (SELECTED && SELECTED.kind===kind && SELECTED.id===id) ? null : {kind, id};   // click again = deselect
    renderHouseOverlay(floorFor(currentFloor));
    rebuildHandles3D();   // one selection model, two views: 3D handles mirror the 2D outline
    updateHedButtons();
    // Audit L1: selection was never announced — use the same aria-live channel (#hedFb)
    // every committed edit already speaks through, for pointer AND keyboard paths alike.
    if(!SELECTED){ hedSay(WavrT("Selection cleared.")); return; }
    let what = kind;
    const f = floorFor(currentFloor);
    if(kind === "room"){
      const r = f && (f.rooms||[]).find(x => x.id === id);
      what = (r && r.name) ? r.name : WavrT("room");
    } else if(kind === "feature"){
      const ft = f && (f.features||[]).find(x => x.id === id);
      what = (ft && ft.type) ? ft.type : WavrT("marker");
    }
    hedSay(WavrT("Selected: {what}.", { what: what }));   // actionFeedback renders via textContent — untrusted names stay inert
  }
  function deleteSelectedHouseEl(){
    if(!SELECTED) return;
    const f = floorFor(currentFloor); if(!f) return;
    pushHouseHistory();
    const key = SELECTED.kind==="room" ? "rooms" : SELECTED.kind==="wall" ? "walls" : "features";
    f[key] = (f[key]||[]).filter(o => o.id !== SELECTED.id);
    SELECTED = null;
    afterHouseEdit();
  }
  function addHouseFloor(){
    pushHouseHistory();
    const nl = Math.max(...HOUSE.floors.map(f=>f.level)) + 1;
    HOUSE.floors.push({id: hedUid("f"), name: "Floor " + nl, level: nl, rooms: [], walls: [], features: [], backdrop: null});
    currentFloor = nl;
    afterHouseEdit();
  }
  async function saveHouseDoc(){
    const fb = document.getElementById("hedFb");
    try{
      const r = await WavrAPI.fetch("/api/house", {method: "PUT", json: HOUSE});
      if(r.ok){ HOUSE = await r.json(); markHouseDirty(false); buildFloorSelect(); renderCurrentView(); actionFeedback(fb, true); }
      else { actionFeedback(fb, false, WavrT("save failed (") + r.status + ")"); }
    }catch{ actionFeedback(fb, false, WavrT("connection failed while saving")); }
  }

  function initHouseEditor(){
    if(MODE !== "live") return;              // editor is central-only; demo + companion never see it or PUT
    const ed = document.getElementById("houseEditor"); if(!ed) return;
    ed.hidden = false;   // v2: the toolbar stays mounted in BOTH views — the pointer-handler
                         // wiring below branches on viewMode (2D on #radar, 3D on #radar3d)
    svg.classList.add("hed-live");
    let hedStart = null, hedPreview = null;
    function cancelHedDraw(){ hedStart = null; if(hedPreview){ hedPreview.remove(); hedPreview = null; } }
    cancelHedDrawIfActive = cancelHedDraw;   // expose to setViewMode() (defined outside this closure)

    // ==== v2 in-3D editor: pointer handlers on #radar3d, guarded by viewMode==="3d" ====
    // Mirrors the 2D handlers below exactly in contract: same TOOL set, same snap grid, same
    // pushHouseHistory→mutate→afterHouseEdit pipeline, same HOUSE fields. These listeners are
    // registered BEFORE OrbitControls' own (ensure3D() resolves asynchronously later), so a
    // recognized edit-drag can set controls.enabled=false before OrbitControls sees the event
    // — the single trick that stops orbit from fighting the drag. Controls are re-enabled on
    // pointerup/cancel; an unrecognized pointerdown never touches them, so orbiting works
    // exactly as before.
    const canvas3d = document.getElementById("radar3d");
    let hed3dDownPt = null;   // click (≤6px) detection for select/stairs

    function beginHed3d(evt, drag){
      hed3d = drag;
      hed3d.pointerId = evt.pointerId;   // a second touch mid-drag must never move/commit it
      three3d.controls.enabled = false;
      try{ canvas3d.setPointerCapture(evt.pointerId); }catch{}
      evt.preventDefault();
    }
    function cancelHed3d(){
      if(!hed3d) return;
      hed3d = null;
      hideHedPreviews3D();
      rebuildHandles3D();          // restore handle positions + idle tint (HOUSE was never mutated)
      if(three3d) three3d.controls.enabled = true;
    }
    cancelHed3dIfActive = cancelHed3d;   // expose to setViewMode()

    // The in-progress geometry for the active drag: base + snapped delta (move/resize) or
    // origin + snapped point (draw). Pure function of the latest pointer event; HOUSE is
    // never written here — commit happens once, on pointerup.
    function hed3dWorking(evt){
      const p = screenToGroundPoint3D(evt, hed3d.planeY);
      if(!p) return hed3d.last || null;   // ray missed the plane: hold the last good geometry
      let g = null;
      const k = hed3d.kind;
      if(k === "draw-room"){
        const x0 = hed3d.origin.x, y0 = hed3d.origin.y, x1 = snap(p.x), y1 = snap(p.y);
        const x = Math.min(x0,x1), y = Math.min(y0,y1), X = Math.max(x0,x1), Y = Math.max(y0,y1);
        g = { poly: [[x,y],[X,y],[X,Y],[x,Y]], snapPt: [x1,y1] };
      } else if(k === "draw-wall"){
        const a = [hed3d.origin.x, hed3d.origin.y], b = [snap(p.x), snap(p.y)];
        g = { a, b, snapPt: b };
      } else {
        const dx = p.x - hed3d.p0.x, dy = p.y - hed3d.p0.y;
        if(k === "move-room"){
          const sdx = snap(dx), sdy = snap(dy);   // snap the DELTA — rectangularity survives
          g = { poly: hed3d.base.map(([x,y]) => [x+sdx, y+sdy]), snapPt: null };
        } else if(k === "resize-vertex"){
          const poly = hed3d.base.map(q => [q[0], q[1]]);
          const b = hed3d.base[hed3d.vIdx];
          poly[hed3d.vIdx] = [snap(b[0]+dx), snap(b[1]+dy)];
          g = { poly, snapPt: poly[hed3d.vIdx] };
        } else if(k === "resize-edge"){
          const poly = hed3d.base.map(q => [q[0], q[1]]);
          const sdx = snap(dx), sdy = snap(dy);
          const i = hed3d.eIdx, j = (i+1) % poly.length;
          poly[i] = [hed3d.base[i][0]+sdx, hed3d.base[i][1]+sdy];
          poly[j] = [hed3d.base[j][0]+sdx, hed3d.base[j][1]+sdy];
          g = { poly, snapPt: [(poly[i][0]+poly[j][0])/2, (poly[i][1]+poly[j][1])/2] };
        } else if(k === "wall-endpoint"){
          const pts = hed3d.base.map(q => [q[0], q[1]]);
          const b = hed3d.base[hed3d.vIdx];
          pts[hed3d.vIdx] = [snap(b[0]+dx), snap(b[1]+dy)];
          g = { a: pts[0], b: pts[1], snapPt: pts[hed3d.vIdx] };
        }
      }
      if(g) hed3d.last = g;
      return g;
    }

    function hed3dPreview(evt){
      const g = hed3dWorking(evt);
      if(!g) return;
      const floorY = offsetForLevel(currentFloor);
      // Handles follow the working geometry ONLY when the drag is about the selection —
      // a draw-room/draw-wall drag must never drag another (still-selected) room's handles.
      const aboutSelection = hed3d.kind !== "draw-room" && hed3d.kind !== "draw-wall";
      if(g.poly){ setPreviewLine3D(g.poly, floorY); if(aboutSelection) positionHandles3D(g); }
      else if(g.a && g.b){ setPreviewWall3D(g.a, g.b, floorY); if(aboutSelection) positionHandles3D(g); }
      setSnapRing3D(g.snapPt, floorY);
    }

    // Turn the grabbed handle amber for the drag's lifetime (spec's idle→hover→drag ramp).
    function tintDraggedHandle(u){
      if(!three3d) return;
      three3d.handleGroup.children.forEach(h => {
        const hu = h.userData || {};
        if(hu.isHandle && hu.hKind === u.hKind && hu.idx === u.idx) h.material = three3d.hDragMat;
      });
    }

    canvas3d.addEventListener("pointerdown", (evt)=>{
      hed3dDownPt = { x: evt.clientX, y: evt.clientY };
      if(viewMode!=="3d" || MODE!=="live" || !three3d || hed3d) return;
      if(evt.button !== undefined && evt.button !== 0) return;   // left button / touch only
      const f = floorFor(currentFloor); if(!f) return;
      const planeY = offsetForLevel(currentFloor);

      if(TOOL==="room" || TOOL==="wall"){
        const p = screenToGroundPoint3D(evt, planeY);
        if(!p) return;
        beginHed3d(evt, { kind: TOOL==="room" ? "draw-room" : "draw-wall",
                          planeY, origin: { x: snap(p.x), y: snap(p.y) } });
        hed3dPreview(evt);
        return;
      }
      if(TOOL!=="select") return;   // stairs = single click, committed on pointerup

      // 1) Handle hit? (highest priority — resize/reshape/wall-endpoint)
      const hHit = raycast3D(evt, three3d.handleGroup.children)
        .find(h => h.object.userData && (h.object.userData.isHandleHit || h.object.userData.isHandle));
      if(hHit && SELECTED){
        const u = hHit.object.userData;
        const hy = planeY + WALL_H + 0.1;          // drag plane at the handles' own height —
        const p0 = screenToGroundPoint3D(evt, hy); // no parallax jump on grab
        if(!p0) return;
        if(SELECTED.kind==="room"){
          const room = (f.rooms||[]).find(r => r.id===SELECTED.id);
          if(!room || !room.polygon) return;
          const base = room.polygon.map(q => [q[0], q[1]]);
          if(u.hKind==="vertex"){
            beginHed3d(evt, { kind:"resize-vertex", planeY: hy, p0, roomId: room.id, vIdx: u.idx, base });
          } else if(u.hKind==="edge"){
            if(evt.shiftKey){
              // Add-vertex-from-edge (Figma-style): Shift-dragging an edge handle inserts a
              // new vertex at that edge, then continues as a normal vertex drag — this is
              // how a 4-gon becomes a 5-gon (L-shapes) without a separate tool.
              const a = base[u.idx], b = base[(u.idx+1)%base.length];
              const mid = [snap((a[0]+b[0])/2), snap((a[1]+b[1])/2)];
              const grown = base.slice(); grown.splice(u.idx+1, 0, mid);
              beginHed3d(evt, { kind:"resize-vertex", planeY: hy, p0, roomId: room.id,
                                vIdx: u.idx+1, base: grown, grew: true });
            } else {
              beginHed3d(evt, { kind:"resize-edge", planeY: hy, p0, roomId: room.id, eIdx: u.idx, base });
            }
          }
        } else if(SELECTED.kind==="wall" && u.hKind==="wallEnd"){
          const w = (f.walls||[]).find(w => w.id===SELECTED.id);
          if(!w || !w.a || !w.b) return;
          beginHed3d(evt, { kind:"wall-endpoint", planeY: hy, p0, wallId: w.id, vIdx: u.idx,
                            base: [[w.a[0], w.a[1]], [w.b[0], w.b[1]]] });
        }
        if(hed3d) tintDraggedHandle(u);
        return;
      }

      // 2) Selected room's own body (floor plate or wall face)? → rigid move
      if(SELECTED && SELECTED.kind==="room"){
        const grp = floorGroups.get(currentFloor);
        const hit = grp ? raycast3D(evt, grp.children)
          .find(h => h.object.userData && h.object.userData.roomId === SELECTED.id) : null;
        if(hit){
          const room = (f.rooms||[]).find(r => r.id===SELECTED.id);
          const p0 = screenToGroundPoint3D(evt, planeY);
          if(room && room.polygon && p0){
            beginHed3d(evt, { kind:"move-room", planeY, p0, roomId: room.id,
                              base: room.polygon.map(q => [q[0], q[1]]) });
            canvas3d.style.cursor = "move";
          }
        }
      }
      // else: not edit-capable — fall through untouched, OrbitControls owns the drag.
    });

    canvas3d.addEventListener("pointermove", (evt)=>{
      if(hed3d){ if(evt.pointerId === hed3d.pointerId) hed3dPreview(evt); return; }
      // Hover feedback — mouse only (no touch hover), select tool only.
      if(viewMode!=="3d" || MODE!=="live" || !three3d || evt.pointerType!=="mouse" || TOOL!=="select") return;
      let cursor = "";
      const hHit = raycast3D(evt, three3d.handleGroup.children)
        .find(h => h.object.userData && (h.object.userData.isHandleHit || h.object.userData.isHandle));
      const vis = hHit ? (hHit.object.userData.vis || hHit.object) : null;
      if(hed3dHover && hed3dHover !== vis){
        hed3dHover.material = hed3dHover.userData.hKind==="edge" ? three3d.hEdgeMat : three3d.hVertMat;
        hed3dHover.scale.setScalar(1);
        hed3dHover = null;
      }
      if(vis){
        if(hed3dHover !== vis){
          hed3dHover = vis;
          vis.material = three3d.hHoverMat;
          vis.scale.setScalar(1.3);
        }
        cursor = "grab";
      } else if(SELECTED && SELECTED.kind==="room"){
        const grp = floorGroups.get(currentFloor);
        const hit = grp ? raycast3D(evt, grp.children)
          .find(h => h.object.userData && h.object.userData.roomId === SELECTED.id) : null;
        if(hit) cursor = "move";
      }
      canvas3d.style.cursor = cursor;
    });

    function commitHed3d(evt){
      const g = hed3dWorking(evt);        // final geometry from the release point
      const d = hed3d;
      hed3d = null;
      try{ canvas3d.releasePointerCapture(evt.pointerId); }catch{}
      hideHedPreviews3D();
      if(three3d) three3d.controls.enabled = true;
      canvas3d.style.cursor = TOOL==="select" ? "" : "crosshair";
      const f = floorFor(currentFloor);
      if(!f || !g){ rebuildHandles3D(); return; }
      // History discipline: push once, immediately before the single mutation — identical to
      // the 2D handlers. A cancelled/no-op drag therefore never costs an undo step (and never
      // clears the redo stack).
      if(d.kind === "draw-room"){
        const [[x, y],, [X, Y]] = g.poly;
        if(X - x >= EDIT_SNAP && Y - y >= EDIT_SNAP){
          pushHouseHistory();
          f.rooms = f.rooms || [];
          f.rooms.push({ id: hedUid("r"), name: "room " + (f.rooms.length + 1), polygon: g.poly });
          afterHouseEdit();
          hedSay(WavrT("Room added."));
        } else rebuildHandles3D();
      } else if(d.kind === "draw-wall"){
        if(Math.hypot(g.b[0]-g.a[0], g.b[1]-g.a[1]) >= EDIT_SNAP){
          pushHouseHistory();
          f.walls = f.walls || [];
          f.walls.push({ id: hedUid("w"), a: [g.a[0], g.a[1]], b: [g.b[0], g.b[1]] });
          afterHouseEdit();
          hedSay(WavrT("Wall added."));
        } else rebuildHandles3D();
      } else if(d.kind === "wall-endpoint"){
        const w = (f.walls||[]).find(w => w.id === d.wallId);
        if(w && JSON.stringify([g.a, g.b]) !== JSON.stringify([w.a, w.b])){
          pushHouseHistory();
          w.a = g.a; w.b = g.b;
          afterHouseEdit();
          hedSay(WavrT("Wall updated."));
        } else rebuildHandles3D();
      } else if(g.poly){                   // move-room / resize-vertex / resize-edge
        const room = (f.rooms||[]).find(r => r.id === d.roomId);
        if(room && JSON.stringify(g.poly) !== JSON.stringify(room.polygon)){
          pushHouseHistory();
          room.polygon = g.poly;
          afterHouseEdit();
          hedSay(d.kind === "move-room" ? WavrT("Room moved.") : d.grew ? WavrT("Room reshaped.") : WavrT("Room resized."));
        } else rebuildHandles3D();
      } else rebuildHandles3D();
    }

    canvas3d.addEventListener("pointerup", (evt)=>{
      const down = hed3dDownPt; hed3dDownPt = null;
      if(hed3d){
        // Audit H2: a drag commit (or a stray 2nd touch mid-drag) is an edit, not a room
        // pick — flag it so the cross-highlight handler (registered later) skips this event.
        evt.__wavrHedConsumed = true;
        if(evt.pointerId === hed3d.pointerId) commitHed3d(evt);
        return;
      }
      if(viewMode!=="3d" || MODE!=="live" || !three3d) return;
      if(evt.button !== undefined && evt.button > 0) return;   // primary button / touch only
      // Click path (≤6px, same threshold as the view-select handler): stairs placement or
      // editor selection. Real orbit-drags fall through here untouched.
      if(!down || Math.hypot(evt.clientX-down.x, evt.clientY-down.y) > 6) return;
      const f = floorFor(currentFloor); if(!f) return;
      if(TOOL==="stairs"){
        const p = screenToGroundPoint3D(evt, offsetForLevel(currentFloor));
        if(!p) return;
        pushHouseHistory();
        f.features = f.features || [];
        f.features.push({ id: hedUid("s"), type: "stairs", at: [snap(p.x), snap(p.y)], to_level: currentFloor + 1 });
        afterHouseEdit();
        hedSay(WavrT("Stairs added."));
        evt.__wavrHedConsumed = true;   // audit H2: stairs placement is an edit — no cross-highlight
        return;
      }
      if(TOOL!=="select") return;
      // Scoped to the ACTIVE floor's group — ghost floors are non-interactive by design.
      const grp = floorGroups.get(currentFloor); if(!grp) return;
      const hit = raycast3D(evt, grp.children).find(h => h.object.userData &&
        (h.object.userData.roomId || h.object.userData.wallId || h.object.userData.featureId));
      if(!hit) return;
      const u = hit.object.userData;
      if(u.featureId) selectHouseEl("feature", u.featureId);
      else if(u.wallId) selectHouseEl("wall", u.wallId);
      else if(u.roomId) selectHouseEl("room", u.roomId);
      // Audit H2: the editor consumed this click — mutually exclusive with __wavrSelectRoom's
      // presence-card scroll/focus (which would otherwise also fire on the same event).
      evt.__wavrHedConsumed = true;
    });

    canvas3d.addEventListener("pointercancel", ()=>{ hed3dDownPt = null; cancelHed3d(); });
    function updateHedPreview(a, b){
      if(!hedPreview) return;
      if(TOOL === "room"){
        hedPreview.setAttribute("x", Math.min(a.x,b.x)); hedPreview.setAttribute("y", Math.min(a.y,b.y));
        hedPreview.setAttribute("width", Math.abs(b.x-a.x)); hedPreview.setAttribute("height", Math.abs(b.y-a.y));
      } else {
        hedPreview.setAttribute("x1", a.x); hedPreview.setAttribute("y1", a.y);
        hedPreview.setAttribute("x2", b.x); hedPreview.setAttribute("y2", b.y);
      }
    }
    document.querySelectorAll('#houseEditor [data-tool]').forEach(b=>{
      b.onclick = ()=>{
        TOOL = b.dataset.tool;
        document.querySelectorAll('#houseEditor [data-tool]').forEach(x => {
          x.classList.toggle("on", x===b);
          x.setAttribute("aria-pressed", x===b ? "true" : "false");   // announce the active tool
        });
        svg.classList.toggle("hed-drawing", TOOL==="room" || TOOL==="wall" || TOOL==="stairs");
        cancelHedDraw();
        cancelHed3d();
        canvas3d.style.cursor = TOOL==="select" ? "" : "crosshair";
        syncHed3dArm();
      };
    });
    document.getElementById("hedUndo").onclick = undoHouse;
    document.getElementById("hedRedo").onclick = redoHouse;
    document.getElementById("hedSave").onclick = saveHouseDoc;
    document.getElementById("hedAddFloor").onclick = ()=>{
      addHouseFloor();
      hedSay(WavrT("New floor added — use + Room to start it."));
    };
    document.getElementById("hedDelete").onclick = ()=>{
      if(!SELECTED) return;
      deleteSelectedHouseEl();
      hedSay(WavrT("Deleted."));
    };

    // ---- Export / import the house map (client-side only; import re-uses the save path) ----
    document.getElementById("hedExport").onclick = ()=>{
      const blob = new Blob([JSON.stringify(HOUSE, null, 2)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "wavr-house.json";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    };
    const importFile = document.getElementById("hedImportFile");
    document.getElementById("hedImportBtn").onclick = ()=> importFile.click();
    importFile.onchange = async ()=>{
      const file = importFile.files && importFile.files[0];
      importFile.value = "";          // reset so importing the same filename again still fires onchange
      if(!file) return;
      let parsed;
      try{ parsed = JSON.parse(await file.text()); }
      catch{ alert(WavrT("Import failed: invalid JSON")); return; }
      if(!parsed || !Array.isArray(parsed.floors) || !parsed.floors.length){
        alert(WavrT("Import failed: invalid floor plan (missing floors)")); return;
      }
      HOUSE = parsed;
      SELECTED = null;
      if(!floorFor(currentFloor)) currentFloor = HOUSE.floors[0].level;
      await saveHouseDoc();           // existing save path: PUT /api/house; re-renders on ok, alerts on failure
    };

    updateHedButtons();

    svg.addEventListener("pointerdown", (evt)=>{
      if(viewMode!=="top") return;
      if(MODE!=="live" || editMode) return;             // never fight the zone editor for the same drag
      if(TOOL==="select" || TOOL==="stairs") return;     // stairs = single click, handled on pointerup
      const p = svgPoint(evt); if(!p) return;
      hedStart = {x: snap(p.x), y: snap(p.y)};
      hedPreview = document.createElementNS(SVG_NS, TOOL==="room" ? "rect" : "line");
      hedPreview.setAttribute("class", TOOL==="room" ? "radar-draw" : "wall");
      if(TOOL==="wall") hedPreview.setAttribute("stroke-dasharray", ".08 .06");
      drawLayer.appendChild(hedPreview);
      updateHedPreview(hedStart, hedStart);
      try{ svg.setPointerCapture(evt.pointerId); }catch{}
      evt.preventDefault();
    });
    svg.addEventListener("pointermove", (evt)=>{
      if(viewMode!=="top") return;
      if(MODE!=="live" || editMode || !hedStart) return;
      const p = svgPoint(evt); if(p) updateHedPreview(hedStart, {x: snap(p.x), y: snap(p.y)});
    });
    svg.addEventListener("pointerup", (evt)=>{
      if(viewMode!=="top") return;
      if(MODE!=="live" || editMode) return;
      const f = floorFor(currentFloor);
      if(TOOL==="stairs"){
        const p = svgPoint(evt); if(!p || !f) return;
        pushHouseHistory();
        f.features = f.features || [];
        f.features.push({id: hedUid("s"), type:"stairs", at:[snap(p.x), snap(p.y)], to_level: currentFloor+1});
        afterHouseEdit();
        return;
      }
      if(!hedStart) return;
      const p = svgPoint(evt);
      const end = p ? {x: snap(p.x), y: snap(p.y)} : hedStart;
      try{ svg.releasePointerCapture(evt.pointerId); }catch{}
      if(TOOL==="room" && f){
        const x = Math.min(hedStart.x,end.x), y = Math.min(hedStart.y,end.y);
        const X = Math.max(hedStart.x,end.x), Y = Math.max(hedStart.y,end.y);
        if(X-x >= EDIT_SNAP && Y-y >= EDIT_SNAP){
          pushHouseHistory();
          f.rooms = f.rooms || [];
          f.rooms.push({id: hedUid("r"), name: "room "+(f.rooms.length+1), polygon:[[x,y],[X,y],[X,Y],[x,Y]]});
          afterHouseEdit();
        }
      } else if(TOOL==="wall" && f){
        if(Math.hypot(end.x-hedStart.x, end.y-hedStart.y) >= EDIT_SNAP){
          pushHouseHistory();
          f.walls = f.walls || [];
          f.walls.push({id: hedUid("w"), a:[hedStart.x,hedStart.y], b:[end.x,end.y]});
          afterHouseEdit();
        }
      }
      cancelHedDraw();
    });
    window.addEventListener("keydown", (e)=>{
      if(MODE!=="live") return;
      if(e.ctrlKey && (e.key==="z"||e.key==="Z") && !e.shiftKey){ e.preventDefault(); undoHouse(); }
      if(e.ctrlKey && (e.key==="y"||e.key==="Y" || (e.shiftKey && (e.key==="z"||e.key==="Z")))){ e.preventDefault(); redoHouse(); }
      // A11y: arrow keys nudge the SELECTED room by one snap step (Shift = 1 m) — a real
      // keyboard path to repositioning, in both views, without any pointer at all.
      const ARROWS = { ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1] };
      if(ARROWS[e.key] && SELECTED && SELECTED.kind==="room" && !e.ctrlKey && !e.altKey && !e.metaKey){
        const t = e.target;
        if(t && (t.tagName==="INPUT" || t.tagName==="SELECT" || t.tagName==="TEXTAREA" || t.isContentEditable)) return;
        const ed2 = document.getElementById("houseEditor");
        if(!ed2 || ed2.hidden || ed2.offsetParent === null) return;   // editor not on screen
        const f = floorFor(currentFloor);
        const room = f && (f.rooms||[]).find(r => r.id === SELECTED.id);
        if(!room || !room.polygon) return;
        e.preventDefault();
        const step = e.shiftKey ? 1.0 : EDIT_SNAP;
        const [ux, uy] = ARROWS[e.key];
        pushHouseHistory();
        room.polygon = room.polygon.map(([x, y]) => [snap(x + ux*step), snap(y + uy*step)]);
        afterHouseEdit();
        hedSay(WavrT("Room moved."));
      }
    });
    // Escape cancels an in-progress draw/drag in either view (mirrors pointercancel).
    // Capture phase, and stops propagation ONLY while a drag is live — so the same Escape
    // never also closes the Settings overlay out from under the drawing (the shell's own
    // document-level Escape→closeGear handler runs in the bubble phase, after this).
    window.addEventListener("keydown", (e)=>{
      if(e.key!=="Escape" || (!hed3d && !hedStart)) return;
      e.stopPropagation();
      cancelHedDraw();
      cancelHed3d();
    }, true);
    syncHedHint();      // view-aware toolbar help line (2D vs 3D copy)
    syncHed3dArm();
    syncFloorEditing();
  }
  initHouseEditor();

  // ---- View toggle wiring (top-down <-> 3D). Both buttons are visible in every MODE — 3D
  // is a read-only lens on data already loaded client-side, so the public demo gets it too. ----
  // View-aware map sub-copy: the tile must not claim "3D" while the user is on Floor plan
  // (cosmetic Stage-1 note, fixed in Stage 3a).
  function syncRadarSub(){
    const el = document.getElementById("radarSub");
    if(!el) return;
    el.textContent = viewMode === "3d"
      ? WavrT("Your Space in 3D: the dots are people. Drag to orbit; “Floor plan” is the precise 2D view.")
      : WavrT("Floor plan: the dots are people. “3D” goes back to the perspective view.");
  }
  // View-aware house-editor help line (same pattern as syncRadarSub): the toolbar now works
  // in BOTH views, so the hint teaches whichever input surface is active.
  function syncHedHint(){
    const el = document.getElementById("hedHint");
    if(!el) return;
    el.textContent = viewMode === "3d"
      ? WavrT("Select: click a room to grab it — drag its body to move, a corner to resize, an edge to push a wall out (Shift-drag an edge adds a corner) · + Room/+ Wall: drag on the ground · + Stairs: click on the ground. Two fingers (or right-drag) still orbits. Arrow keys nudge the selected room.")
      : WavrT("Select: click a room/wall/stairs to mark and delete it · + Room/+ Wall: drag on the plan · + Stairs: click on the plan. Arrow keys nudge the selected room.");
  }
  function setViewMode(mode){
    if(mode === viewMode) return;
    if(editMode){ editMode=false; zoneEditBtn.classList.remove("on"); zoneEditBtn.textContent=WavrT("Edit zones");
                  zoneHintEl.hidden=true; svg.classList.remove("editing"); cancelPending(); }
    cancelHedDrawIfActive();
    cancelHed3dIfActive();   // never leak an in-progress 3D drag across a view switch
    viewMode = mode;
    try{ localStorage.setItem(VIEW_KEY, mode); }catch{}   // persist the explicit choice (§4)
    syncRadarSub();
    // P5 fix 6: aria-pressed mirrors the .on class (the .fchip toggles already do this).
    const vtb = document.getElementById("viewTop"), v3b = document.getElementById("view3d");
    vtb.classList.toggle("on", mode==="top");
    vtb.setAttribute("aria-pressed", mode==="top" ? "true" : "false");
    v3b.classList.toggle("on", mode==="3d");
    v3b.setAttribute("aria-pressed", mode==="3d" ? "true" : "false");
    // toggleAttribute (not the .hidden IDL property): <svg> is foreign/SVG-namespace
    // content and setting .hidden on it does not reliably reflect to the "hidden"
    // content attribute in every engine, which would silently break the [hidden] CSS
    // above and the svgPoint()/getScreenCTM() null-return guard the 6 pointer listeners
    // rely on. toggleAttribute works uniformly for both the <svg> and the <canvas>.
    svg.toggleAttribute("hidden", mode !== "top");
    document.getElementById("radar3d").toggleAttribute("hidden", mode !== "3d");
    const zb = document.getElementById("zoneBar"), zp = document.getElementById("zonePanel");
    // Companion (LAN viewer) never gets zone-edit back, even after a 3D->top toggle — mirrors
    // the init-time "viewer: zones view-only" rule (MODE==="companion" hides zoneBar once, up
    // front); without the MODE check here this unconditional toggle would silently re-show it.
    if(zb) zb.hidden = (mode!=="top") || (MODE==="companion");
    if(zp) zp.hidden = (mode!=="top") || !(zones.length || editMode);
    // v2: #houseEditor stays mounted in BOTH views (live only — initHouseEditor unhid it);
    // only the hint copy and the touch-action gate track the view.
    syncHedHint();
    syncHed3dArm();
    // Only stop here — starting is owned by render3D() itself (see its comment): on the
    // first-ever entry into 3D, three3d doesn't exist yet at this synchronous point, so a
    // start attempt here would silently no-op and the canvas would stay blank forever.
    if(mode!=="3d") stopLoop3D();
    renderCurrentView();
  }
  document.getElementById("viewTop").onclick = ()=> setViewMode("top");
  document.getElementById("view3d").onclick  = ()=> setViewMode("3d");

  // ---- Core-panel map collapse (three states: collapsed -> tap to view -> explicit edit) ----
  // On the landscape/short panel the map is collapsed by default (CSS), presenting only the
  // "View house map" bar so the room measurements are the glanceable hero and no touch-capturing
  // canvas sits in the scroll path. Tapping expands it for VIEWING (orbit/pan) — a deliberate,
  // opt-in action; editing (zone / floor-plan tools) stays a separate explicit mode, untouched.
  // Guarded by matchMedia so the (display:none-on-desktop) button is inert everywhere but the
  // panel: a stray click never pauses the always-on desktop map loop.
  (function(){
    const mapHome = document.getElementById("mapHome");
    const toggle  = document.getElementById("mapPanelToggle");
    if(!mapHome || !toggle) return;
    const PANEL_MQ = window.matchMedia("(orientation:landscape) and (max-height:820px) and (min-aspect-ratio:2/1)");
    // display:none -> visible sizes the WebGL canvas via its ResizeObserver (resize3D fixes the
    // camera aspect); we only nudge the render loop so a collapsed panel isn't spending frames.
    function afterExpand(){
      if(viewMode === "3d"){ try{ resize3D(); startLoop3D(); }catch(e){} }
    }
    toggle.onclick = function(){
      if(!PANEL_MQ.matches) return;                 // desktop: button is hidden; do nothing
      const open = mapHome.classList.toggle("map-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if(open){ requestAnimationFrame(afterExpand); }
      else { try{ stopLoop3D(); }catch(e){} }        // collapsed: stop burning frames on a hidden canvas
    };
    // Fix 2: tapping a room row opens (never toggles closed) the collapsed map, then focuses
    // that room on it. Idempotent — a no-op when already open or off the panel form factor.
    window.__wavrPanelOpenMap = function(){
      if(!PANEL_MQ.matches || mapHome.classList.contains("map-open")) return;
      mapHome.classList.add("map-open");
      toggle.setAttribute("aria-expanded", "true");
      requestAnimationFrame(afterExpand);
    };
    // Leaving the panel form factor (rotate / resize to desktop): the map becomes permanently
    // visible again, so drop the collapse state and make sure the loop is live (never frozen).
    const onMq = function(e){
      if(!e.matches){
        mapHome.classList.remove("map-open");
        toggle.setAttribute("aria-expanded", "false");
        if(viewMode === "3d"){ try{ resize3D(); startLoop3D(); }catch(err){} }
      }
    };
    if(PANEL_MQ.addEventListener) PANEL_MQ.addEventListener("change", onMq);
    else if(PANEL_MQ.addListener) PANEL_MQ.addListener(onMq);   // older engines
  })();

  radarUpdate = (rs)=>{
    // Geometry from the ALL-floor index, so an off-floor room (not in roomIdx)
    // still lands in liveTargets and reaches the 3D ghost-marker. A room absent
    // from the whole house doc has no geometry -> nothing to place.
    const rect = roomRectIdx[rs.room];
    if(!rect) return;
    // Feed absolute target positions for EVERY floor's rooms; the 3D people/floor-stack
    // renderer decides how to draw an off-floor one (ghost marker).
    liveTargets[rs.room] = (rs.targets||[]).map(t => {
      // est -> hazy 3D marker. An unpositioned target is treated the same way,
      // so it never reads as a fix; it is also excluded from zone hit-testing.
      const p = absTargetPoint(rect, t);
      p.est = isEstimated(t) || !p.positioned;
      return p;
    });
    // The 2D SVG only ever draws the ACTIVE floor, so its dot bookkeeping stays gated
    // on roomIdx (which only holds active-floor rooms + their DOM nodes).
    const e = roomIdx[rs.room];
    if(e){
      paintRoom2D(rs.room);   // liveness tri-state (occupied/offline/empty/blind), not just .occ
      // Reuse dots keyed by target id so movement TRANSITIONS instead of teleporting
      // (a person walking reads as motion, not a slideshow of positions).
      const seen = new Set();
      (rs.targets||[]).forEach(t=>{
        const key = String(t.id ?? "?");
        seen.add(key);
        const g = e.dots.get(key);
        if(g){ placeDot(g, e.r, t); }
        else { const n = dot(e.r, t); e.dots.set(key, n); e.layer.appendChild(n); }
      });
      [...e.dots.keys()].forEach(k=>{
        if(!seen.has(k)){ e.dots.get(k).remove(); e.dots.delete(k); }
      });
    }
    updateZoneOcc();   // per-frame: only re-highlight (no DOM rebuild); structure rebuilt on zone change
    updatePeople3D();  // keep the 3D people markers in sync with the same live target data
    tintRoom3D(rs.room);   // §6: floor tint tracks this frame's confidence (roomOcc/roomConf
                           // were already updated by updateHouse() — handle() runs it first)
  };

  // ---- Stage-1 shell hook (render-on-demand, MASTER-SPEC §4): the tab router pauses the
  // 3D loop when the map leaves the active screen and resumes it on return. Additive only —
  // startLoop3D()/stopLoop3D() and viewMode are untouched; resume() no-ops until the 3D
  // scene exists (render3D() owns the very first start, exactly as before).
  window.__wavrMap = {
    pause: stopLoop3D,
    resume: ()=>{ if(viewMode === "3d" && three3d) startLoop3D(); },
    getViewMode: ()=> viewMode,
  };
  try{ window.__wavrShellSync?.(); }catch{}
}
if(!window.WAVR_MOBILE) renderRadar();   // mobile: deferred to the post-ready boot below (token cache empty at parse time)
