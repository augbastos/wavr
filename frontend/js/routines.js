// ==========================================================================
// routines.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Routines (Rotinas tab): layperson "when THIS -> do THAT" builder ----
// Plain-language trigger/action menus, mirroring routines.py's VALID_TRIGGERS/VALID_ACTIONS
// and their required trigger_params/action-params keys exactly (a 400 there is surfaced
// honestly here, never silently retried or guessed around). The picker never emits JSON —
// every field is a <select> option, a text box, or a time input.
const ROT_TRIGGERS = [
  {kind:"house_arrived", label:"When someone arrives", paramKey:null},
  {kind:"house_left", label:"When everyone leaves", paramKey:null},
  {kind:"person_arrived", label:"When a specific person arrives", paramKey:"person"},
  {kind:"person_left", label:"When a specific person leaves", paramKey:"person"},
  {kind:"room_occupied", label:"When a room fills", paramKey:"room"},
  {kind:"room_empty", label:"When a room empties", paramKey:"room"},
  {kind:"schedule", label:"At a time of day", paramKey:"at"},
  {kind:"house_away_by_time", label:"If nobody's home by a time", paramKey:"by"},
  {kind:"device_seen", label:"When a device shows up", paramKey:"mac"},
  {kind:"no_motion", label:"If a room has no movement for a while", paramKey:"no_motion"},
];
const ROT_ACTION_KINDS = [
  {kind:"ha_service", label:"Turn a light/switch on or off"},
  {kind:"notify", label:"Send me a notification"},
  {kind:"set_watch", label:"Enter/leave discreet mode (Watch)"},
  // Body is COMPUTED at fire time (routines.py's notify_new_devices) — the count of devices
  // first-seen while the house was away; `message` is only an optional prefix, never required
  // (VALID_ACTIONS'/_ACTION_REQUIRED's own contract — see routines.py). Pairs naturally with
  // the "When someone arrives" trigger, no special-casing needed here.
  {kind:"notify_new_devices", label:"Tell me what new devices showed up while I was out"},
];
// One-tap "quick starts" — a preset only pre-fills openBuilder()'s state; it never posts on
// its own. The user still names the routine and hits Save/Create through the normal flow.
const ROT_QUICKSTARTS = [
  {
    label: "Everyone leaves → discreet mode",
    name: "Everyone leaves → discreet mode",
    trigger_kind: "house_left", trigger_params: {},
    action: {kind: "set_watch", params: {on: true}},
  },
  {
    label: "Nobody home by midnight → notify me",
    name: "Nobody home by midnight",
    trigger_kind: "house_away_by_time", trigger_params: {by: "00:00"},
    action: {kind: "notify", params: {message: "Nobody's home and it's past midnight."}},
  },
  {
    label: "Every night at 11pm → discreet mode",
    name: "Every night at 11pm → discreet mode",
    trigger_kind: "schedule", trigger_params: {at: "23:00"},
    action: {kind: "set_watch", params: {on: true}},
  },
  {
    label: "When I arrive → turn on a light",
    name: "When I arrive → turn on a light",
    trigger_kind: "house_arrived", trigger_params: {},
    // entity_id deliberately left blank — the builder's own "choose a light/switch…" picker
    // is where the user actually selects it, never guessed here.
    action: {kind: "ha_service", params: {service: "turn_on"}},
  },
];
function rotTriggerLabel(kind){ const t = ROT_TRIGGERS.find(x => x.kind === kind); return t ? WavrT(t.label) : kind; }
function rotActionLabel(kind){ const a = ROT_ACTION_KINDS.find(x => x.kind === kind); return a ? WavrT(a.label) : kind; }

