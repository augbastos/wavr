// ==========================================================================
// peers.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Peers — cross-instance mutual pairing (live/central only, peers-gated) ----
// Discovers other Wavr hubs (mDNS) and pairs with them mutually, confirmed by an
// out-of-band certificate-fingerprint check (the MitM defense, mirroring the device
// pairing fingerprint box). The /api/peers* routes only exist when the peers feature is
// on; when GET /api/peers 404s (or the backend is unreachable) the whole panel stays
// hidden — same probe-gate as the pairing and identity panels. Never in Plano B (demo)
// or companion (MODE!=="live" returns immediately). Every peer name/host/base_url/
// fingerprint is server/network data rendered via textContent — NEVER innerHTML.
async function renderPeers(){
  if(MODE!=="live") return;                 // live/central only; never in Plano B (demo) or companion
  if(await _wavrMultideviceOff) return;     // proven off via /api/status — skip the doomed probe (no console 404)
  // Probe the paired list first. A 404 means the peers feature is off; a thrown fetch
  // means the backend is momentarily unreachable. Either way: leave the panel hidden.
  let probe;
  try{ probe = await WavrAPI.fetch("/api/peers"); }
  catch{ return; }                          // backend unreachable — stay hidden
  if(!probe.ok) return;                     // 404 → peers feature off → stay hidden
  let seedPaired; try{ seedPaired = await probe.json(); }catch{ seedPaired = []; }
  document.getElementById("peers").hidden = false;

  const discList   = document.getElementById("peerDiscList");
  const pairedList = document.getElementById("peerPairedList");
  const peerFb     = document.getElementById("peerFb");
  const fpBox      = document.getElementById("peerFpBox");
  const fpName     = document.getElementById("peerFpName");
  const fpValue    = document.getElementById("peerFpValue");
  const codeInput  = document.getElementById("peerCodeInput");
  const fpConfirm  = document.getElementById("peerFpConfirm");
  const fpCancel   = document.getElementById("peerFpCancel");
  const showOwnBtn = document.getElementById("peerShowOwnCode");
  const ownCodeBox = document.getElementById("peerOwnCodeBox");
  const ownCodeEl  = document.getElementById("peerOwnCode");
  const ownFpEl    = document.getElementById("peerOwnFp");
  const peersTile  = document.getElementById("peers");

  // Mint THIS hub's own pairing code from its OWN loopback /api/pair-code — the SAME endpoint
  // + headers the device pair-code flow uses (role "central": a peer link is admin-to-admin).
  // Used by the "Show this hub's pairing code" button so this hub can act as TARGET B: the
  // OTHER hub's operator reads the code + fingerprint off THIS screen and types the code into
  // their pairing box. The code is minted on a trusted loopback screen, NEVER vended over the
  // network (that was /api/peers/exchange = C1, now deleted). mintOwnPeerCode() caches the full
  // response so ownCertFingerprint() returns the fingerprint from the SAME mint.
  let _lastMint = null;
  async function mintOwnPeerCode(){
    const r = await WavrAPI.fetch("/api/pair-code", {method: "POST", json: {role:"central"}});
    if(!r.ok) throw new Error("mint failed");
    _lastMint = await r.json();
    return String(_lastMint.code || "");    // 8-digit code from server
  }
  function ownCertFingerprint(){
    return (_lastMint && typeof _lastMint.cert_fingerprint==="string") ? _lastMint.cert_fingerprint : "";
  }
  // "Show this hub's pairing code" — mint + reveal our code and cert fingerprint for the other
  // hub's operator to read off-screen. Toggles the box; each show re-mints a fresh code.
  if(showOwnBtn){
    showOwnBtn.onclick = async ()=>{
      if(!ownCodeBox.hidden){ ownCodeBox.hidden = true; return; }   // toggle off
      showOwnBtn.disabled = true;
      try{
        const code = await mintOwnPeerCode();
        ownCodeEl.textContent = code || "—";                       // textContent — server data
        ownFpEl.textContent = ownCertFingerprint() || "—";         // textContent — our own cert
        ownCodeBox.hidden = false;
      }catch{ actionFeedback(peerFb, false, WavrT("couldn't mint this hub's code")); }
      showOwnBtn.disabled = false;
    };
  }

  // --- Discovered peers (mDNS) — untrusted until the admin pairs with a fingerprint check.
  // GET /api/peers/discovered returns a plain array of {name, host, port, role}. ---
  function renderDiscovered(list){
    list = Array.isArray(list) ? list : [];
    discList.textContent = "";
    if(!list.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("No peers discovered yet");
      discList.appendChild(e); return;
    }
    list.forEach(p => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = p.name || "(unnamed hub)"; // textContent — mDNS data
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      const host = (p.host!=null ? String(p.host) : "");
      const port = (p.port!=null ? String(p.port) : "");
      meta.textContent = " · " + (host && port ? host+":"+port : (host || WavrT("no address"))); // textContent — network data
      left.appendChild(name); left.appendChild(meta);
      if(p.role){                                          // role badge — server/network data
        const badge = document.createElement("span"); badge.className = "peer-badge";
        badge.textContent = String(p.role);                // textContent — never innerHTML
        left.appendChild(badge);
      }
      row.appendChild(left);
      const ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
      const pairBtn = document.createElement("button"); pairBtn.type = "button"; pairBtn.className = "ctl small";
      pairBtn.textContent = WavrT("Pair");
      pairBtn.onclick = ()=>{ startPeerPairing(p, pairBtn); };
      ctl.appendChild(pairBtn); row.appendChild(ctl);
      discList.appendChild(row);
    });
  }

  async function refreshDiscoveredPeers(){
    let r; try{ r = await WavrAPI.fetch("/api/peers/discovered"); }catch{ return; }
    if(!r.ok) return;
    let j; try{ j = await r.json(); }catch{ return; }
    renderDiscovered(j);
  }

  // --- Paired peers — GET /api/peers returns an array of PeerStore.to_dict()
  // ({peer_id, name, base_url, created_ts, revoked, ...}). Revoked rows are past
  // unpairings, so the "Paired" list shows only active links. Each active row carries a
  // two-step reveal-then-confirm Unpair (never a native confirm(), matching this file). ---
  function renderPaired(list){
    const active = (Array.isArray(list) ? list : []).filter(p => !p.revoked);
    pairedList.textContent = "";
    if(!active.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("No peers paired yet");
      pairedList.appendChild(e); return;
    }
    active.forEach(p => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = p.name || "(unnamed hub)"; // textContent — server data
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      const paired = (typeof p.created_ts==="string" && p.created_ts.length>=10) ? p.created_ts.slice(0,10) : "";
      meta.textContent = " · " + (p.base_url!=null ? String(p.base_url) : "") + (paired ? " · " + WavrT("paired {when}", {when: paired}) : ""); // textContent — server data
      left.appendChild(name); left.appendChild(meta);
      row.appendChild(left);
      const ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
      const peerId = (p.peer_id!=null ? p.peer_id : p.id);
      // Two-step reveal-then-confirm: the first click swaps the button for a Confirm/Cancel
      // pair; only the Confirm click DELETEs. A single mis-tap can't unpair a peer.
      const unpairBtn = document.createElement("button"); unpairBtn.type = "button"; unpairBtn.className = "rm";
      unpairBtn.textContent = WavrT("Unpair");
      unpairBtn.setAttribute("aria-label", WavrT("unpair {name}", {name: p.name || WavrT("peer")}));
      unpairBtn.onclick = ()=>{
        ctl.textContent = "";                              // reveal the confirm affordance in place
        const warn = document.createElement("span"); warn.className = "pair-dev-meta"; warn.textContent = WavrT("Unpair? ");
        const yes = document.createElement("button"); yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm");
        const no  = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
        no.onclick = ()=>{ renderPaired(active); };        // re-render restores the plain row
        yes.onclick = async ()=>{
          yes.disabled = true; no.disabled = true;
          let r;
          try{ r = await WavrAPI.fetch("/api/peers/"+encodeURIComponent(peerId), {method: "DELETE"}); }catch{}
          if(r && r.ok){ actionFeedback(peerFb, true, null, "✓ unpaired"); refreshPairedPeers(); }
          else { actionFeedback(peerFb, false, WavrT("couldn't unpair")); renderPaired(active); }
        };
        ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
      };
      ctl.appendChild(unpairBtn); row.appendChild(ctl);
      pairedList.appendChild(row);
    });
  }

  async function refreshPairedPeers(){
    let r; try{ r = await WavrAPI.fetch("/api/peers"); }catch{ return; }
    if(!r.ok) return;
    let j; try{ j = await r.json(); }catch{ return; }
    renderPaired(j);
  }

  // --- Pairing handshake (C1-fix ceremony). Leg 1: ask OUR OWN backend to OBSERVE the
  // peer's live cert fingerprint — POST /api/peers/observe {peer_base_url} over loopback
  // (X-Wavr-Local). The backend opens the pinned TLS socket and returns the fingerprint the
  // peer's cert ACTUALLY presents (never a self-reported JSON value — closes M1). We do NOT
  // mint or send any code here; the operator will TYPE the peer's on-screen code at confirm.
  // We STOP and surface the observed fingerprint for out-of-band confirmation — never
  // auto-continuing (the MitM defense). ---
  async function startPeerPairing(discovered, pairBtn){
    const base_url = discovered.base_url ||
      ("https://" + (discovered.host || "") + ":" + (discovered.port || ""));
    if(pairBtn) pairBtn.disabled = true;
    let r;
    try{
      r = await fetch(location.origin+"/api/peers/observe",{method:"POST",
        headers:{"Content-Type":"application/json","X-Wavr-Local":"1"},   // our own backend — loopback CSRF guard
        body:JSON.stringify({peer_base_url: base_url})});
    }catch{ actionFeedback(peerFb, false, WavrT("could not reach ") + (discovered.name || WavrT("peer"))); if(pairBtn) pairBtn.disabled = false; return; }
    if(!r.ok){ actionFeedback(peerFb, false, WavrT("could not reach ") + (discovered.name || WavrT("peer"))); if(pairBtn) pairBtn.disabled = false; return; }
    let obs; try{ obs = await r.json(); }catch{ obs = {}; }
    const fingerprint = (obs && typeof obs.fingerprint==="string") ? obs.fingerprint : "";
    if(!fingerprint){ actionFeedback(peerFb, false, WavrT("could not read the peer's certificate")); if(pairBtn) pairBtn.disabled = false; return; }
    showFingerprintConfirmDialog(discovered, base_url, fingerprint, pairBtn);
  }

  // --- Out-of-band confirm — the human-in-the-loop MitM defense (C1-fix ceremony). Shows the
  // SERVER-OBSERVED fingerprint (from /observe, never a self-reported body — M1). The admin
  // (a) compares it to the peer's own on-screen fingerprint AND (b) types the 8-digit code the
  // peer displays. Confirm stays disabled until 8 digits are entered. On confirm, leg 2: POST
  // our OWN /api/peers/confirm {peer_base_url, peer_name, peer_code, peer_fingerprint} over
  // loopback; the backend redeems the typed code on the peer over the PINNED channel (I1: the
  // observed fingerprint is pinned before any credential is sent) then auto-bootstraps the
  // reverse credential. May return 200 with reverse_leg_ok:false (peer stored, reverse to retry). ---
  function showFingerprintConfirmDialog(discovered, base_url, fingerprint, pairBtn){
    const peer_name = discovered.name || "peer";
    fpName.textContent = peer_name;                        // textContent — network data
    fpValue.textContent = fingerprint;                     // textContent — the OBSERVED fingerprint
    codeInput.value = "";
    fpBox.hidden = false;
    fpConfirm.disabled = true;                             // enabled only once 8 digits typed
    fpCancel.disabled = false;
    const onCode = ()=>{
      codeInput.value = codeInput.value.replace(/\D/g,"").slice(0,8);   // digits only, cap 8
      fpConfirm.disabled = codeInput.value.length !== 8;
    };
    codeInput.oninput = onCode;
    try{ codeInput.focus(); }catch{}
    fpCancel.onclick = ()=>{ fpBox.hidden = true; codeInput.value = ""; if(pairBtn) pairBtn.disabled = false; };
    fpConfirm.onclick = async ()=>{
      const peer_code = codeInput.value;
      if(peer_code.length !== 8) return;                   // guard: never send a partial code
      fpConfirm.disabled = true; fpCancel.disabled = true;
      let r;
      try{
        r = await fetch(location.origin+"/api/peers/confirm",{method:"POST",
          headers:{"Content-Type":"application/json","X-Wavr-Local":"1"},                 // our own backend — loopback CSRF guard
          body:JSON.stringify({
            peer_base_url: base_url,
            peer_name: peer_name,
            peer_code: peer_code,           // the code the operator READ off the peer's screen
            peer_fingerprint: fingerprint,  // the OBSERVED fingerprint — pinned by the backend (I1)
          })});
      }catch{}
      fpBox.hidden = true;
      codeInput.value = "";
      if(pairBtn) pairBtn.disabled = false;
      if(r && r.ok){
        let j; try{ j = await r.json(); }catch{ j = {}; }
        if(j && j.reverse_leg_ok === false) actionFeedback(peerFb, true, null, "✓ paired (reverse leg pending)");
        else actionFeedback(peerFb, true, null, "✓ paired");
        refreshPairedPeers();
      } else {
        // The Core says why, and this threw it away for a fixed sentence.
        // "Check the code" is a dead end whenever the code is not the problem:
        // a Core serving plain HTTP refuses to join a peer mesh at all (the two
        // sides exchange admin credentials), and no amount of retyping changes
        // that. `failureText` is the shared reader the Settings screen already
        // uses, and it falls back to a sentence carrying the status when the
        // Core genuinely did not say why.
        actionFeedback(peerFb, false, await failureText(r, WavrT("pair with that Core")));
      }
    };
  }

  // --- Poll discovered peers only while the panel is on screen. The tile lives inside the
  // Settings overlay (hidden => display:none => offsetParent null), so an IntersectionObserver
  // starts the poll when it scrolls into view and clears it the instant it isn't shown —
  // the same visibility discipline the pair-code rotation uses. ---
  let discTimer = null;
  function startDiscPoll(){ if(discTimer) return; refreshDiscoveredPeers(); discTimer = setInterval(refreshDiscoveredPeers, 5000); }
  function stopDiscPoll(){ if(discTimer){ clearInterval(discTimer); discTimer = null; } }
  if(peersTile && "IntersectionObserver" in window){
    const io = new IntersectionObserver((entries)=>{
      for(const en of entries){ if(en.isIntersecting) startDiscPoll(); else stopDiscPoll(); }
    }, {threshold:0.01});
    io.observe(peersTile);
  } else {
    startDiscPoll();                          // no IntersectionObserver — poll unconditionally (still MODE-gated)
  }

  renderPaired(seedPaired);                    // seed the paired list from the probe we already read
  refreshDiscoveredPeers();                    // one immediate discovered fetch
}
renderPeers();

