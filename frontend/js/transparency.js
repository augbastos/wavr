// ==========================================================================
// transparency.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- What Wavr knows (feature #6): the trust screen, GET /api/transparency ----
// Same MODE gate/auth shape as renderHouseStatus() just above (live + an authenticated
// companion, Bearer/presence:read -- root/central/user all carry that scope by default; a
// guest device does NOT, so a guest's phone could never see this screen even if it somehow
// reached the tab). Never fetched in simulated/demo -- same "this demo makes no backend
// calls" invariant as the System egress dashboard's own #egressDemoNote. Every value below
// is server data, rendered via createElement/textContent -- NEVER innerHTML.
function renderTransparency(){
  const tile = document.getElementById("transTile");
  if(!tile) return;   // additive: a stale cached page without this markup just no-ops
  const loading   = document.getElementById("transLoading");
  const body      = document.getElementById("transBody");
  const sensingEl = document.getElementById("transSensing");
  const sensingSub= document.getElementById("transSensingSub");
  const camH      = document.getElementById("transCamH");
  const camNone   = document.getElementById("transCamNone");
  const camList   = document.getElementById("transCameras");
  const countsEl  = document.getElementById("transCounts");
  const egTile    = document.getElementById("transEgressTile");
  const egSum     = document.getElementById("transEgressSummary");
  const egList    = document.getElementById("transEgressList");
  const note      = document.getElementById("transNote");

  function showNote(msg){
    loading.hidden = true; body.hidden = true; egTile.hidden = true;
    note.hidden = false; note.textContent = msg;
  }

  if(MODE!=="live" && MODE!=="companion"){
    showNote(WavrT("This demo has no hub connected — “What Wavr knows” needs a real Wavr hub."));
    return;                                    // never a backend call in simulated/demo
  }
  if(MODE==="companion" && !companionToken()) return;  // companion pairing state: no token yet
  const auth = MODE==="companion" ? {"Authorization":"Bearer "+companionToken()} : {"X-Wavr-Local":"1"};

  // v2 egress icons' own visual language (System → "What can leave this home"): a badge per
  // row is the primary signal — green/lock "stays home", amber/net "can leave". Built locally
  // (not a call into the Stage-3b IIFE's own egRow/egBadge, which are private to that closure)
  // but reuses the SAME .eg-row/.eg-icon/.eg-main/.eg-name/.eg-tag/.eg-state CSS classes, so
  // this screen and System's dashboard read as one voice about egress.
  function egRowEl(item){
    const on = !!(item && item.on);
    const row = document.createElement("div"); row.className = "eg-row";
    const badge = document.createElement("span");
    badge.className = "eg-icon " + (on ? "egress" : "local");
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", on ? "#ic-net" : "#ic-lock");
    svg.appendChild(use); badge.appendChild(svg);
    row.appendChild(badge);
    const main = document.createElement("div"); main.className = "eg-main";
    const nameLine = document.createElement("div"); nameLine.className = "eg-name";
    nameLine.appendChild(document.createTextNode(String((item && item.channel) || "")));  // textContent-equivalent — server data
    const tag = document.createElement("span");
    tag.className = "eg-tag" + (on ? " on" : "");
    tag.textContent = on ? WavrT("on") : WavrT("off");
    nameLine.appendChild(tag);
    main.appendChild(nameLine);
    const state = document.createElement("span"); state.className = "eg-state";
    state.textContent = String((item && item.detail) || "");    // textContent — server data
    main.appendChild(state);
    row.appendChild(main);
    return row;
  }

  /* Two consecutive misses, then say so. Never keep a verdict.
   *
   * This used to `return` on a failed fetch under the comment "keep last view",
   * and the last view on THIS screen is "Sensing is on · Wavr is actively
   * watching for presence." A reassuring claim about surveillance state, made
   * over a Core that is not answering, is the worst instance this product has:
   * every other screen here is about what Wavr knows, and this is the one
   * somebody opens to check whether it is watching at all.
   *
   * Worse, a failed fetch was the easy half. A Core that goes DARK — power cut,
   * suspend, Wi-Fi dropped — does not refuse the connection, it says nothing,
   * so `await fetch(...)` never settled and `refresh` never reached the catch
   * either. Hence the deadline below as well: for a claim about live sensing, a
   * late answer is not an answer.
   *
   * Two misses rather than one, and `showNote` rather than a new surface: at a
   * 20-second poll one dropped request is not evidence of anything, and the
   * same two-miss rule already governs the landing tile (`house-status.js`), so
   * the two screens cannot contradict each other about the same silence.
   */
  let misses = 0;
  function cannotTell(){
    misses += 1;
    if(misses < 2) return;                     // one dropped request proves nothing
    showNote(WavrT("Wavr is not answering, so this cannot be checked. Check that the Core is running."));
  }

  async function refresh(){
    let r;
    try{ r = await WavrAPI.fetch("/api/transparency", {headers:auth, timeoutMs: 8000}); }
    catch{ cannotTell(); return; }             // no answer, including a host that never replies
    if(MODE==="companion" && (r.status===401 || r.status===403)){ companionAuthFailed(); return; }
    if(!r.ok){ showNote(WavrT("Couldn't load this screen right now — try again in a moment.")); return; }
    let t; try{ t = await r.json(); }catch{ showNote(WavrT("Couldn't load this screen right now — try again in a moment.")); return; }
    misses = 0;                                // a real answer clears the run of silence

    loading.hidden = true; note.hidden = true; body.hidden = false;

    const sensingOn = t.sensing_on !== false;
    sensingEl.className = "qc-headline " + (sensingOn ? "home" : "off");
    sensingEl.textContent = sensingOn ? WavrT("Sensing is on") : WavrT("Sensing is off");
    sensingSub.textContent = sensingOn
      ? WavrT("Wavr is actively watching for presence.")
      : WavrT("Wavr is paused — nothing is sensing right now.");

    const cameras = Array.isArray(t.cameras) ? t.cameras : [];
    camList.textContent = "";
    camH.hidden = cameras.length === 0;
    camNone.hidden = cameras.length > 0;
    cameras.forEach(function(c){
      const row = document.createElement("div"); row.className = "qc-room-row";
      const n = document.createElement("span"); n.className = "qc-room-name";
      n.textContent = String((c && c.name) || "");   // textContent — server data
      const on = !!(c && c.on);
      const s = document.createElement("span");
      s.className = "qc-room-state" + (on ? " occ" : "");
      s.textContent = on ? WavrT("on") : WavrT("off");
      row.appendChild(n); row.appendChild(s);
      camList.appendChild(row);
    });

    const counts  = (t.counts && typeof t.counts === "object") ? t.counts : {};
    const people  = Number(counts.people_known) || 0;
    const devices = Number(counts.devices_seen) || 0;
    const rooms   = Number(counts.rooms) || 0;
    // Whole clauses with a slot, never a translated word glued onto a number:
    // the count does not sit in the same place in every language.
    countsEl.textContent =
      WavrT("{n} person known|{n} people known", { n: people }) +
      " · " + WavrT("{n} device seen|{n} devices seen", { n: devices }) +
      " · " + WavrT("{n} room mapped|{n} rooms mapped", { n: rooms });

    const egress = Array.isArray(t.egress) ? t.egress : [];
    egTile.hidden = false;
    egList.textContent = "";
    egress.forEach(function(e){ egList.appendChild(egRowEl(e)); });
    // Honesty gate: name what's actually on rather than a bare count — disclosure, not alarm.
    const onRows = egress.filter(function(e){ return e && e.on; });
    if(onRows.length === 0){
      egSum.className = "egress-summary ok";
      egSum.textContent = WavrT("Nothing leaves your network — everything runs on this device.");
    } else {
      egSum.className = "egress-summary warn";
      // The channel NAMES are the backend's; the sentence around them is ours.
      egSum.textContent = WavrT("{what} is on — see below.|{what} are on — see below.", {
        n: onRows.length,
        what: onRows.map(function(e){ return e.channel; }).join(", ")
      });
    }
    // The topbar privacy chip (shell-nav.js) reads the SAME rows, so the two
    // surfaces that both promise to answer "does anything leave right now"
    // can never disagree — one producer, one fetch, already running.
    if(typeof window.__wavrOnEgressUpdate === "function") window.__wavrOnEgressUpdate(onRows);
  }

  refresh();
  setInterval(refresh, 20000);
}
renderTransparency();

// One-shot read of GET /api/status.features.multidevice, shared by the three
// multidevice-gated panels below (Pairing/Peers/Nodes). /api/devices, /api/peers and
// /api/nodes are only mounted when WAVR_MULTIDEVICE=1 (backend/wavr/app.py) -- on a
// single-device (HTTP) install they are GUARANTEED to 404, and the browser logs that
// network failure to the console itself even though the code below already catches
// it (fetch() rejecting/resolving-non-ok doesn't suppress DevTools' own "failed to
// load resource" line). Knowing multidevice=false ahead of time lets each panel skip
// the doomed probe entirely instead of just handling its failure. Computed ONCE (not
// per-panel) and resolves to false -- never short-circuiting -- if /api/status itself
// is unreachable or doesn't say, so the existing probe-then-404-hides-panel behavior
// below is untouched for every case this can't prove ahead of time (multidevice=true
// but peers/nodes individually off, status endpoint down, etc).
const _wavrMultideviceOff = (MODE==="live")
  ? fetch(location.origin+"/api/status").then(r=>r.ok?r.json():null)
      .then(j=>(j&&j.features&&j.features.multidevice)===false).catch(()=>false)
  : Promise.resolve(false);

