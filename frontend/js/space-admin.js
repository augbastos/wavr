// ==========================================================================
// space-admin.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ============================================================================
// Space (Settings → Space): the Space model's admin surface (backend/wavr/space_store.py +
// api_space.py) — name/kind, People, Devices' three independent axes, and Core topology.
// GET/PUT /api/space, /api/space/people, /api/space/devices, /api/space/cores. Loopback-root
// or an admin-scoped device only, live-only — same gate idiom as the rest of this overlay.
// Every name/room/id below is operator/server data -> textContent, never innerHTML.
// ============================================================================

// `fb` is a SHARED, persistent feedback span living OUTSIDE the list this row belongs to
// (matches renderPairing()'s own pairFb precedent) — `onChanged` rebuilds the whole list on
// success, which would destroy a per-row feedback node before it could be read, so the
// message has to live somewhere the rebuild does not touch.
function buildPersonRow(p, roles, fb, onChanged){
  var row = document.createElement("div"); row.className = "pair-dev-row";
  var left = document.createElement("span");
  var name = document.createElement("b"); name.textContent = p.display_name || WavrT("(unnamed)");
  var meta = document.createElement("span"); meta.className = "pair-dev-meta";
  meta.textContent = " · " + p.role + (p.expired ? WavrT(" · expired") : "");
  left.appendChild(name); left.appendChild(meta);
  row.appendChild(left);

  var ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
  if(p.role === "owner"){
    // Deliberately not editable from here: space_store.set_person_role() refuses to demote
    // or promote-into an Owner (transfer_ownership is a separate, loopback-root-ONLY act
    // this screen does not expose) — a neutral badge, not a dead-end control.
    var badge = document.createElement("span"); badge.className = "pill off"; badge.textContent = WavrT("Owner");
    ctl.appendChild(badge);
  } else {
    var sel = document.createElement("select"); sel.className = "pair-dev-role";
    sel.setAttribute("aria-label", WavrT("role for {name}", {name: p.display_name || WavrT("person")}));
    (roles || []).filter(function(r){ return r !== "owner"; }).forEach(function(r){
      var o = document.createElement("option"); o.value = r;
      o.textContent = r.charAt(0).toUpperCase() + r.slice(1);
      if(r === p.role) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = async function(){
      var prev = p.role;
      sel.disabled = true;
      var r;
      try{ r = await WavrAPI.fetch("/api/space/people/" + encodeURIComponent(p.person_id) + "/role", {method: "POST", json: {role: sel.value}}); }catch(e){}
      if(r && r.ok){
        var j; try{ j = await r.json(); }catch(e2){ j = {}; }
        // Honest disclosure the endpoint itself returns: a role change never revokes an
        // already-issued credential — surface that note rather than implying it happened.
        actionFeedback(fb, true, null, j.note ? WavrT("✓ changed — {note}", {note: j.note}) : WavrT("✓ changed"));
        onChanged();
      } else {
        sel.disabled = false; sel.value = prev;
        actionFeedback(fb, false, WavrT("couldn't change the role"));
      }
    };
    ctl.appendChild(sel);

    var rm = document.createElement("button"); rm.type = "button"; rm.className = "rm"; rm.textContent = WavrT("Remove");
    rm.setAttribute("aria-label", WavrT("remove {name}", {name: p.display_name || WavrT("person")}));
    rm.onclick = function(){
      // Two-step reveal-then-confirm, never a native confirm() — mirrors Unpair/Remove elsewhere.
      ctl.textContent = "";
      var warn = document.createElement("span"); warn.className = "pair-dev-meta";
      warn.textContent = WavrT("Remove {name}? Their devices lose access immediately.", {name: p.display_name || WavrT("this person")}) + " ";
      var yes = document.createElement("button"); yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm");
      var no = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
      no.onclick = function(){ onChanged(); };
      yes.onclick = async function(){
        yes.disabled = true; no.disabled = true;
        var r;
        try{ r = await WavrAPI.fetch("/api/space/people/" + encodeURIComponent(p.person_id), {method: "DELETE"}); }catch(e){}
        if(r && r.ok){ actionFeedback(fb, true, null, WavrT("✓ removed")); onChanged(); }
        else {
          // Never a silent no-op: e.g. the Owner can't be removed (422) — say so, don't just
          // quietly leave them in the list with no explanation.
          var msg = await failureText(r, WavrT("remove it"));
          actionFeedback(fb, false, msg);
          onChanged();
        }
      };
      ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
    };
    ctl.appendChild(rm);
  }
  row.appendChild(ctl);
  return row;
}

function buildDeviceRow(d, people, funcsAvailable, onChanged){
  var row = document.createElement("div"); row.className = "pair-dev-row space-dev-row";
  var left = document.createElement("span");
  var name = document.createElement("b"); name.textContent = d.name || WavrT("(unnamed)");
  var meta = document.createElement("span"); meta.className = "pair-dev-meta";
  meta.textContent = WavrT(" · role: {role}", {role: d.role || "?"}) + (d.revoked ? WavrT(" · revoked") : "");
  left.appendChild(name); left.appendChild(meta);
  row.appendChild(left);

  var ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";

  var funcWrap = document.createElement("span"); funcWrap.className = "space-func-wrap";
  var boxes = {};
  funcsAvailable.forEach(function(f){
    var lab = document.createElement("label"); lab.className = "space-func-chk";
    var cb = document.createElement("input"); cb.type = "checkbox"; cb.value = f;
    cb.checked = (d.functions || []).indexOf(f) !== -1;
    boxes[f] = cb;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(f));
    funcWrap.appendChild(lab);
  });
  var roomInp = document.createElement("input"); roomInp.type = "text"; roomInp.className = "space-room-inp";
  roomInp.placeholder = WavrT("room"); roomInp.value = d.room || "";
  roomInp.setAttribute("aria-label", WavrT("room for {name}", {name: d.name || WavrT("device")}));
  var portableChk = document.createElement("input"); portableChk.type = "checkbox"; portableChk.checked = !!d.portable;
  var portableLab = document.createElement("label"); portableLab.className = "space-func-chk";
  portableLab.appendChild(portableChk); portableLab.appendChild(document.createTextNode(WavrT("portable")));

  var funcFb = document.createElement("span"); funcFb.className = "action-fb"; funcFb.setAttribute("aria-live","polite");
  var saveFn = document.createElement("button"); saveFn.type = "button"; saveFn.className = "ctl small";
  saveFn.textContent = WavrT("Save functions");
  saveFn.onclick = async function(){
    var wanted = Object.keys(boxes).filter(function(f){ return boxes[f].checked; });
    saveFn.disabled = true;
    var r;
    try{ r = await WavrAPI.fetch("/api/space/devices/" + encodeURIComponent(d.device_id) + "/functions", {method: "PUT", json: {functions: wanted, room: roomInp.value, portable: portableChk.checked}}); }catch(e){}
    saveFn.disabled = false;
    if(r && r.ok) actionFeedback(funcFb, true);
    else actionFeedback(funcFb, false, WavrT("couldn't save"));
  };

  var personSel = document.createElement("select"); personSel.className = "pair-dev-role";
  personSel.setAttribute("aria-label", WavrT("person for {name}", {name: d.name || WavrT("device")}));
  var noneOpt = document.createElement("option"); noneOpt.value = ""; noneOpt.textContent = WavrT("(unassigned)");
  personSel.appendChild(noneOpt);
  people.forEach(function(p){
    var o = document.createElement("option"); o.value = p.person_id; o.textContent = p.display_name;
    if(p.person_id === d.person_id) o.selected = true;
    personSel.appendChild(o);
  });
  var personFb = document.createElement("span"); personFb.className = "action-fb"; personFb.setAttribute("aria-live","polite");
  personSel.onchange = async function(){
    var pid = personSel.value || null;
    personSel.disabled = true;
    var r;
    try{ r = await WavrAPI.fetch("/api/space/devices/" + encodeURIComponent(d.device_id) + "/person", {method: "POST", json: {person_id: pid}}); }catch(e){}
    personSel.disabled = false;
    if(r && r.ok) actionFeedback(personFb, true);
    else actionFeedback(personFb, false, WavrT("couldn't save"));
  };

  ctl.appendChild(funcWrap); ctl.appendChild(roomInp); ctl.appendChild(portableLab);
  ctl.appendChild(saveFn); ctl.appendChild(funcFb);
  ctl.appendChild(personSel); ctl.appendChild(personFb);
  row.appendChild(ctl);
  return row;
}

function renderCoresWarn(container, topo){
  container.textContent = "";
  var msgs = [];
  if(topo.contested) msgs.push({txt: WavrT("Two Cores both claim to be authoritative right now. Promote " +
    "the one you want to keep — the other should be demoted."), danger:true});
  else if(topo.leaderless) msgs.push({txt: WavrT("No Core is authoritative right now. Promote one below " +
    "to bring the Space back to life."), danger:true});
  else if(topo.primary_stale) msgs.push({txt: WavrT("The current primary Core hasn't checked in recently."), danger:false});
  msgs.forEach(function(m){
    var box = document.createElement("div"); box.className = "pair-approve" + (m.danger ? " danger" : "");
    var p = document.createElement("p"); p.className = "pa-h"; p.textContent = m.txt;
    box.appendChild(p);
    container.appendChild(box);
  });
}

// `fb` is a SHARED, persistent feedback span living OUTSIDE #spaceCoresList (same reasoning
// as buildPersonRow's own `fb` param) — `onChanged` rebuilds the whole list, which would
// otherwise destroy a per-row feedback node before its message could be read.
// The three states that lead to different actions. `observing` is what the
// backend computed from each sensor's health; nothing is recomputed here, so the
// dashboard and an agent asking over MCP cannot disagree.
async function renderCoverage(){
  var tile = document.getElementById("spaceCoverageTile");
  var list = document.getElementById("spaceCoverageList");
  if(!tile || !list) return;
  var r;
  try{ r = await WavrAPI.fetch("/api/coverage"); }
  catch{ return; }                              // hub unreachable -> leave hidden
  if(!r.ok) return;
  var body;
  try{ body = await r.json(); }catch{ return; }
  if(!body || body.available === false) return; // could not enumerate -> say nothing

  // ---- The comparative view -------------------------------------------------
  //
  // Derived from the SAME `precision_level` the row below renders, so the table
  // and the sentence can never disagree. The ladder is Wavr's own
  // (`fusion.RESOLUTION_SCOPE`): none < house < room < count < position, and a
  // rung means every rung below it is available too.
  var LADDER = ["none", "house", "room", "count", "position"];
  var tbody = document.querySelector("#spaceCapMatrix tbody");
  if (tbody) {
    tbody.textContent = "";
    // Identity is a house-wide SETTING, not a per-room capability, and it is
    // off unless somebody turned it on. Rendering it as a per-room "no" would
    // read as a hardware limit rather than a choice.
    var identityOn = !!(body.identity_enabled);
    (body.rooms || []).forEach(function (room) {
      var rung = LADDER.indexOf(String(room.precision_level || "none"));
      var tr = document.createElement("tr");
      var th = document.createElement("th");
      th.scope = "row"; th.textContent = room.room;
      tr.appendChild(th);

      // A room that HAS the sensors but is not being watched right now is a
      // third state: it is not a missing capability, it is a lost one, and the
      // errand is different.
      var watching = room.observing !== false;
      [["room", "Presence"], ["count", "Count"], ["position", "Position"]]
        .forEach(function (pair) {
          var need = LADDER.indexOf(pair[0]);
          var td = document.createElement("td");
          var has = rung >= need && need > 0;
          if (has && !watching) {
            td.dataset.has = "lost"; td.textContent = WavrT("— not right now");
          } else if (has) {
            td.dataset.has = "yes"; td.textContent = WavrT("✓ yes");
          } else {
            td.dataset.has = "no"; td.textContent = WavrT("— no");
          }
          tr.appendChild(td);
        });

      var idt = document.createElement("td");
      if (identityOn) { idt.dataset.has = "yes"; idt.textContent = WavrT("✓ on"); }
      else { idt.dataset.has = "off"; idt.textContent = WavrT("off by choice"); }
      tr.appendChild(idt);
      tbody.appendChild(tr);
    });
  }

  list.textContent = "";
  (body.rooms || []).forEach(function(room){
    var row = document.createElement("div"); row.className = "pair-dev-row";
    var left = document.createElement("span");
    var name = document.createElement("b"); name.textContent = room.room;
    var meta = document.createElement("span"); meta.className = "pair-dev-meta";
    var parts = (room.sensors || []).map(function(s){
      // A sensor that is not observing says WHY in one word, because "camera
      // (off)" and "radar (not answering)" are different errands.
      var how = s.observing ? "" :
        (s.health === "disabled" ? WavrT(" (off)") :
         s.health === "offline" ? WavrT(" (not answering)") : " (" + s.health + ")");
      // The household's word for the sensor, not the wire's. This line read
      // "Bedroom · mmwave, pir · watched now": `mmwave` and `pir` are the
      // values the fusion engine passes around, and the very same two sensors
      // are called "Radar" and "Motion sensor (PIR)" on the room card. Guarded
      // the way devices.js and tooltips.js guard it, because `modalityLabel`
      // is a global out of radar.js and script order is all that provides it.
      var word = (typeof modalityLabel === "function")
        ? (modalityLabel(s.modality) || s.modality || s.kind)
        : (s.modality || s.kind);
      return word + how;
    });
    meta.textContent = " \u00b7 " + (parts.join(", ") || WavrT("nothing")) +
      " \u00b7 " + (room.observing
        ? WavrT("watched now, to {level} detail", {level: room.precision_level})
        : WavrT("NOT being watched"));
    left.appendChild(name); left.appendChild(meta);
    row.appendChild(left);

    // The room has a camera that is only counting people. Offer the walk-through
    // that lifts it to placing them -- this is where someone asks why Wavr will
    // not say WHERE in the room somebody is.
    var uncal = (room.sensors || []).find(function(s){
      return s.kind === "camera" && !s.calibrated;
    });
    if(uncal){
      var ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
      var btn = document.createElement("button");
      btn.type = "button"; btn.className = "ctl small";
      btn.textContent = WavrT("Set up positioning");
      btn.onclick = function(){
        window.__wavrCalibrateNext = uncal.sensor_id;
        if(window.switchTab) window.switchTab("dispositivos");
      };
      ctl.appendChild(btn);
      row.appendChild(ctl);
    }
    list.appendChild(row);
  });

  (body.uncovered || []).forEach(function(name){
    var row = document.createElement("div"); row.className = "pair-dev-row";
    var left = document.createElement("span");
    var b = document.createElement("b"); b.textContent = name;
    var meta = document.createElement("span"); meta.className = "pair-dev-meta";
    // The sentence that matters. An unwatched room is not an empty room, and a
    // dashboard that renders the two the same way is the reason this tile exists.
    meta.textContent = " \u00b7 " + WavrT("no sensor at all \u2014 Wavr cannot see this room, " +
      "which is not the same as it being empty");
    left.appendChild(b); left.appendChild(meta);
    row.appendChild(left);
    list.appendChild(row);
  });

  (body.house_wide || []).forEach(function(s){
    var row = document.createElement("div"); row.className = "pair-dev-row";
    var left = document.createElement("span");
    var b = document.createElement("b");
    // Same reason as the per-room list above: the household's word, not the
    // wire's.
    b.textContent = (typeof modalityLabel === "function")
      ? (modalityLabel(s.modality) || s.modality || s.kind)
      : (s.modality || s.kind);
    var meta = document.createElement("span"); meta.className = "pair-dev-meta";
    meta.textContent = WavrT(" · the whole Space, not a room");
    left.appendChild(b); left.appendChild(meta);
    row.appendChild(left);
    list.appendChild(row);
  });

  tile.hidden = !list.children.length;
}

function buildCoreRow(c, candidateId, fb, onChanged){
  var row = document.createElement("div"); row.className = "pair-dev-row";
  var left = document.createElement("span");
  var name = document.createElement("b"); name.textContent = c.name || c.core_id;
  var meta = document.createElement("span"); meta.className = "pair-dev-meta";
  var bits = [WavrT(c.effective_status)];
  if(c.is_self) bits.push(WavrT("this machine"));
  if(c.portable) bits.push(WavrT("portable"));
  if(c.room) bits.push(c.room);
  if(c.stale && c.status !== "primary") bits.push(WavrT("not checking in"));
  if(c.core_id === candidateId) bits.push(WavrT("recommended next primary"));
  meta.textContent = " · " + bits.join(" · ");
  left.appendChild(name); left.appendChild(meta);
  row.appendChild(left);

  var ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";

  function confirmAction(label, btnClass, warnTxt, path){
    ctl.textContent = "";
    var warn = document.createElement("span"); warn.className = "pair-dev-meta"; warn.textContent = warnTxt;
    var yes = document.createElement("button"); yes.type = "button"; yes.className = btnClass; yes.textContent = WavrT("Confirm");
    var no = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
    no.onclick = function(){ onChanged(); };
    yes.onclick = async function(){
      yes.disabled = true; no.disabled = true;
      var r;
      try{ r = await WavrAPI.fetch(path, {method: "POST"}); }catch(e){}
      if(r && r.ok){ actionFeedback(fb, true, null, WavrT(label === "Promote" ? "✓ promoted" : "✓ demoted")); onChanged(); }
      else {
        var msg = WavrT(label === "Promote" ? "couldn't promote" : "couldn't demote");
        try{ if(r){ var j = await r.json(); if(j && j.detail) msg = String(j.detail); } }catch(e2){}
        actionFeedback(fb, false, msg);
        onChanged();
      }
    };
    ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
  }

  if(c.status !== "primary"){
    var promote = document.createElement("button"); promote.type = "button"; promote.className = "ctl small";
    promote.textContent = WavrT("Promote");
    promote.onclick = function(){
      confirmAction("Promote", "ctl small primary", WavrT("Make {name} authoritative?", {name: c.name || WavrT("this Core")}) + " ",
        "/api/space/cores/" + encodeURIComponent(c.core_id) + "/promote");
    };
    ctl.appendChild(promote);
  } else {
    var demote = document.createElement("button"); demote.type = "button"; demote.className = "ctl small off";
    demote.textContent = WavrT("Demote");
    demote.onclick = function(){
      confirmAction("Demote", "rm", WavrT("Stand {name} down? Nothing else becomes primary automatically.", {name: c.name || WavrT("this Core")}) + " ",
        "/api/space/cores/" + encodeURIComponent(c.core_id) + "/demote");
    };
    ctl.appendChild(demote);
  }

  // Re-home a portable Core. Only offered for one that is actually portable:
  // a desk machine does not move, and a control for something that never
  // happens is noise on the row that matters.
  if(c.portable){
    var rehome = document.createElement("button");
    rehome.type = "button"; rehome.className = "ctl small";
    rehome.textContent = c.room ? WavrT("Change room") : WavrT("Set room");
    rehome.onclick = function(){
      ctl.textContent = "";
      var inp = document.createElement("input");
      inp.type = "text"; inp.maxLength = 64; inp.className = "space-room-inp";
      inp.placeholder = WavrT("which room is it in?");
      inp.value = c.room || "";
      var save = document.createElement("button");
      save.type = "button"; save.className = "ctl small primary";
      save.textContent = WavrT("Save");
      var cancel = document.createElement("button");
      cancel.type = "button"; cancel.className = "ctl small off";
      cancel.textContent = WavrT("Cancel");
      cancel.onclick = function(){ onChanged(); };
      save.onclick = async function(){
        save.disabled = true; cancel.disabled = true;
        var r;
        try{
          r = await WavrAPI.fetch("/api/space/cores/" +
                          encodeURIComponent(c.core_id) + "/room", {method: "POST", json: {room: inp.value}});
        }catch(e){}
        if(r && r.ok){ actionFeedback(fb, true, null, WavrT("\u2713 room saved")); onChanged(); }
        else { actionFeedback(fb, false, WavrT("couldn’t save the room")); onChanged(); }
      };
      ctl.appendChild(inp); ctl.appendChild(save); ctl.appendChild(cancel);
      inp.focus();
    };
    ctl.appendChild(rehome);
  }

  // Forget a Core that is gone for good. Never offered for THIS machine: a Core
  // cannot sensibly forget itself, and the button would only ever return 422.
  if(!c.is_self){
    var forget = document.createElement("button");
    forget.type = "button"; forget.className = "ctl small off";
    forget.textContent = WavrT("Forget");
    forget.onclick = function(){
      ctl.textContent = "";
      var warn = document.createElement("span");
      warn.className = "pair-dev-meta";
      warn.textContent = WavrT("Remove {name} from this Space? Do this only if it is gone for good.",
        {name: c.name || WavrT("this Core")}) + " ";
      var yes = document.createElement("button");
      yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm");
      var no = document.createElement("button");
      no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
      no.onclick = function(){ onChanged(); };
      yes.onclick = async function(){
        yes.disabled = true; no.disabled = true;
        var r;
        try{
          r = await WavrAPI.fetch("/api/space/cores/" +
                          encodeURIComponent(c.core_id), {method: "DELETE"});
        }catch(e){}
        if(r && r.ok){ actionFeedback(fb, true, null, WavrT("\u2713 forgotten")); }
        else {
          var msg = await failureText(r, WavrT("forget it"));
          actionFeedback(fb, false, msg);
        }
        onChanged();
      };
      ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
    };
    ctl.appendChild(forget);
  }

  row.appendChild(ctl);
  return row;
}

async function renderSpace(){
  if(MODE !== "live") return;                 // live-only; never companion or Plano B (demo)
  var note = document.getElementById("spaceNote");
  var spaceTile = document.getElementById("spaceTile");
  var peopleTile = document.getElementById("spacePeopleTile");
  var devicesTile = document.getElementById("spaceDevicesTile");
  var coresTile = document.getElementById("spaceCoresTile");
  if(!note || !spaceTile || !peopleTile || !devicesTile || !coresTile) return;

  var probe;
  try{ probe = await WavrAPI.fetch("/api/space"); }
  catch{ note.hidden = false; note.textContent = WavrT("Couldn't reach the hub — try again in a moment."); return; }
  if(probe.status === 404){
    note.hidden = false;
    note.textContent = WavrT("This Core has no Space yet — finish the setup wizard first.");
    return;
  }
  if(!probe.ok){
    note.hidden = false;
    note.textContent = WavrT("Space management needs local admin access on this hub.");
    return;
  }
  var space; try{ space = await probe.json(); }catch{ space = {}; }
  note.hidden = true;
  spaceTile.hidden = false; peopleTile.hidden = false; devicesTile.hidden = false; coresTile.hidden = false;

  var nameInp = document.getElementById("spaceName");
/* The human name for a Space kind.
 *
 * This was `WavrT(k.charAt(0).toUpperCase() + k.slice(1))` — the API slug with
 * its first letter capitalised. The result is a string no extractor can
 * declare and no catalogue can hold, so every kind but the handful that happen
 * to be spelled in the wizard stayed English forever, and `warehouse` reached
 * a Portuguese screen as "Warehouse".
 *
 * A written-out table instead. The KEYS are the wire values and never change;
 * the values are literals `WavrT` can see. A kind the backend adds and this
 * does not know falls back to the old capitalisation, which is English and
 * readable rather than a blank.
 */
function kindLabel(k) {
  switch (k) {
    case "home": return WavrT("Home");
    case "apartment": return WavrT("Apartment");
    case "office": return WavrT("Office");
    case "shop": return WavrT("Shop");
    case "restaurant": return WavrT("Restaurant");
    case "school": return WavrT("School");
    case "warehouse": return WavrT("Warehouse");
    case "workshop": return WavrT("Workshop");
    case "hotel": return WavrT("Hotel");
    case "laboratory": return WavrT("Laboratory");
    case "other": return WavrT("Other");
    default: return String(k || "").charAt(0).toUpperCase()
                    + String(k || "").slice(1);
  }
}

  var kindSel = document.getElementById("spaceKind");
  var spaceFb = document.getElementById("spaceFb");
  nameInp.value = space.name || "";
  kindSel.textContent = "";
  (space.kinds || []).forEach(function(k){
    var o = document.createElement("option"); o.value = k;
    o.textContent = kindLabel(k);
    if(k === space.kind) o.selected = true;
    kindSel.appendChild(o);
  });
  document.getElementById("spaceForm").onsubmit = async function(e){
    e.preventDefault();
    var btn = e.target.querySelector("button[type=submit]");
    btn.disabled = true;
    var r;
    try{ r = await WavrAPI.fetch("/api/space", {method: "PUT", json: {name: nameInp.value, kind: kindSel.value}}); }catch(e2){}
    btn.disabled = false;
    if(r && r.ok){
      actionFeedback(spaceFb, true);
      var updated; try{ updated = await r.json(); }catch(e3){ updated = null; }
      // One writer (wizard.js showSpaceName): it sets the wordmark AND the
      // window title. Writing the slot here directly is what left a renamed
      // Space showing its old name in the browser tab until a reload.
      if(window.__wavrShowSpace) window.__wavrShowSpace(updated);
    } else {
      var msg = await failureText(r, WavrT("save that"));
      actionFeedback(spaceFb, false, msg);
    }
  };

  var peopleCache = [];

  async function refreshPeople(){
    var listEl = document.getElementById("spacePeopleList");
    var peopleFb = document.getElementById("spacePeopleFb");
    var r; try{ r = await WavrAPI.fetch("/api/space/people"); }catch(e){ return; }
    if(!r.ok) return;
    var j; try{ j = await r.json(); }catch(e){ j = {}; }
    peopleCache = Array.isArray(j.people) ? j.people : [];
    var roles = Array.isArray(j.roles) ? j.roles : ["admin", "user", "guest"];
    listEl.textContent = "";
    if(!peopleCache.length){
      var e2 = document.createElement("div"); e2.className = "empty"; e2.textContent = WavrT("No one added yet.");
      listEl.appendChild(e2);
    } else {
      peopleCache.forEach(function(p){ listEl.appendChild(buildPersonRow(p, roles, peopleFb, refreshPeople)); });
    }
    refreshDevices();      // person names feed the device "person" select below
  }

  var personForm = document.getElementById("spacePersonForm");
  var personFb = document.getElementById("spacePersonFb");
  personForm.onsubmit = async function(e){
    e.preventDefault();
    var fd = new FormData(personForm);
    var btn = personForm.querySelector("button[type=submit]");
    btn.disabled = true;
    var r;
    try{ r = await WavrAPI.fetch("/api/space/people", {method: "POST", json: {display_name: fd.get("display_name"), role: fd.get("role")}}); }catch(e2){}
    btn.disabled = false;
    if(r && r.ok){
      var j; try{ j = await r.json(); }catch(e3){ j = {}; }
      // The endpoint's own preview of what a paired device will get — surfaced here rather
      // than discovered later, since a person's role isn't wired into pairing yet (see
      // space_store.device_role_for_person's own docstring).
      actionFeedback(personFb, true, null, j.device_role
        ? WavrT("✓ added — their next paired device would get {role} access", {role: j.device_role})
        : WavrT("✓ added"));
      personForm.reset();
      refreshPeople();
    } else {
      var msg = WavrT("couldn't add");
      try{ if(r){ var jj = await r.json(); if(jj && jj.detail) msg = String(jj.detail); } }catch(e4){}
      actionFeedback(personFb, false, msg);
    }
  };

  async function refreshDevices(){
    var listEl = document.getElementById("spaceDeviceList");
    var r; try{ r = await WavrAPI.fetch("/api/space/devices"); }catch(e){ return; }
    if(!r.ok) return;
    var j; try{ j = await r.json(); }catch(e){ j = {}; }
    var devs = Array.isArray(j.devices) ? j.devices : [];
    var funcsAvailable = Array.isArray(j.functions_available) ? j.functions_available : [];
    listEl.textContent = "";
    if(!devs.length){
      var e2 = document.createElement("div"); e2.className = "empty"; e2.textContent = WavrT("No paired devices yet.");
      listEl.appendChild(e2);
      return;
    }
    devs.forEach(function(d){ listEl.appendChild(buildDeviceRow(d, peopleCache, funcsAvailable, refreshDevices)); });
  }

  async function refreshCores(){
    var listEl = document.getElementById("spaceCoresList");
    var warnEl = document.getElementById("spaceCoresWarn");
    var coresFb = document.getElementById("spaceCoresFb");
    var r; try{ r = await WavrAPI.fetch("/api/space/cores"); }catch(e){ return; }
    if(!r.ok) return;
    var topo; try{ topo = await r.json(); }catch(e){ topo = {}; }
    if(warnEl) renderCoresWarn(warnEl, topo);
    listEl.textContent = "";
    var cores = Array.isArray(topo.cores) ? topo.cores : [];
    if(!cores.length){
      var e2 = document.createElement("div"); e2.className = "empty"; e2.textContent = WavrT("No Cores registered.");
      listEl.appendChild(e2); return;
    }
    var candidateId = topo.promotion_candidate ? topo.promotion_candidate.core_id : null;
    cores.forEach(function(c){ listEl.appendChild(buildCoreRow(c, candidateId, coresFb, refreshCores)); });
  }

  // ---- Named places (anchors) --------------------------------------------
  // backend/wavr/anchors.py has had a store, and api_anchors.py a full set of
  // admin routes, and all three SDKs a reader — and no screen anywhere could
  // create one. So `context.anchors` was `[]` in every install, `can("anchors")`
  // was false everywhere, and the shipped anchor-demo had nothing to show. This
  // is the writing half. Same idiom as the tiles above: server data reaches the
  // DOM through textContent only; each write re-reads the list rather than
  // patching the row, so what is on screen is always what the Core holds.
  var anchorsTile = document.getElementById("spaceAnchorsTile");
  var anchorRooms = [];
  var enc = encodeURIComponent;

  // The floor plan's rooms, flat, in the same shape housemap.room_names()
  // produces server-side (v2 floors[].rooms[], tolerating a v1 top-level list).
  function roomNamesOf(house){
    var out = [];
    if(house && Array.isArray(house.floors)){
      house.floors.forEach(function(f){ (f.rooms || []).forEach(function(r){ if(r.name) out.push(r.name); }); });
    } else if(house && Array.isArray(house.rooms)){
      house.rooms.forEach(function(r){ if(r.name) out.push(r.name); });
    }
    return out;
  }

  function fillRoomSelect(sel, current){
    sel.textContent = "";
    anchorRooms.forEach(function(name){
      var o = document.createElement("option"); o.value = name; o.textContent = name;
      if(name === current) o.selected = true;
      sel.appendChild(o);
    });
    if(current && anchorRooms.indexOf(current) < 0){
      // An orphaned anchor's room: shown as what it IS, so the operator can
      // see the problem and move it, rather than silently re-homed to the
      // first room in the list.
      var gone = document.createElement("option"); gone.value = current;
      gone.textContent = WavrT("{room} (no longer on the map)", {room: current});
      gone.selected = true; sel.insertBefore(gone, sel.firstChild);
    }
  }

  function buildAnchorRow(a, fb, onChanged){
    var id = enc(a.anchor_id);
    // `anchor-row` on top of `pair-dev-row`: the pairing rows this borrows from
    // carry two controls, and `.pair-dev-ctl{flex-shrink:0}` is right for two.
    // This row carries six, and at a phone width that squeezed the place name
    // down to one character per line and pushed Remove off the screen entirely.
    var row = document.createElement("div"); row.className = "pair-dev-row anchor-row";
    var left = document.createElement("span");
    var name = document.createElement("b"); name.textContent = a.name;
    var meta = document.createElement("span"); meta.className = "pair-dev-meta";
    var where = (a.positioned && typeof a.x === "number")
      ? WavrT("{x} m, {y} m from the corner of {room}", {x: a.x, y: a.y, room: a.room})
      : WavrT("in {room}, no coordinates", {room: a.room});
    meta.textContent = " · " + where + (a.orphaned ? WavrT(" · that room is no longer on the map") : "");
    left.appendChild(name); left.appendChild(meta);
    row.appendChild(left);

    async function send(path, opts, verb){
      var r; try{ r = await WavrAPI.fetch(path, opts); }catch(e){}
      if(r && r.ok){ actionFeedback(fb, true); onChanged(); return; }
      actionFeedback(fb, false, await failureText(r, verb));
    }

    var ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
    // Rename: the input holds the current name and saves when it changes.
    // No prompt(): a browser dialog blocks the page, and the kiosk has no
    // keyboard to dismiss one with.
    var nameInp = document.createElement("input"); nameInp.type = "text"; nameInp.maxLength = 64;
    nameInp.value = a.name; nameInp.setAttribute("aria-label", WavrT("rename {name}", {name: a.name}));
    nameInp.addEventListener("change", function(){
      var v = nameInp.value.trim();
      if(!v || v === a.name){ nameInp.value = a.name; return; }
      send("/api/anchors/" + id + "/name", {method: "POST", json: {name: v}}, WavrT("rename it"));
    });
    ctl.appendChild(nameInp);
    // Room. Moving clears the coordinates — they were metres from the OLD
    // room's corner — and the server's reply says so; the re-read shows it.
    var roomSel = document.createElement("select"); roomSel.className = "pair-dev-role";
    roomSel.setAttribute("aria-label", WavrT("room of {name}", {name: a.name}));
    fillRoomSelect(roomSel, a.room);
    roomSel.addEventListener("change", function(){
      send("/api/anchors/" + id + "/room", {method: "POST", json: {room: roomSel.value}}, WavrT("move it"));
    });
    ctl.appendChild(roomSel);
    // Position: metres from the room's corner. Optional — a place without
    // coordinates is a complete place. The Core refuses a point outside the
    // room, and that refusal is what the feedback span shows.
    // Width comes from `.anchor-row` in the stylesheet, not from an inline
    // style: at a phone width these have to be allowed to reflow with the rest
    // of the row, and a hard `4.5em` in JS wins over the media query that would
    // otherwise let them.
    var xInp = document.createElement("input"); xInp.type = "number"; xInp.step = "0.1"; xInp.min = "0";
    xInp.placeholder = "x"; xInp.setAttribute("aria-label", WavrT("x in metres"));
    var yInp = document.createElement("input"); yInp.type = "number"; yInp.step = "0.1"; yInp.min = "0";
    yInp.placeholder = "y"; yInp.setAttribute("aria-label", WavrT("y in metres"));
    if(typeof a.x === "number"){ xInp.value = a.x; yInp.value = a.y; }
    var setBtn = document.createElement("button"); setBtn.type = "button"; setBtn.className = "ctl small";
    setBtn.textContent = WavrT("Set position");
    setBtn.onclick = function(){
      var x = parseFloat(xInp.value), y = parseFloat(yInp.value);
      if(isNaN(x) || isNaN(y)){ actionFeedback(fb, false, WavrT("both x and y are needed")); return; }
      send("/api/anchors/" + id + "/place", {method: "POST", json: {x: x, y: y}}, WavrT("place it"));
    };
    ctl.appendChild(xInp); ctl.appendChild(yInp); ctl.appendChild(setBtn);
    // Remove: two clicks on the same button, no dialog. A dialog blocks the
    // page and a kiosk has nothing to dismiss one with.
    //
    // It disarms itself. Armed indefinitely, it was a trap rather than a
    // confirmation: the first click could be minutes or a session earlier —
    // Settings closed and reopened in between, the button back to reading
    // "Remove" from a fresh render or not — and the next click deleted.
    var rm = document.createElement("button"); rm.type = "button"; rm.className = "ctl small";
    rm.textContent = WavrT("Remove");
    var armed = false, armTimer = null;
    function disarm(){
      armed = false; rm.textContent = WavrT("Remove");
      clearTimeout(armTimer); armTimer = null;
    }
    rm.onclick = function(){
      if(!armed){
        armed = true; rm.textContent = WavrT("Remove — sure?");
        clearTimeout(armTimer);
        armTimer = setTimeout(disarm, 5000);
        return;
      }
      clearTimeout(armTimer); armTimer = null;
      send("/api/anchors/" + id, {method: "DELETE"}, WavrT("remove it"));
    };
    rm.addEventListener("blur", function(){ if(armed) disarm(); });
    ctl.appendChild(rm);
    row.appendChild(ctl);

    // Links to external systems: the id an AR runtime uses for THIS place. An
    // identity link, never a position claim — anchors.py says why.
    var links = document.createElement("div"); links.className = "pair-dev-row anchor-row";
    var linksLeft = document.createElement("span"); linksLeft.className = "pair-dev-meta";
    (a.mappings || []).forEach(function(m){
      var chip = document.createElement("span"); chip.className = "pill off";
      chip.textContent = m.provider_id + " · " + m.external_id + " ";
      var x = document.createElement("button"); x.type = "button"; x.className = "ctl small";
      x.textContent = "✕"; x.setAttribute("aria-label", WavrT("unlink {id}", {id: m.external_id}));
      x.onclick = function(){
        send("/api/anchors/" + id + "/bind/" + enc(m.provider_id) + "/" + enc(m.external_id),
             {method: "DELETE"}, WavrT("unlink it"));
      };
      chip.appendChild(x); linksLeft.appendChild(chip); linksLeft.appendChild(document.createTextNode(" "));
    });
    links.appendChild(linksLeft);
    var linkCtl = document.createElement("span"); linkCtl.className = "pair-dev-ctl";
    var provInp = document.createElement("input"); provInp.type = "text"; provInp.maxLength = 64;
    provInp.placeholder = WavrT("system, e.g. arkit"); provInp.setAttribute("aria-label", WavrT("external system"));
    var extInp = document.createElement("input"); extInp.type = "text"; extInp.maxLength = 200;
    extInp.placeholder = WavrT("that system's id"); extInp.setAttribute("aria-label", WavrT("that system's id"));
    var linkBtn = document.createElement("button"); linkBtn.type = "button"; linkBtn.className = "ctl small";
    linkBtn.textContent = WavrT("Link");
    linkBtn.onclick = function(){
      var p = provInp.value.trim(), x = extInp.value.trim();
      if(!p || !x){ actionFeedback(fb, false, WavrT("both the system and its id are needed")); return; }
      send("/api/anchors/" + id + "/bind", {method: "POST", json: {provider_id: p, external_id: x}}, WavrT("link it"));
    };
    linkCtl.appendChild(provInp); linkCtl.appendChild(extInp); linkCtl.appendChild(linkBtn);
    links.appendChild(linkCtl);

    var wrap = document.createDocumentFragment();
    wrap.appendChild(row); wrap.appendChild(links);
    return wrap;
  }

  async function refreshAnchors(){
    if(!anchorsTile) return;
    var listEl = document.getElementById("spaceAnchorsList");
    var fbEl = document.getElementById("spaceAnchorsFb");
    var noRooms = document.getElementById("spaceAnchorsNoRooms");
    var form = document.getElementById("spaceAnchorForm");
    var hr; try{ hr = await WavrAPI.fetch("/api/house"); }catch(e){}
    var house = null; if(hr && hr.ok){ try{ house = await hr.json(); }catch(e){} }
    anchorRooms = roomNamesOf(house);
    var r; try{ r = await WavrAPI.fetch("/api/anchors"); }catch(e){ return; }
    if(!r.ok) return;                 // the tile stays hidden: nothing to list, no way to write
    var j; try{ j = await r.json(); }catch(e){ j = {}; }
    anchorsTile.hidden = false;
    fillRoomSelect(form.elements.room, null);
    // A place lives in a room. With no room on the map there is nothing to
    // put one in, and a form that would only ever fail is worse than a note
    // saying what to do first.
    var canAdd = anchorRooms.length > 0;
    noRooms.hidden = canAdd; form.hidden = !canAdd;
    listEl.textContent = "";
    var rows = Array.isArray(j.anchors) ? j.anchors : [];
    if(!rows.length){
      var e2 = document.createElement("div"); e2.className = "empty"; e2.textContent = WavrT("No places named yet.");
      listEl.appendChild(e2); return;
    }
    rows.forEach(function(a){ listEl.appendChild(buildAnchorRow(a, fbEl, refreshAnchors)); });
  }

  var anchorForm = document.getElementById("spaceAnchorForm");
  if(anchorForm) anchorForm.onsubmit = async function(e){
    e.preventDefault();
    var fd = new FormData(anchorForm);
    var anchorFb = document.getElementById("spaceAnchorFb");
    var btn = anchorForm.querySelector("button[type=submit]");
    btn.disabled = true;
    var r;
    try{ r = await WavrAPI.fetch("/api/anchors", {method: "POST", json: {name: fd.get("name"), room: fd.get("room")}}); }catch(e2){}
    btn.disabled = false;
    if(r && r.ok){
      actionFeedback(anchorFb, true, null, WavrT("✓ added"));
      anchorForm.reset();
      refreshAnchors();
    } else {
      actionFeedback(anchorFb, false, await failureText(r, WavrT("add it")));
    }
  };

  refreshPeople();       // also triggers the first refreshDevices() once people are known
  refreshCores();
  refreshAnchors();
  // Last, and independent: coverage reads its own route and hides itself if the
  // Core cannot enumerate its sensors, so a failure here leaves the rest intact.
  renderCoverage();
}
renderSpace();

