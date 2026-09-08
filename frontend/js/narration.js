// ==========================================================================
// narration.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Narration (Plano A / live only) ----
// Stage 3b (§9): the narrator is the app's ONLY egress, so its trigger is structurally
// distinct from every other control — a two-step confirm before POST /api/narrate, and a
// transient non-green "sending to the cloud…" indicator (echoed briefly in a top-center toast,
// Fix F3) while the request is actually in flight. The fixed egress-note stays visible throughout.
function renderNarrate(){
  const card = document.getElementById("narrate");
  if(!card) return;
  if(MODE!=="live"){                        // narration hits the backend/cloud; never in Plano B
    // P5 fix 8: don't vanish silently — show the card with an honest placeholder, matching
    // the Sistema egress panel's demo-mode explanation pattern.
    card.hidden = false;
    const b = document.getElementById("narrateBtn");
    const o = document.getElementById("narrateOut");
    if(b) b.hidden = true;
    if(o){
      o.className = "narrate-out muted";
      o.textContent = MODE==="companion"
        ? WavrT("The narrator runs on the Wavr hub — generate summaries from the hub's panel (local access).")
        : WavrT("The narrator runs on the Wavr hub — unavailable in this demo.");
    }
    return;
  }
  card.hidden = false;
  const btn = document.getElementById("narrateBtn");
  const out = document.getElementById("narrateOut");
  const box = document.getElementById("narrateConfirm");
  const yes = document.getElementById("narrateYes");
  const no  = document.getElementById("narrateNo");
  const sending = document.getElementById("narrateSending");
  const pill = document.getElementById("pillEgress");
  function setSending(on){
    if(sending) sending.hidden = !on;
    if(pill){
      clearTimeout(pill._t);
      if(on) pill.hidden = false;
      else pill._t = setTimeout(()=>{ pill.hidden = true; }, 1200);   // brief top-center toast after the send
    }
  }
  // Step 1: "Generate summary" only ARMS the explicit confirm — it never sends by itself.
  btn.onclick = ()=>{
    if(!box){ return; }
    box.hidden = false; btn.disabled = true;
    yes?.focus();
  };
  if(no) no.onclick = ()=>{ box.hidden = true; btn.disabled = false; btn.focus(); };
  // Step 2 (confirmed): the ONLY line in the whole app that sends data to the cloud.
  if(yes) yes.onclick = async ()=>{
    box.hidden = true;
    btn.disabled = true; out.className = "narrate-out muted"; out.textContent = WavrT("generating summary…");
    setSending(true);
    try{
      const r = await WavrAPI.fetch("/api/narrate", {method: "POST"});
      if(r.status===503){
        // 503 now covers two honest cases: not-configured OR revoked in the Connectors screen.
        // Surface the server's own detail (textContent) so a revoked narrator reads accurately
        // instead of the misleading "not configured" hint. Fall back if the body is absent.
        let detail=""; try{ detail=(await r.json()).detail||""; }catch{}
        out.textContent = detail || WavrT("narration not configured (set WAVR_NARRATE_ENABLED and configure a provider)");
      }
      else if(!r.ok){ out.textContent = WavrT("couldn't generate the narration"); }
      else { const d = await r.json(); out.className = "narrate-out"; out.textContent = d.narration || WavrT("(no response)"); }
    }catch{ out.textContent = WavrT("connection failed"); }
    finally{ setSending(false); btn.disabled = false; }
  };
}
renderNarrate();