async function renderRoutines(){
  // Same MODE tier as renderControls()/System: reachable live (loopback root) OR a companion
  // paired 'central' (its DEFAULT_SCOPES include "control", the router-level gate on every
  // /api/routines* route) — never a plain 'user' companion or the public demo.
  const companionCentral = MODE === "companion" && companionIsCentral();
  const tile = document.getElementById("routinesTile");
  const note = document.getElementById("rotNote");
  if(MODE !== "live" && !companionCentral){
    if(tile) tile.hidden = true;
    if(note) note.hidden = false;
    return;
  }
  if(note) note.hidden = true;
  if(tile) tile.hidden = false;

  // Mobile: route through the native pinned fetch (base = stored central) so the companion
  // Bearer token never hits the app's own https://localhost — same __nf pattern as
  // renderControls/renderStatus. Absent the hook, __nf is exactly the original same-origin fetch.
  const __nf = (path, opt)=> window.WAVR_MOBILE
    ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
    : fetch(location.origin + path, opt);
  const companionAuth = companionCentral ? {"Authorization":"Bearer "+companionToken()} : null;
  // Fix D pattern (matches renderControls/renderConnectors): a companion sends its Bearer
  // instead of the loopback-only X-Wavr-Local header on every write; GETs need only the Bearer.
  const writeHdr = ()=> companionCentral
    ? Object.assign({"Content-Type":"application/json"}, companionAuth)
    : {"Content-Type":"application/json","X-Wavr-Local":"1"};
  const deleteHdr = ()=> companionCentral ? companionAuth : {"X-Wavr-Local":"1"};
  const getOpt = companionAuth ? {headers: companionAuth} : undefined;

  const list = document.getElementById("rotList");
  const empty = document.getElementById("rotEmpty");
  const fb = document.getElementById("rotFb");
  let ROUTINES = [];
  let ENTITIES = null;    // lazy-cached GET /api/routines/ha-entities result ([] once fetched)
  let PERSONS = null;     // lazy-cached known-person names (live only, mirrors renderKnownPresence)

  async function fetchEntities(){
    if(ENTITIES) return ENTITIES;
    try{
      const r = await __nf("/api/routines/ha-entities", getOpt);
      const j = await r.json();
      ENTITIES = Array.isArray(j.entities) ? j.entities : [];
    }catch{ ENTITIES = []; }
    return ENTITIES;
  }
  async function fetchPersons(){
    if(PERSONS) return PERSONS;
    PERSONS = [];
    if(MODE !== "live") return PERSONS;   // known-presence is live-only (renderKnownPresence's own gate)
    try{
      const r = await WavrAPI.fetch("/api/identity/known-presence");
      if(r.ok){
        const j = await r.json();
        const names = (Array.isArray(j.corroborators) ? j.corroborators : [])
          .map(c => c.person).filter(p => typeof p === "string" && p.trim());
        PERSONS = Array.from(new Set(names)).sort();
      }
    }catch{}
    return PERSONS;
  }
  function entityLabel(id){
    if(!id) return WavrT("a device");
    const e = ENTITIES && ENTITIES.find(x => x.entity_id === id);
    return e ? e.name : id;   // fall back to the raw id — still honest, just unresolved
  }

  // ---- Plain-language summaries for the list (never a JSON dump) ----
  function triggerSummary(r){
    const p = r.trigger_params || {};
    switch(r.trigger_kind){
      case "house_arrived": return WavrT("When someone arrives");
      case "house_left": return WavrT("When everyone leaves");
      case "person_arrived": return WavrT("When {person} arrives", {person: p.person || WavrT("someone")});
      case "person_left": return WavrT("When {person} leaves", {person: p.person || WavrT("someone")});
      case "room_occupied": return WavrT("When the {room} fills", {room: p.room || WavrT("room")});
      case "room_empty": return WavrT("When the {room} empties", {room: p.room || WavrT("room")});
      case "schedule": return WavrT("At {time}", {time: p.at || "—"});
      case "house_away_by_time": return WavrT("If nobody is here by {time}", {time: p.by || "—"});
      case "device_seen": return WavrT("When a device shows up");
      case "no_motion": return WavrT("If {room} has no movement for {minutes} min",
        {room: p.room || WavrT("a room"), minutes: p.minutes != null ? p.minutes : "—"});
      default: return rotTriggerLabel(r.trigger_kind);
    }
  }
  function actionSummary(a){
    const p = (a && a.params) || {};
    if(a.kind === "ha_service"){
      return WavrT(p.service === "turn_off" ? "turn {name} off" : "turn {name} on",
                   {name: entityLabel(p.entity_id)});
    }
    if(a.kind === "notify") return WavrT("notify me: “{message}”", {message: p.message || ""});
    if(a.kind === "set_watch") return p.on ? WavrT("turn on discreet mode") : WavrT("turn off discreet mode");
    if(a.kind === "notify_new_devices") return p.message
      ? WavrT("tell me about new devices while I was out (prefix: “{message}”)", {message: p.message})
      : WavrT("tell me about new devices while I was out");
    return rotActionLabel(a && a.kind);
  }
  function routineSummary(r){
    const acts = Array.isArray(r.actions) ? r.actions : [];
    return triggerSummary(r) + " → " + (acts.length ? acts.map(actionSummary).join(", ") : WavrT("(no actions)"));
  }

  function renderList(){
    list.textContent = "";
    if(!ROUTINES.length){
      empty.hidden = false; list.hidden = true;
      return;
    }
    empty.hidden = true; list.hidden = false;
    ROUTINES.forEach(r => {
      const row = document.createElement("div"); row.className = "rot-row";
      const left = document.createElement("div"); left.className = "rot-row-left";
      const name = document.createElement("div"); name.className = "rot-name";
      name.textContent = r.name || WavrT("(unnamed routine)");        // textContent — user data
      left.appendChild(name);
      const summary = document.createElement("p"); summary.className = "rot-summary";
      summary.textContent = routineSummary(r);                 // textContent — server/user data
      left.appendChild(summary);
      const firedRel = fmtRelative(r.last_fired);
      if(firedRel){
        const meta = document.createElement("p");
        meta.className = "rot-meta" + (r.last_status === "partial" ? " warn"
          : r.last_status === "failed" ? " danger" : "");
        meta.textContent = WavrT("last ran {when}", {when: firedRel}) + (r.last_status ? " · " + WavrT(r.last_status) : "");
        left.appendChild(meta);
      }
      row.appendChild(left);

      const ctl = document.createElement("div"); ctl.className = "rot-row-ctl";
      const sw = document.createElement("button"); sw.type = "button";
      sw.className = "ctl switch small " + (r.enabled ? "on" : "off");
      sw.setAttribute("role", "switch");
      sw.setAttribute("aria-checked", r.enabled ? "true" : "false");
      sw.setAttribute("aria-label", WavrT(r.enabled ? "{name}: on" : "{name}: off", {name: r.name || WavrT("routine")}));
      sw.onclick = async ()=>{
        sw.disabled = true;
        let resp;
        try{
          resp = await __nf("/api/routines/"+encodeURIComponent(r.id)+"/enable",
            {method:"POST", headers:writeHdr(), body:JSON.stringify({on: !r.enabled})});
        }catch{}
        if(resp && resp.ok){
          actionFeedback(fb, true, null, !r.enabled ? WavrT("✓ turned on") : WavrT("✓ turned off"));
          refresh();
        } else {
          sw.disabled = false;
          actionFeedback(fb, false, WavrT("couldn't change — check the connection"));
        }
      };
      ctl.appendChild(sw);

      const testBtn = document.createElement("button"); testBtn.type = "button";
      testBtn.className = "ctl small"; testBtn.textContent = WavrT("Test");
      testBtn.setAttribute("data-tip", WavrT("Runs this routine's actions right now, for real — e.g. a light really turns on"));
      const testFb = document.createElement("span"); testFb.className = "action-fb"; testFb.setAttribute("aria-live", "polite");
      testBtn.onclick = async ()=>{
        testBtn.disabled = true; testFb.className = "action-fb"; testFb.textContent = WavrT("running…");
        let resp;
        try{
          resp = await __nf("/api/routines/"+encodeURIComponent(r.id)+"/test",
            {method:"POST", headers:writeHdr()});
        }catch{}
        testBtn.disabled = false;
        if(resp && resp.ok){
          let j; try{ j = await resp.json(); }catch{ j = {}; }
          const st = j.status || "ok";
          if(st === "ok") actionFeedback(testFb, true, null, WavrT("✓ ran ok"));
          else if(st === "partial") actionFeedback(testFb, false, WavrT("ran, but some actions failed"));
          else actionFeedback(testFb, false, WavrT("failed — check Home Assistant / the notifier"));
        } else {
          actionFeedback(testFb, false, WavrT("couldn't run — check the connection"));
        }
      };
      ctl.appendChild(testBtn); ctl.appendChild(testFb);

      const editBtn = document.createElement("button"); editBtn.type = "button";
      editBtn.className = "ctl small"; editBtn.textContent = WavrT("Edit");
      editBtn.onclick = (e)=>{ openBuilder(r, e.currentTarget); };
      ctl.appendChild(editBtn);

      // Two-step reveal-then-confirm — never a native confirm(), mirrors Nodes/Peers "Remove".
      const rmBtn = document.createElement("button"); rmBtn.type = "button"; rmBtn.className = "rm";
      rmBtn.textContent = WavrT("Remove");
      rmBtn.setAttribute("aria-label", WavrT("remove {name}", {name: r.name || WavrT("routine")}));
      rmBtn.onclick = ()=>{
        ctl.textContent = "";
        const warn = document.createElement("span"); warn.className = "pair-dev-meta"; warn.textContent = WavrT("Remove? ");
        const yes = document.createElement("button"); yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm");
        const no = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
        no.onclick = ()=>{ renderList(); };
        yes.onclick = async ()=>{
          yes.disabled = true; no.disabled = true;
          let resp;
          try{
            resp = await __nf("/api/routines/"+encodeURIComponent(r.id), {method:"DELETE", headers:deleteHdr()});
          }catch{}
          if(resp && resp.ok){ actionFeedback(fb, true, null, WavrT("✓ removed")); refresh(); }
          else { actionFeedback(fb, false, WavrT("couldn't remove")); renderList(); }
        };
        ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
      };
      ctl.appendChild(rmBtn);
      row.appendChild(ctl);
      list.appendChild(row);
    });
  }

  async function refresh(){
    let resp;
    try{ resp = await __nf("/api/routines", getOpt); }catch{ return; }   // backend momentarily unreachable — keep last view
    if(!resp || !resp.ok) return;
    let j; try{ j = await resp.json(); }catch{ j = {routines: []}; }
    ROUTINES = Array.isArray(j.routines) ? j.routines : [];
    renderList();
  }

  // ---- Builder overlay (New / Edit) — guided form only, never a JSON editor ----
  const overlay = document.getElementById("routineOverlay");
  const content = document.getElementById("routineContent");
  const title = document.getElementById("routine-h");
  const backBtn = document.getElementById("routineBack");
  let builderTrigger = null;

  function closeBuilder(){
    if(!overlay || overlay.hidden) return;
    overlay.hidden = true;
    if(window.__wavrSetAppInert) window.__wavrSetAppInert(false);
    if(builderTrigger && builderTrigger.focus) builderTrigger.focus();
    builderTrigger = null;
  }
  if(backBtn) backBtn.onclick = closeBuilder;
  if(overlay && window.__wavrTrapFocus) window.__wavrTrapFocus(overlay);
  document.addEventListener("keydown", (e)=>{ if(e.key === "Escape" && overlay && !overlay.hidden) closeBuilder(); });

  function elc(tag, cls, text){
    const e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text != null) e.textContent = text;
    return e;
  }
  function fieldRow(labelText){
    const wrap = elc("label", "pair-role");
    wrap.appendChild(document.createTextNode(labelText));
    return wrap;
  }

  // preset (optional): a ROT_QUICKSTARTS entry — pre-fills the form only, same as opening
  // "New routine" by hand. Never submits on its own; the user still names it and hits Save.
  async function openBuilder(existing, triggerEl, preset){
    if(!overlay || !content) return;
    builderTrigger = triggerEl || document.activeElement;
    title.textContent = existing ? WavrT("Edit routine") : WavrT("New routine");
    content.textContent = "";
    content.appendChild(elc("p", "narrate-out muted", WavrT("Loading…")));
    overlay.hidden = false;
    if(window.__wavrSetAppInert) window.__wavrSetAppInert(true);

    const [entities, persons] = await Promise.all([fetchEntities(), fetchPersons()]);

    // Local editable state, seeded from `existing` when editing, or from a quick-start `preset`
    // when creating from one. Never sent verbatim — the submit handler strips the UI-only
    // "_manual"/"_personManual" markers below first.
    const state = {
      name: existing ? (existing.name || "") : (preset ? preset.name : ""),
      trigger_kind: existing ? existing.trigger_kind : (preset ? preset.trigger_kind : "house_arrived"),
      trigger_params: existing ? Object.assign({}, existing.trigger_params) : (preset ? Object.assign({}, preset.trigger_params) : {}),
      actions: existing ? (existing.actions || []).map(a => ({kind: a.kind, params: Object.assign({}, a.params || {})}))
        : (preset ? [{kind: preset.action.kind, params: Object.assign({}, preset.action.params)}] : []),
      enabled: existing ? !!existing.enabled : false,
    };

    content.textContent = "";
    const form = document.createElement("form"); form.className = "rot-form";

    // -- Name --
    const nameTile = elc("div", "tile");
    const nameHead = elc("div", "tile-head"); nameHead.appendChild(elc("h2", null, WavrT("Name")));
    nameTile.appendChild(nameHead);
    const nameLabel = fieldRow(WavrT("Routine name"));
    const nameInput = document.createElement("input");
    nameInput.type = "text"; nameInput.maxLength = 80; nameInput.required = true;
    nameInput.value = state.name; nameInput.placeholder = WavrT("e.g. Everyone leaves");
    nameInput.setAttribute("aria-label", WavrT("routine name"));
    nameInput.oninput = ()=>{ state.name = nameInput.value; };
    nameLabel.appendChild(nameInput);
    nameTile.appendChild(nameLabel);
    form.appendChild(nameTile);

    // -- Trigger --
    const trigTile = elc("div", "tile");
    const trigHead = elc("div", "tile-head"); trigHead.appendChild(elc("h2", null, WavrT("When…")));
    trigTile.appendChild(trigHead);
    const trigLabel = fieldRow(WavrT("Trigger"));
    const trigSelect = document.createElement("select");
    trigSelect.setAttribute("aria-label", WavrT("trigger"));
    ROT_TRIGGERS.forEach(t => trigSelect.appendChild(new Option(WavrT(t.label), t.kind, t.kind === state.trigger_kind, t.kind === state.trigger_kind)));
    trigLabel.appendChild(trigSelect);
    trigTile.appendChild(trigLabel);
    const trigParamHost = elc("div", "rot-trigparam");
    trigTile.appendChild(trigParamHost);
    form.appendChild(trigTile);

    function renderTriggerParam(){
      trigParamHost.textContent = "";
      const meta = ROT_TRIGGERS.find(t => t.kind === state.trigger_kind);
      const key = meta && meta.paramKey;
      if(!key){
        trigParamHost.appendChild(elc("p", "panel-note", WavrT("No extra details needed for this trigger.")));
        return;
      }
      if(key === "person"){
        const wrap = fieldRow(WavrT("Person"));
        const useManual = !persons.length || state.trigger_params._personManual === true
          || (state.trigger_params.person && persons.indexOf(state.trigger_params.person) === -1);
        if(!useManual){
          const sel = document.createElement("select");
          sel.setAttribute("aria-label", WavrT("person"));
          sel.appendChild(new Option(WavrT("choose a person…"), "", !state.trigger_params.person, !state.trigger_params.person));
          persons.forEach(p => sel.appendChild(new Option(p, p, p === state.trigger_params.person, p === state.trigger_params.person)));
          sel.appendChild(new Option(WavrT("Someone else (type a name)…"), "__other__"));
          sel.onchange = ()=>{
            if(sel.value === "__other__"){ state.trigger_params._personManual = true; renderTriggerParam(); return; }
            state.trigger_params.person = sel.value;
          };
          wrap.appendChild(sel);
        } else {
          const inp = document.createElement("input");
          inp.type = "text"; inp.maxLength = 64; inp.setAttribute("aria-label", WavrT("person name"));
          inp.placeholder = WavrT("e.g. augusto"); inp.value = state.trigger_params.person || "";
          inp.oninput = ()=>{ state.trigger_params.person = inp.value; };
          wrap.appendChild(inp);
          if(persons.length){
            const back = document.createElement("button"); back.type = "button";
            back.className = "linklike"; back.textContent = WavrT("choose from the list instead");
            back.onclick = ()=>{ state.trigger_params._personManual = false; renderTriggerParam(); };
            wrap.appendChild(back);
          }
        }
        trigParamHost.appendChild(wrap);
      } else if(key === "room"){
        const wrap = fieldRow(WavrT("Room"));
        const inp = document.createElement("input");
        inp.type = "text"; inp.maxLength = 64; inp.setAttribute("aria-label", WavrT("room name"));
        inp.placeholder = WavrT("e.g. living room"); inp.value = state.trigger_params.room || "";
        inp.oninput = ()=>{ state.trigger_params.room = inp.value; };
        wrap.appendChild(inp);
        trigParamHost.appendChild(wrap);
      } else if(key === "at" || key === "by"){
        const wrap = fieldRow(key === "at" ? WavrT("Time") : WavrT("By this time"));
        const inp = document.createElement("input");
        inp.type = "time"; inp.setAttribute("aria-label", key === "at" ? WavrT("time of day") : WavrT("deadline time"));
        inp.value = state.trigger_params[key] || "";
        inp.oninput = ()=>{ state.trigger_params[key] = inp.value; };
        wrap.appendChild(inp);
        trigParamHost.appendChild(wrap);
      } else if(key === "mac"){
        const wrap = fieldRow(WavrT("Device address (MAC)"));
        const inp = document.createElement("input");
        inp.type = "text"; inp.maxLength = 32; inp.setAttribute("aria-label", WavrT("device MAC address"));
        inp.placeholder = "aa:bb:cc:dd:ee:ff"; inp.value = state.trigger_params.mac || "";
        inp.autocomplete = "off"; inp.spellcheck = false;
        inp.oninput = ()=>{ state.trigger_params.mac = inp.value; };
        wrap.appendChild(inp);
        trigParamHost.appendChild(wrap);
      } else if(key === "no_motion"){
        // Elder-care guardian trigger — needs BOTH room + minutes (routines.py._validate: a
        // non-positive-int minutes is a 400, surfaced verbatim in errBox on submit).
        const roomWrap = fieldRow(WavrT("Room"));
        const roomInp = document.createElement("input");
        roomInp.type = "text"; roomInp.maxLength = 64; roomInp.setAttribute("aria-label", WavrT("room name"));
        roomInp.placeholder = WavrT("e.g. living room"); roomInp.value = state.trigger_params.room || "";
        roomInp.oninput = ()=>{ state.trigger_params.room = roomInp.value; };
        roomWrap.appendChild(roomInp);
        trigParamHost.appendChild(roomWrap);

        const minWrap = fieldRow(WavrT("Minutes with no movement"));
        const minInp = document.createElement("input");
        minInp.type = "number"; minInp.min = "1"; minInp.step = "1";
        minInp.setAttribute("aria-label", WavrT("notify after this many minutes with no movement"));
        minInp.placeholder = WavrT("e.g. 180");
        if(state.trigger_params.minutes != null) minInp.value = String(state.trigger_params.minutes);
        minInp.oninput = ()=>{
          const n = parseInt(minInp.value, 10);
          if(Number.isFinite(n)) state.trigger_params.minutes = n; else delete state.trigger_params.minutes;
        };
        minWrap.appendChild(minInp);
        trigParamHost.appendChild(minWrap);

        // Honest, load-bearing note (ADR-0003): never let this trigger read as a medical/safety
        // alarm, and never claim stillness in a room Wavr structurally cannot sense.
        trigParamHost.appendChild(elc("p", "panel-note",
          WavrT("Best-effort awareness, not a medical or safety alarm (ADR-0003). Works only in a room "
          + "with a radar / mmWave motion sensor — it reads per-target velocity, so it can tell "
          + "when someone has stopped moving. A camera alone can't measure stillness, so Wavr "
          + "won't claim someone hasn't moved in a room it can't sense that way.")));
      }
    }
    renderTriggerParam();
    trigSelect.onchange = ()=>{
      state.trigger_kind = trigSelect.value;
      state.trigger_params = {};   // switching trigger kind drops the old, now-irrelevant param
      renderTriggerParam();
    };

    // -- Actions (a list; "Add all my lights (off)" fans out to N ha_service actions at once) --
    const actTile = elc("div", "tile");
    const actHead = elc("div", "tile-head");
    actHead.appendChild(elc("h2", null, WavrT("Then do this")));
    const addActBtn = document.createElement("button"); addActBtn.type = "button";
    addActBtn.className = "ctl small"; addActBtn.textContent = WavrT("+ Add action");
    actHead.appendChild(addActBtn);
    actTile.appendChild(actHead);
    const actList = elc("div", "rot-action-list");
    actTile.appendChild(actList);
    const addAllOffBtn = document.createElement("button"); addAllOffBtn.type = "button";
    addAllOffBtn.className = "ctl small"; addAllOffBtn.style.marginTop = "10px"; addAllOffBtn.hidden = true;
    addAllOffBtn.textContent = WavrT("Add all my lights (off)");
    addAllOffBtn.setAttribute("data-tip", WavrT("Adds one “turn off” action for every light/switch Wavr can control — the fast way to build “turn everything off”"));
    actTile.appendChild(addAllOffBtn);
    const actEmptyNote = elc("p", "panel-note", WavrT("Add at least one action."));
    actTile.appendChild(actEmptyNote);
    form.appendChild(actTile);

    function renderActions(){
      actList.textContent = "";
      addAllOffBtn.hidden = !entities.length;
      actEmptyNote.hidden = !!state.actions.length;
      state.actions.forEach((a, idx) => {
        const row = elc("div", "rot-action-row");
        const fields = elc("div", "rot-action-fields");

        const kindLabel = fieldRow(WavrT("Action"));
        const kindSel = document.createElement("select");
        kindSel.setAttribute("aria-label", WavrT("action {n} type", {n: idx+1}));
        ROT_ACTION_KINDS.forEach(ak => kindSel.appendChild(new Option(WavrT(ak.label), ak.kind, ak.kind === a.kind, ak.kind === a.kind)));
        kindSel.onchange = ()=>{ a.kind = kindSel.value; a.params = {}; renderActions(); };
        kindLabel.appendChild(kindSel);
        fields.appendChild(kindLabel);

        if(a.kind === "ha_service"){
          const entLabel = fieldRow(WavrT("Light/switch"));
          const useManual = !entities.length || a.params._manual === true
            || (a.params.entity_id && !entities.some(e => e.entity_id === a.params.entity_id));
          if(!useManual){
            const sel = document.createElement("select");
            sel.setAttribute("aria-label", WavrT("entity for action {n}", {n: idx+1}));
            sel.appendChild(new Option(WavrT("choose a light/switch…"), "", !a.params.entity_id, !a.params.entity_id));
            entities.forEach(e => sel.appendChild(new Option(e.name + " (" + e.domain + ")", e.entity_id,
              e.entity_id === a.params.entity_id, e.entity_id === a.params.entity_id)));
            sel.appendChild(new Option(WavrT("Type the entity ID myself…"), "__other__"));
            sel.onchange = ()=>{
              if(sel.value === "__other__"){ a.params._manual = true; renderActions(); return; }
              const chosen = entities.find(e => e.entity_id === sel.value);
              a.params.entity_id = sel.value;
              a.params.domain = chosen ? chosen.domain : (a.params.domain || "light");
              renderActions();
            };
            entLabel.appendChild(sel);
          } else {
            const inp = document.createElement("input");
            inp.type = "text"; inp.setAttribute("aria-label", WavrT("entity id for action {n}", {n: idx+1}));
            inp.placeholder = WavrT("e.g. light.sala"); inp.value = a.params.entity_id || "";
            inp.autocomplete = "off"; inp.spellcheck = false;
            inp.oninput = ()=>{
              a.params.entity_id = inp.value;
              a.params.domain = inp.value.split(".")[0] || "light";
            };
            entLabel.appendChild(inp);
            if(entities.length){
              const back = document.createElement("button"); back.type = "button";
              back.className = "linklike"; back.textContent = WavrT("choose from the list instead");
              back.onclick = ()=>{ a.params._manual = false; renderActions(); };
              entLabel.appendChild(back);
            }
          }
          fields.appendChild(entLabel);

          const onoffLabel = fieldRow(WavrT("Turn"));
          const onoffSel = document.createElement("select");
          onoffSel.setAttribute("aria-label", WavrT("on or off for action {n}", {n: idx+1}));
          if(!a.params.service) a.params.service = "turn_on";
          onoffSel.appendChild(new Option(WavrT("On"), "turn_on", a.params.service !== "turn_off", a.params.service !== "turn_off"));
          onoffSel.appendChild(new Option(WavrT("Off"), "turn_off", a.params.service === "turn_off", a.params.service === "turn_off"));
          onoffSel.onchange = ()=>{ a.params.service = onoffSel.value; };
          onoffLabel.appendChild(onoffSel);
          fields.appendChild(onoffLabel);
        } else if(a.kind === "notify"){
          const msgLabel = fieldRow(WavrT("Message"));
          const inp = document.createElement("input");
          inp.type = "text"; inp.maxLength = 200; inp.setAttribute("aria-label", WavrT("notification message for action {n}", {n: idx+1}));
          inp.placeholder = WavrT("e.g. Everyone left"); inp.value = a.params.message || "";
          inp.oninput = ()=>{ a.params.message = inp.value; };
          msgLabel.appendChild(inp);
          fields.appendChild(msgLabel);
        } else if(a.kind === "set_watch"){
          const wLabel = fieldRow(WavrT("Discreet mode"));
          const sel = document.createElement("select");
          sel.setAttribute("aria-label", WavrT("discreet mode for action {n}", {n: idx+1}));
          if(a.params.on === undefined) a.params.on = true;
          sel.appendChild(new Option(WavrT("Turn on"), "on", a.params.on === true, a.params.on === true));
          sel.appendChild(new Option(WavrT("Turn off"), "off", a.params.on !== true, a.params.on !== true));
          sel.onchange = ()=>{ a.params.on = (sel.value === "on"); };
          wLabel.appendChild(sel);
          fields.appendChild(wLabel);
        } else if(a.kind === "notify_new_devices"){
          // No required params (routines.py _ACTION_REQUIRED["notify_new_devices"] = ()) — the
          // notification body is computed at fire time from the count of devices first-seen
          // while the house was away; this text is only an optional prefix (e.g. "Home:").
          const msgLabel = fieldRow(WavrT("Message prefix (optional)"));
          const inp = document.createElement("input");
          inp.type = "text"; inp.maxLength = 200;
          inp.setAttribute("aria-label", WavrT("optional message prefix for action {n}", {n: idx+1}));
          inp.placeholder = WavrT("e.g. Here:"); inp.value = a.params.message || "";
          inp.oninput = ()=>{ a.params.message = inp.value; };
          msgLabel.appendChild(inp);
          fields.appendChild(msgLabel);
        }
        row.appendChild(fields);

        const rm = document.createElement("button"); rm.type = "button";
        rm.className = "rm"; rm.textContent = WavrT("Remove");
        rm.setAttribute("aria-label", WavrT("remove action {n}", {n: idx+1}));
        rm.onclick = ()=>{ state.actions.splice(idx, 1); renderActions(); };
        row.appendChild(rm);

        actList.appendChild(row);
      });
    }
    addActBtn.onclick = ()=>{ state.actions.push({kind:"ha_service", params:{service:"turn_on"}}); renderActions(); };
    addAllOffBtn.onclick = ()=>{
      entities.forEach(e => state.actions.push({kind:"ha_service", params:{domain:e.domain, service:"turn_off", entity_id:e.entity_id}}));
      renderActions();
    };
    renderActions();

    // -- Enable now (new routines only — an existing one already has its own on/off switch
    // in the list) + Save/Cancel --
    const saveTile = elc("div", "tile");
    if(!existing){
      const enableLabel = elc("label", "pair-role");
      const enableChk = document.createElement("input");
      enableChk.type = "checkbox"; enableChk.checked = state.enabled;
      enableChk.setAttribute("aria-label", WavrT("enable this routine now"));
      enableChk.onchange = ()=>{ state.enabled = enableChk.checked; };
      enableLabel.appendChild(enableChk);
      enableLabel.appendChild(document.createTextNode(" " + WavrT("Turn this routine on now")));
      saveTile.appendChild(enableLabel);
    }
    const errBox = elc("p", "action-fb err", "");
    errBox.hidden = true;
    saveTile.appendChild(errBox);
    const btnRow = elc("div", "controls-row");
    const saveBtn = document.createElement("button"); saveBtn.type = "submit";
    saveBtn.className = "ctl primary"; saveBtn.textContent = existing ? WavrT("Save changes") : WavrT("Create routine");
    const cancelBtn = document.createElement("button"); cancelBtn.type = "button";
    cancelBtn.className = "ctl off"; cancelBtn.textContent = WavrT("Cancel");
    cancelBtn.onclick = closeBuilder;
    btnRow.appendChild(saveBtn); btnRow.appendChild(cancelBtn);
    saveTile.appendChild(btnRow);
    form.appendChild(saveTile);

    form.onsubmit = async (e)=>{
      e.preventDefault();
      errBox.hidden = true;
      // Strip UI-only markers (which widget to show) before this ever reaches the network —
      // they are never real routine fields.
      const cleanTriggerParams = Object.assign({}, state.trigger_params);
      delete cleanTriggerParams._personManual;
      const cleanActions = state.actions.map(a => {
        const p = Object.assign({}, a.params);
        delete p._manual;
        return {kind: a.kind, params: p};
      });
      const body = {
        name: state.name.trim(), trigger_kind: state.trigger_kind,
        trigger_params: cleanTriggerParams, actions: cleanActions,
      };
      if(!existing) body.enabled = state.enabled;
      saveBtn.disabled = true;
      let resp;
      try{
        resp = existing
          ? await __nf("/api/routines/"+encodeURIComponent(existing.id), {method:"PUT", headers:writeHdr(), body:JSON.stringify(body)})
          : await __nf("/api/routines", {method:"POST", headers:writeHdr(), body:JSON.stringify(body)});
      }catch{}
      saveBtn.disabled = false;
      if(resp && resp.ok){
        closeBuilder();
        actionFeedback(fb, true, null, existing ? WavrT("✓ routine updated") : WavrT("✓ routine created"));
        refresh();
      } else {
        // Honest validation echo — routines.py's ValueError text names exactly which field
        // was wrong (e.g. "trigger 'schedule' requires param 'at'"), never a generic failure.
        let msg = WavrT("couldn't save — check the fields above");
        try{ const j = await resp.json(); if(j && j.detail) msg = String(j.detail); }catch{}
        errBox.hidden = false; errBox.textContent = msg;
      }
    };

    content.appendChild(form);
    nameInput.focus();
  }

  const rotNewBtn = document.getElementById("rotNewBtn");
  const rotEmptyCta = document.getElementById("rotEmptyCta");
  if(rotNewBtn) rotNewBtn.onclick = (e)=>{ openBuilder(null, e.currentTarget); };
  if(rotEmptyCta) rotEmptyCta.onclick = (e)=>{ openBuilder(null, e.currentTarget); };

  // ---- Quick starts: one tap OPENS the builder pre-filled — never a silent create. Sits
  // above both the empty state and the list (single element, positioned once), so it shows
  // in either case without duplicating markup. ----
  const rotQuick = document.getElementById("rotQuick");
  if(rotQuick){
    rotQuick.textContent = "";
    rotQuick.appendChild(elc("h4", "det-sub", WavrT("Quick starts")));
    const row = elc("div", "rot-quick-row");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", WavrT("quick-start routines"));
    ROT_QUICKSTARTS.forEach(qs => {
      const b = document.createElement("button"); b.type = "button";
      b.className = "ctl small rot-quick-btn"; b.textContent = WavrT(qs.label);
      b.onclick = (e)=>{ openBuilder(null, e.currentTarget, qs); };
      row.appendChild(b);
    });
    rotQuick.appendChild(row);
  }

  // Entities BEFORE the first list paint so a "turn X on/off" summary shows the friendly HA
  // name from the start, not the raw entity_id until the builder happens to be opened once.
  await fetchEntities();
  await refresh();
}
renderRoutines();

