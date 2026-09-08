// ==========================================================================
// identity.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- My devices — consent-first identity registry (live only, 2026-07-06 ethics) ----
// The registry holds ONLY admin-confirmed devices. Three surfaces: (1) "Detect my paired
// devices" -> GET /api/identity/bonded lists THIS PC's bonded BT devices PRE-CHECKED (a
// pairing act is a suggestion, not consent — the admin unchecks a housemate's speaker and
// confirms) -> POST /api/identity/devices. (2) manual add (address+name) for non-bonded.
// (3) the registered list, each with Un-register = the participation opt-out (DELETE ->
// the live provider stops returning it next scan cycle and its name is gone). Router-level
// require_central: on loopback root (MODE==="live") the admin always passes; a multidevice
// 'user' peer gets 403 and the panel stays hidden. `person` is PII — EVERY name/address is
// rendered via textContent, NEVER innerHTML.
//
// Every toast on this screen goes through WavrT, the successes included. They
// did not: `actionFeedback(fb, false, WavrT("couldn't add — check the name"))`
// beside `actionFeedback(fb, true, null, "✓ added")`, four times over. So one
// button answered a Portuguese-speaking household in Portuguese when it failed
// and in English when it worked — and the same "✓ added" was translated from
// the mobile import a few lines down, which is what makes it read as a bug in
// the product rather than as a language it does not speak.
async function renderIdentity(){
  if(MODE!=="live") return;                 // live-only; never in companion/demo
  let probe;
  try{ probe = await WavrAPI.fetch("/api/identity/devices"); }
  catch{ return; }                          // backend unreachable — stay hidden
  if(!probe.ok) return;                     // 403 (multidevice 'user') — stay hidden
  let seed; try{ seed = (await probe.json()).devices; }catch{ seed = []; }
  document.getElementById("myDevices").hidden = false;

  const devList  = document.getElementById("idDevList");
  const detectBtn = document.getElementById("idDetectBtn");
  const bondedBox = document.getElementById("idBondedBox");
  const bondedList = document.getElementById("idBondedList");
  const bondedName = document.getElementById("idBondedName");
  const bondedRegister = document.getElementById("idBondedRegister");
  const detectFb = document.getElementById("idDetectFb");
  const manualForm = document.getElementById("idManualForm");
  const manualFb = document.getElementById("idManualFb");

  // Registered list — each row is one consented device; Un-register is the opt-out.
  function renderList(devices){
    devices = Array.isArray(devices) ? devices : [];
    devList.textContent = "";
    if(!devices.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("no registered devices yet");
      devList.appendChild(e); return;
    }
    devices.forEach(d => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = d.person || WavrT("(unnamed)"); // textContent — PII
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      const sig = modalityLabel(d.source) || d.source || "?";
      const via = d.origin === "bonded" ? WavrT(" · paired") : "";
      meta.textContent = " · " + sig + " · " + (d.address || "") + via;   // textContent — server data
      left.appendChild(name); left.appendChild(meta);
      row.appendChild(left);
      const rm = document.createElement("button"); rm.type = "button"; rm.className = "rm";
      rm.textContent = WavrT("Un-register");
      rm.setAttribute("aria-label", WavrT("un-register {name} — privacy opt-out", {name: d.person || WavrT("device")}));
      rm.onclick = async ()=>{
        let r;
        try{ r = await WavrAPI.fetch("/api/identity/devices/"+encodeURIComponent(d.address), {method: "DELETE"}); }catch{}
        if(r && r.ok){
          let j; try{ j = await r.json(); }catch{ j = {}; }
          actionFeedback(manualFb, true, null, WavrT("✓ opted out")); renderList(j.devices);
        } else actionFeedback(manualFb, false, WavrT("couldn't un-register"));
      };
      row.appendChild(rm); devList.appendChild(row);
    });
  }

  // (1) Detect — read THIS PC's bonded BT devices; pre-check the not-yet-registered ones.
  detectBtn.onclick = async ()=>{
    let devices;
    try{ devices = (await (await WavrAPI.fetch("/api/identity/bonded")).json()).devices; }
    catch{ actionFeedback(detectFb, false, WavrT("couldn't read paired devices")); return; }
    devices = Array.isArray(devices) ? devices : [];
    bondedList.textContent = "";
    bondedBox.hidden = false;
    if(!devices.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("no paired Bluetooth devices found on this PC");
      bondedList.appendChild(e); return;
    }
    devices.forEach(d => {
      const already = !!d.already_registered;
      const row = document.createElement("div");
      row.className = "id-bonded-row" + (already ? " dim" : "");
      const lab = document.createElement("label");
      const cb = document.createElement("input"); cb.type = "checkbox";
      cb.checked = !already;                 // SUGGESTION: pre-check unregistered, admin can uncheck
      cb.disabled = already;                 // already registered -> not re-offered here
      cb.dataset.address = d.address || "";
      const nm = document.createElement("b"); nm.textContent = d.name || WavrT("(unnamed device)"); // textContent
      const mac = document.createElement("span"); mac.className = "mac"; mac.textContent = d.address || ""; // textContent
      lab.appendChild(cb); lab.appendChild(nm); lab.appendChild(mac);
      row.appendChild(lab);
      if(already){
        const tag = document.createElement("span"); tag.className = "id-bonded-tag";
        tag.textContent = WavrT("already registered");
        row.appendChild(tag);
      }
      bondedList.appendChild(row);
    });
  };

  // Register the checked bonded devices under the admin's name (origin='bonded').
  bondedRegister.onclick = async ()=>{
    const person = (bondedName.value || "").trim();
    if(!person){ actionFeedback(detectFb, false, WavrT("enter your name first")); return; }
    const checks = Array.from(bondedList.querySelectorAll('input[type=checkbox]'))
                        .filter(cb => cb.checked && !cb.disabled);
    if(!checks.length){ actionFeedback(detectFb, false, WavrT("check at least one device")); return; }
    const devices = checks.map(cb => ({address: cb.dataset.address, source: "ble", origin: "bonded"}));
    let r;
    try{ r = await WavrAPI.fetch("/api/identity/devices", {method: "POST", json: {person, devices}}); }catch{}
    if(r && r.ok){
      let j; try{ j = await r.json(); }catch{ j = {}; }
      actionFeedback(detectFb, true, null, WavrT("✓ registered")); renderList(j.devices);
      bondedBox.hidden = true;               // collapse the suggestion list after a confirm
    } else actionFeedback(detectFb, false, WavrT("couldn't register — check the name"));
  };

  // (2) Manual add — address + name for anything not bonded (origin='manual').
  manualForm.onsubmit = async (e)=>{
    e.preventDefault();
    const address = (manualForm.address.value || "").trim();
    const label   = (manualForm.label.value || "").trim();
    const source  = manualForm.source.value || "ble";
    if(!address){ actionFeedback(manualFb, false, WavrT("enter an address")); return; }
    if(!label){ actionFeedback(manualFb, false, WavrT("enter a name")); return; }
    let r;
    try{ r = await WavrAPI.fetch("/api/identity/devices", {method: "POST", json: {person: label, devices:[{address, source, origin:"manual"}]}}); }catch{}
    if(r && r.ok){
      let j; try{ j = await r.json(); }catch{ j = {}; }
      actionFeedback(manualFb, true, null, WavrT("✓ added")); renderList(j.devices); manualForm.reset();
    } else actionFeedback(manualFb, false, WavrT("invalid address or name"));
  };

  renderList(seed);                          // seed the registered list from the probe we already read
}
renderIdentity();

