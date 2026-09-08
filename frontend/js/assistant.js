// ==========================================================================
// assistant.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Wavr Assistant (Phase 2B): engine picker + bounded ask + audit trail (gear) ----
// Live/central only, same "hidden until the backend answers 200" discipline as
// renderConnectors() above: MODE!=="live" or a non-ok GET (403 non-admin peer, or the
// router simply not reachable) means this whole card stays hidden, byte-identical to
// today for demo/companion and for any build where the feature is off.
async function renderAssistant(){
  if(MODE!=="live") return;
  const card = document.getElementById("assistant");
  if(!card) return;
  let probe;
  try{ probe = await WavrAPI.fetch("/api/assistant/engines"); }
  catch{ return; }                          // backend unreachable — stay hidden
  if(!probe.ok) return;                     // 403 (non-admin peer) / feature off — stay hidden
  let data; try{ data = await probe.json(); }catch{ data = {}; }
  card.hidden = false;

  const list = document.getElementById("asstList");
  const fb = document.getElementById("asstFb");
  const manualBox = document.getElementById("asstManualForm");
  const manualTitle = document.getElementById("asstManualFormTitle");
  const manualUrl = document.getElementById("asstManualBaseUrl");
  const manualModel = document.getElementById("asstManualModel");
  const manualKey = document.getElementById("asstManualKeyEnv");
  const manualSave = document.getElementById("asstManualSave");
  const manualCancel = document.getElementById("asstManualCancel");
  const manualFb = document.getElementById("asstManualFb");
  const qInput = document.getElementById("asstQuestion");
  const askBtn = document.getElementById("asstAskBtn");
  const askConfirmBox = document.getElementById("asstAskConfirm");
  const askConfirmTxt = document.getElementById("asstAskConfirmTxt");
  const askYes = document.getElementById("asstAskYes");
  const askNo = document.getElementById("asstAskNo");
  const asking = document.getElementById("asstAsking");
  const answerP = document.getElementById("asstAnswer");
  const traceBox = document.getElementById("asstTrace");
  const logList = document.getElementById("asstLogList");

  const SVG_NS2 = "http://www.w3.org/2000/svg";
  const KEY_ENV_RE = /^[A-Z][A-Z0-9_]*$/;
  let engines = Array.isArray(data.engines) ? data.engines : [];

  function labelFor(id){                    // derived from the live catalog — one source of truth
    const e = engines.find(x => x.id === id);
    return (e && e.label) || id;
  }

  function jumpToConnectors(){
    const btn = document.querySelector('.gear-rail-item[data-section="gearSecConnectors"]');
    if(btn) btn.click();                    // switches the visible section at panel width
    const el = document.getElementById("connectors");
    if(el) el.scrollIntoView({block:"start"});   // scrolls to it in desktop's single column
  }

  async function refreshEngines(){
    let r; try{ r = await WavrAPI.fetch("/api/assistant/engines"); }catch{ return; }
    if(!r.ok) return;
    let d; try{ d = await r.json(); }catch{ return; }
    engines = Array.isArray(d.engines) ? d.engines : [];
    renderList();
    // UX HIGH #2: the engine (and therefore the confirm's vendor sentence) just changed
    // underneath any still-open confirm box — close it rather than let a stale "sends to
    // <old vendor>" prompt linger past a re-selection.
    const askConfirmBox2 = document.getElementById("asstAskConfirm");
    if(askConfirmBox2 && !askConfirmBox2.hidden){
      askConfirmBox2.hidden = true;
      const askBtn2 = document.getElementById("asstAskBtn");
      if(askBtn2) askBtn2.disabled = false;
    }
  }

  async function selectEngine(engineId, extra){
    const body = Object.assign({engine_id: engineId}, extra || {});
    let r;
    try{
      r = await WavrAPI.fetch("/api/assistant/engine", {method: "POST", json: body});
    }catch{ return {ok:false, detail: WavrT("connection failed")}; }
    if(r.ok){ await refreshEngines(); return {ok:true}; }
    let detail=""; try{ detail = (await r.json()).detail || ""; }catch{}
    return {ok:false, detail: detail || WavrT("couldn't select this engine")};
  }

  function openManualForm(existing){
    manualTitle.textContent = existing && existing.available
      ? WavrT("Edit your OpenAI-compatible endpoint") : WavrT("Add your own OpenAI-compatible endpoint");
    manualUrl.value = (existing && existing.base_url) || "";
    manualModel.value = (existing && existing.model) || "";
    manualKey.value = (existing && existing.key_env_var) || "";
    manualFb.textContent = ""; manualFb.className = "action-fb";
    manualBox.hidden = false;
    manualUrl.focus();
    manualBox.scrollIntoView({block:"nearest"});
  }
  manualCancel.onclick = ()=>{ manualBox.hidden = true; };
  manualSave.onclick = async ()=>{
    const base_url = manualUrl.value.trim();
    const model = manualModel.value.trim();
    const key_env_var = manualKey.value.trim();
    if(!base_url || !model){ actionFeedback(manualFb, false, WavrT("base URL and model are both required")); return; }
    if(!/^https?:\/\/.+/i.test(base_url)){ actionFeedback(manualFb, false, WavrT("base URL must start with http:// or https://")); return; }
    if(key_env_var && !KEY_ENV_RE.test(key_env_var)){ actionFeedback(manualFb, false, WavrT("env var name must look like UPPERCASE_NAME")); return; }
    manualSave.disabled = true;
    const res = await selectEngine("manual", {base_url, model, key_env_var: key_env_var || null});
    manualSave.disabled = false;
    if(res.ok){ manualBox.hidden = true; actionFeedback(fb, true, null, WavrT("✓ manual engine saved and selected")); }
    else actionFeedback(manualFb, false, res.detail);
  };

  function buildCard(e){
    const cardEl = document.createElement("div");
    cardEl.className = "conn-card" + (e.selected ? " engine-selected" : "");

    const top = document.createElement("div"); top.className = "conn-top";
    const nm = document.createElement("h4"); nm.className = "conn-name";
    nm.textContent = e.label || e.id;         // textContent — server descriptor data
    top.appendChild(nm);
    cardEl.appendChild(top);

    const descP = document.createElement("p"); descP.className = "conn-desc";
    descP.textContent = e.description ? WavrT(e.description) : "";
    cardEl.appendChild(descP);

    // Same egress badge/wording as Connectors — one voice about what leaves home.
    const egress = !!e.egress;
    const badge = document.createElement("span");
    badge.className = "conn-egress-badge " + (egress ? "leaves" : "stays");
    const bsvg = document.createElementNS(SVG_NS2, "svg"); bsvg.setAttribute("aria-hidden", "true");
    const buse = document.createElementNS(SVG_NS2, "use"); buse.setAttribute("href", egress ? "#ic-net" : "#ic-lock");
    bsvg.appendChild(buse); badge.appendChild(bsvg);
    badge.appendChild(document.createTextNode(egress ? WavrT("Can reach outside your network") : WavrT("Stays on your local network")));
    cardEl.appendChild(badge);

    if(e.model || e.base_url || e.key_env_var){
      const sc = document.createElement("p"); sc.className = "conn-scope";
      const bits = [];
      if(e.model) bits.push(WavrT("Model: {model}", {model: e.model}));
      if(e.base_url) bits.push(WavrT("Endpoint: {url}", {url: e.base_url}));
      if(e.key_env_var) bits.push(WavrT("API key env var: {name}", {name: e.key_env_var}));
      sc.textContent = bits.join(" · ");      // textContent — server/config data, never interpolated HTML
      cardEl.appendChild(sc);
    }

    const foot = document.createElement("div"); foot.className = "conn-foot";
    const radio = document.createElement("button"); radio.type = "button";
    radio.className = "ctl small engine-radio" + (e.selected ? " on" : "");
    radio.setAttribute("role", "radio");
    radio.setAttribute("aria-checked", e.selected ? "true" : "false");
    // (MED a11y) roving tabindex: every radio starts OUT of the tab order; syncRoving()
    // (called right after renderList() rebuilds the list) puts exactly one back in. The
    // engine id survives the rebuild so focus can be restored onto the NEW node below.
    radio.tabIndex = -1;
    radio.dataset.engineId = e.id;
    const dot = document.createElement("span"); dot.className = "radio-dot"; dot.setAttribute("aria-hidden","true");
    radio.appendChild(dot);
    radio.appendChild(document.createTextNode(
      e.id === "manual" ? (e.available ? WavrT("Edit / re-select") : WavrT("Configure…"))
        : (e.selected ? WavrT("Selected") : WavrT("Select"))));
    radio.onclick = async ()=>{
      if(e.id === "manual"){ openManualForm(e); return; }
      radio.disabled = true;
      const res = await selectEngine(e.id);
      radio.disabled = false;
      if(res.ok) actionFeedback(fb, true, null, WavrT("✓ {name} selected", {name: e.label || e.id}));
      else actionFeedback(fb, false, res.detail);
    };
    foot.appendChild(radio);

    const st = document.createElement("span");
    st.className = "conn-state" + (e.needs ? " setup" : (e.selected ? " active" : ""));
    st.textContent = e.needs === "config" ? WavrT("needs setup")
      : e.needs === "connector" ? WavrT("needs Connectors") : (e.selected ? WavrT("in use") : WavrT("ready"));
    foot.appendChild(st);
    cardEl.appendChild(foot);

    if(e.needs){
      const note = document.createElement("p"); note.className = "conn-note";
      note.textContent = e.needs === "connector"
        ? WavrT("This engine sends data outside your network — enable it in Connectors first.")
        : (e.id === "manual" ? WavrT("Set a base URL and model to finish configuring it.")
           : WavrT("Add {name} to the hub's .env and restart to finish configuring it.", {name: e.key_env_var || WavrT("its API key")}));
      foot.appendChild(note);
      if(e.needs === "connector"){
        const jump = document.createElement("button"); jump.type = "button";
        jump.className = "ctl small"; jump.textContent = WavrT("Open Connectors");
        jump.onclick = jumpToConnectors;
        foot.appendChild(jump);
      }
    }
    return cardEl;
  }

  // (MED a11y) #asstList is role="radiogroup"/role="radio" but previously had no roving
  // tabindex or arrow-key handling — every card's radio sat in the tab order individually
  // and arrow keys did nothing, unlike the app's other correct ARIA widgets (.nav-tabs,
  // .viewToggle.dsub — same cycle/Home/End pattern mirrored below).
  function asstRadios(){
    return Array.prototype.slice.call(list.querySelectorAll('[role="radio"]'));
  }
  function syncRoving(){
    const radios = asstRadios();
    let i = radios.findIndex(r => r.getAttribute("aria-checked") === "true");
    if(i === -1) i = 0;
    radios.forEach((r, idx) => { r.tabIndex = idx === i ? 0 : -1; });
  }
  function renderList(){
    // engine selection round-trips through the backend then rebuilds this list from
    // scratch (server truth) — without this, a keyboard user's focus would silently
    // fall out of the list on every selection (mouse or arrow-key driven alike).
    const active = document.activeElement;
    const focusedId = (active && list.contains(active) && active.dataset) ? active.dataset.engineId : null;
    list.textContent = "";
    engines.forEach(e => list.appendChild(buildCard(e)));
    syncRoving();
    if(focusedId){
      const esc = (window.CSS && CSS.escape) ? CSS.escape(focusedId) : focusedId;
      const toFocus = list.querySelector('[data-engine-id="' + esc + '"]');
      if(toFocus) toFocus.focus();
    }
  }
  renderList();
  const ASST_KEYS = ["ArrowRight","ArrowDown","ArrowLeft","ArrowUp","Home","End"];
  list.addEventListener("keydown", (e)=>{
    if(ASST_KEYS.indexOf(e.key) === -1) return;
    const radios = asstRadios();
    if(!radios.length) return;
    let i = radios.indexOf(document.activeElement);
    if(i === -1) i = radios.findIndex(r => r.getAttribute("aria-checked") === "true");
    if(i === -1) i = 0;
    e.preventDefault();
    if(e.key === "ArrowRight" || e.key === "ArrowDown") i = (i + 1) % radios.length;
    else if(e.key === "ArrowLeft" || e.key === "ArrowUp") i = (i - 1 + radios.length) % radios.length;
    else if(e.key === "Home") i = 0;
    else i = radios.length - 1;
    const target = radios[i];
    if(!target) return;
    target.focus();
    target.click();   // arrow-key SELECTS (native radiogroup behavior), mirroring target.click's
                       // existing click handler — same select/openManualForm path either input takes
  });

  function renderTrace(trace){
    traceBox.textContent = "";
    if(!Array.isArray(trace) || !trace.length){ traceBox.hidden = true; return; }
    trace.forEach(t => {
      const row = document.createElement("div");
      row.className = "asst-trace-step" + (t.final ? " final" : (t.ok === false ? " refused" : ""));
      const n = document.createElement("span"); n.className = "step-n"; n.textContent = WavrT("Step {n}", {n: t.step});
      row.appendChild(n);
      const msg = document.createElement("span");
      if(t.final) msg.textContent = t.forced ? WavrT("gave a final answer (ran out of tool calls)") : WavrT("gave a final answer");
      else if(t.tool) msg.textContent = WavrT(t.ok === false ? "called {tool} — refused/failed" : "called {tool} — ok", {tool: t.tool});
      else msg.textContent = "—";
      row.appendChild(msg);
      traceBox.appendChild(row);
    });
    traceBox.hidden = false;
  }

  async function refreshLog(){
    let r; try{ r = await WavrAPI.fetch("/api/assistant/log?limit=50"); }catch{ return; }
    if(!r.ok) return;
    let d; try{ d = await r.json(); }catch{ return; }
    const rows = Array.isArray(d.log) ? d.log : [];
    logList.textContent = "";
    if(!rows.length){
      const p = document.createElement("p"); p.className = "pair-hint";
      p.textContent = WavrT("No questions asked yet.");
      logList.appendChild(p);
      return;
    }
    rows.forEach(row => {
      const el = document.createElement("div"); el.className = "asst-log-row";
      const top = document.createElement("div"); top.className = "asst-log-top";
      const eng = document.createElement("span"); eng.className = "asst-log-engine";
      eng.textContent = labelFor(row.engine_id);
      top.appendChild(eng);
      const ts = document.createElement("span"); ts.className = "asst-log-ts";
      ts.textContent = fmtRelative(row.ts) || "";
      if(row.ts) ts.title = row.ts;
      top.appendChild(ts);
      el.appendChild(top);

      const q = document.createElement("p"); q.className = "asst-log-q";
      const qLabel = document.createElement("b"); qLabel.textContent = WavrT("Asked:") + " ";
      q.appendChild(qLabel); q.appendChild(document.createTextNode(row.question || ""));
      el.appendChild(q);

      const tools = Array.isArray(row.tool_names_called) ? row.tool_names_called : [];
      const tw = document.createElement("div"); tw.className = "asst-log-tools";
      if(tools.length){
        tools.forEach(name => {
          const chip = document.createElement("span"); chip.className = "asst-tool-chip";
          chip.textContent = name;
          tw.appendChild(chip);
        });
      } else {
        const noTools = document.createElement("span"); noTools.className = "asst-tool-chip";
        noTools.textContent = WavrT("no tools used");
        tw.appendChild(noTools);
      }
      el.appendChild(tw);

      const a = document.createElement("p"); a.className = "asst-log-a";
      const aLabel = document.createElement("b"); aLabel.textContent = WavrT("Answered:") + " ";
      a.appendChild(aLabel); a.appendChild(document.createTextNode(row.answer || ""));
      el.appendChild(a);

      logList.appendChild(el);
    });
  }
  refreshLog();

  function setAsking(on){
    if(asking) asking.hidden = !on;
    askBtn.disabled = on;
  }
  // UX HIGH #2: which engine is CLOUD right now, read live off the same `engines` catalog
  // the picker cards render from (refreshed after every select) — never a fixed id list,
  // so e.g. wavr_assistant/local_llm pointed at a non-loopback endpoint still confirms.
  function selectedAsstEngine(){
    return engines.find(e => e.selected) || null;
  }
  function vendorFor(e){
    if(!e) return WavrT("the selected engine");
    return e.id === "manual" ? WavrT("your configured endpoint") : (e.label || e.id);
  }
  async function doAsk(q){
    setAsking(true);
    answerP.className = "narrate-out muted"; answerP.textContent = WavrT("asking…");
    traceBox.hidden = true; traceBox.textContent = "";
    try{
      const r = await WavrAPI.fetch("/api/assistant/ask", {method: "POST", json: {question:q}});
      if(r.status===422){
        let detail=""; try{ detail=(await r.json()).detail||""; }catch{}
        answerP.textContent = detail || WavrT("that question isn't valid");
      } else if(r.status===503){
        let detail=""; try{ detail=(await r.json()).detail||""; }catch{}
        answerP.textContent = detail || WavrT("the selected engine isn't ready yet");
      } else if(r.status===502){
        answerP.textContent = WavrT("the assistant engine could not answer right now — try again");
      } else if(!r.ok){
        answerP.textContent = WavrT("couldn't get an answer");
      } else {
        const d = await r.json();
        answerP.className = "narrate-out";
        answerP.textContent = d.answer || WavrT("(no response)");
        renderTrace(d.trace);
        refreshLog();                          // a completed ask just appended a new audit row
      }
    }catch{ answerP.textContent = WavrT("connection failed"); }
    finally{ setAsking(false); }
  }
  // Step 1: "Ask" on a CLOUD engine only ARMS the confirm — mirrors narrateBtn.onclick
  // (~narrateConfirm). A local engine (built-in / Local LLM / loopback manual) sends
  // straight away, same one-click flow as before this fix.
  askBtn.onclick = ()=>{
    const q = qInput.value.trim();
    if(!q){ answerP.className = "narrate-out muted"; answerP.textContent = WavrT("type a question first"); qInput.focus(); return; }
    const sel = selectedAsstEngine();
    if(sel && sel.egress && askConfirmBox && askConfirmTxt){
      askConfirmTxt.textContent = WavrT("This sends your question, plus a coarse summary of your Space " +
        "(current occupancy and the Space-status verdict), to {vendor}. Send?", {vendor: vendorFor(sel)});
      askConfirmBox.hidden = false;
      askBtn.disabled = true;
      askYes?.focus();
      return;
    }
    doAsk(q);
  };
  if(askNo) askNo.onclick = ()=>{
    if(askConfirmBox) askConfirmBox.hidden = true;
    askBtn.disabled = false; askBtn.focus();
  };
  // Step 2 (confirmed): the only line that actually sends the cloud request.
  if(askYes) askYes.onclick = ()=>{
    if(askConfirmBox) askConfirmBox.hidden = true;
    doAsk(qInput.value.trim());
  };
}
renderAssistant();