// ---- Item 7: add a device THIS phone is bonded to over Bluetooth (mobile ADMIN device only) ----
// Guarded by the shim contract: window.WAVR_MOBILE + role 'central' + listBondedDevices. Absent it —
// which is ALL THREE web modes (loopback/companion-web/demo) and any non-admin device — this is a
// no-op and #mobileBtImport stays hidden, so web behavior is byte-identical. Reads bonds from the
// native sibling plugin (no BLE scan, no location), diffs against GET /api/identity/devices,
// PRIMARILY via the one-consent addAllBonded() bulk import (Fix C-front c); per-device add stays
// available as the secondary path. Router-level require_central + admin already gates the POST
// server-side (a user tablet 403s). Honest limitation: a phone-bonded MAC is a LABEL enriching
// the registry — live presence still depends on the hub's own radios.
// Fix C-front (b): listBondedDevices() failing is not one undifferentiated "unavailable" —
// distinguish permission-needed / bluetooth-off (both recoverable, retry in place) from a
// genuinely absent capability, from whatever detail the native plugin's thrown error carries.
// Degrades safely to the old honest "not available" copy if the shim gives no detail at all.
function classifyBtUnavailable(err){
  const detail = String((err && (err.code || err.message)) || "").toLowerCase();
  if(/permission/.test(detail)) return "permission-needed";
  if(/bluetooth.*(off|disabled|not enabled|not on)|(disabled|off).*bluetooth/.test(detail)) return "bluetooth-off";
  return "not-available";
}
async function renderMobileBonded(){
  const m = window.WAVR_MOBILE;
  if(!(m && MODE==="companion" && m.role==="central" && typeof m.listBondedDevices==="function")) return;
  const box = document.getElementById("mobileBtImport");
  const list = document.getElementById("mobileBtList");
  const fb = document.getElementById("mobileBtFb");
  if(!box || !list) return;
  const auth = {"Authorization":"Bearer "+companionToken()};
  const __nf = (path, opt)=> m.netFetch(m.base + path, opt);
  box.hidden = false;
  let bonded, btErr = null;
  try{ bonded = await m.listBondedDevices(); }catch(e){ bonded = null; btErr = e; }
  if(bonded === null){                        // capability unavailable — honest, never a silent empty list
    list.textContent = "";
    const reason = classifyBtUnavailable(btErr);
    if(reason === "permission-needed" || reason === "bluetooth-off"){
      const p = document.createElement("p"); p.className = "net-hint";
      p.textContent = reason === "permission-needed"
        ? WavrT("Wavr needs Bluetooth permission on this phone to see devices already paired to it.")
        : WavrT("Bluetooth is off on this phone.");
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "ctl small";
      retry.textContent = reason === "permission-needed" ? WavrT("Grant Bluetooth access") : WavrT("Turn on Bluetooth and retry");
      // Both states are recoverable in place: calling listBondedDevices() again gives the OS a
      // fresh chance to prompt for permission, or picks up Bluetooth being turned back on in
      // Settings meanwhile -- renderMobileBonded() is idempotent (Fix C-front a), safe to re-run.
      retry.onclick = ()=> renderMobileBonded();
      list.appendChild(p); list.appendChild(retry);
    } else {
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("Bluetooth device import isn't available on this device.");
      list.appendChild(e);
    }
    return;
  }
  const registered = new Set();
  try{
    const r = await __nf("/api/identity/devices", {headers:auth});
    if(r.status===401||r.status===403){ companionAuthFailed(); return; }
    if(r.ok){ const j = await r.json();
      (Array.isArray(j.devices)?j.devices:[]).forEach(d=>{ if(d && d.address) registered.add(String(d.address).toLowerCase()); }); }
  }catch{}
  const fresh = bonded.filter(d => d.address && !registered.has(String(d.address).toLowerCase()));
  list.textContent = "";
  if(!fresh.length){
    const e = document.createElement("div"); e.className = "empty";
    e.textContent = bonded.length ? WavrT("All paired devices are already added.") : WavrT("No paired Bluetooth devices found on this phone.");
    list.appendChild(e); return;
  }
  // Fix C-front (c): PRIMARY UX — one consent covers every bonded device at once (bulk-consent
  // contract: the shim's addAllBonded() does the consent prompt + the POST /api/identity/devices
  // loop natively). Only shown when there's something fresh to add; per-device rows below stay as
  // the secondary, one-at-a-time path (e.g. to label devices individually before adding).
  if(typeof m.addAllBonded === "function"){
    const addAllRow = document.createElement("div"); addAllRow.className = "controls-row";
    const addAllBtn = document.createElement("button"); addAllBtn.type = "button"; addAllBtn.className = "ctl";
    addAllBtn.textContent = WavrT("Add all {n} to Wavr", {n: fresh.length});
    addAllBtn.onclick = async ()=>{
      addAllBtn.disabled = true;
      let result;
      try{ result = await m.addAllBonded(); }catch{ result = null; }
      if(result){ actionFeedback(fb, true, null, WavrT("✓ added")); renderMobileBonded(); }
      else { addAllBtn.disabled = false; actionFeedback(fb, false, WavrT("couldn't add — try again")); }
    };
    addAllRow.appendChild(addAllBtn);
    list.appendChild(addAllRow);
  }
  fresh.forEach(d => {
    const row = document.createElement("div"); row.className = "pair-dev-row";
    const left = document.createElement("span");
    const nm = document.createElement("b"); nm.textContent = d.label || "(unnamed device)";     // textContent — user data
    const mac = document.createElement("span"); mac.className = "pair-dev-meta";
    mac.textContent = " · " + (d.address || "");                                                 // textContent — server/plugin data
    left.appendChild(nm); left.appendChild(mac); row.appendChild(left);
    const personIn = document.createElement("input"); personIn.type = "text";
    personIn.placeholder = WavrT("whose device?"); personIn.value = d.label || "";
    const add = document.createElement("button"); add.type = "button"; add.className = "ctl small"; add.textContent = WavrT("Add");
    add.onclick = async ()=>{
      const person = (personIn.value || "").trim();
      if(!person){ actionFeedback(fb, false, WavrT("enter whose device this is")); return; }
      add.disabled = true;
      let r;
      try{ r = await __nf("/api/identity/devices", {method:"POST",
        headers:{...auth, "Content-Type":"application/json"},
        body:JSON.stringify({person, devices:[{address:d.address, source:"ble", origin:"bonded"}]})}); }catch{}
      if(r && (r.status===401||r.status===403)){ companionAuthFailed(); return; }
      if(r && r.ok){
        actionFeedback(fb, true, null, WavrT("✓ added")); row.remove();
        if(!list.querySelector(".pair-dev-row")){
          const e = document.createElement("div"); e.className = "empty";
          e.textContent = WavrT("All paired devices are already added."); list.appendChild(e);
        }
      } else { add.disabled = false; actionFeedback(fb, false, WavrT("couldn't add — check the name")); }
    };
    row.appendChild(personIn); row.appendChild(add); list.appendChild(row);
  });
}
// renderMobileBonded() is invoked from the mobile post-ready boot, PLUS re-invoked (idempotent)
// whenever companionIsCentral() resolves the backend-confirmed role (Fix C-front a, see its
// resolve callback near companionToken()) -- web modes never call it either way.

