// ==========================================================================
// core-settings.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ============================================================================
// Core settings (Settings → Core settings): the screen that replaces editing .env by hand.
// GET /api/settings, PUT /api/settings/{key}, DELETE /api/settings/{key} —
// backend/wavr/settings_store.py. SETTING_SPECS there is the single source of truth: this
// renders every setting the API returns, never a hardcoded list, so a key added to the
// allow-list shows up here with zero frontend change. Loopback-root or an admin-scoped
// device only (require_local + require_scope("admin")), live-only — same gate idiom as
// renderPairing()/renderNodes() above.
// ============================================================================

// Must match settings_store.CONSENT_PHRASE exactly — the store rejects a sensitive write
// without it. Not a secret (anyone who can reach this admin API can read this source), it
// exists so a sensitive flip can never happen as a side effect of a generic "save" click.
var SETTINGS_CONSENT_PHRASE = "i-understand";

// Settings this screen files under the "Advanced" disclosure — the bind address,
// the port, node and peer intake. Anything SETTING_SPECS adds later that isn't
// named here simply stays in the open "Normal" list: visible by default, never
// hidden by omission.
//
// `lan_access` is deliberately NOT in this list any more, and the reason is
// worth keeping. It was filed here as a knob "an ordinary user never needs to
// touch", which is exactly backwards: it is the one switch the website, the
// download page and the first-user script all tell somebody to go and turn on,
// and it is the only door to every phone, tablet and sensor node the product
// has. Collapsed inside "Advanced", the first real user opened Settings, looked
// for it, and wrote "não consegui achar" — the multi-device half of the product
// was unreachable from its own UI while three other surfaces pointed at it.
//
// It stays `sensitive` and `local_only`: it still demands the reveal-then-confirm
// step, and it still refuses to flip from a companion. Being findable and being
// guarded are different properties and it needed both; hiding it only ever
// delivered the first one's opposite.
var SETTINGS_ADVANCED_KEYS = ["bind_host", "port", "nodes_enabled", "peers_enabled"];

// One row per setting. Every control kind funnels a change through `requestChange()`, which
// is the ONLY place that decides whether a reveal-then-confirm step is required —
// `spec.sensitive` is read from the API response, never assumed from the key name, so a
// future sensitive key is protected automatically.
function buildSettingRow(spec){
  var row = document.createElement("div"); row.className = "setting-row";
  // The key, on the element, so anything that needs to POINT AT a setting can
  // find it without matching prose. The pairing panel uses it to walk somebody
  // to `lan_access` instead of offering a second switch of its own: two controls
  // writing the same value eventually disagree, and the guarded one would be the
  // loser.
  row.dataset.key = spec.key || "";
  // What the filter box matches against: the label a person reads, the sentence
  // under it, and the key an operator might know. Stored on the row so filtering
  // never has to walk the DOM guessing which text is meaningful.
  //
  // Translated, because the label on screen is the thing being searched for. The
  // raw key is kept alongside it so `lan_access` still finds "Let other devices
  // connect" for somebody reading a log or this source.
  row.dataset.search = [
    WavrT(spec.label || ""),
    spec.description ? WavrT(spec.description) : "",
    spec.key || ""
  ].join(" ").toLowerCase();
  var top = document.createElement("div"); top.className = "setting-row-top";
  var desc = document.createElement("p"); desc.className = "setting-desc";
  desc.textContent = spec.description ? WavrT(spec.description) : "";   // textContent — still server data
  var note = document.createElement("p"); note.className = "setting-note"; note.hidden = true;
  var confirmBox = null;

  function showNote(text, cls){
    note.hidden = false; note.className = "setting-note" + (cls ? " " + cls : ""); note.textContent = text;
  }
  // `local_only` settings are the ones that take Wavr OFF this machine: LAN
  // access, the bind address, node and peer intake. They are writable only at
  // the Core itself, so on a companion they are read-only for the same reason an
  // env-locked value is -- and get the same grammar. Folding it into
  // `spec.locked` means every control kind (switch, select, input, reset) picks
  // it up without a second flag to remember.
  var lockedHere = MODE === "companion" && !!spec.local_only;
  if(lockedHere) spec.locked = true;

  if(spec.locked){
    showNote(lockedHere
      ? WavrT("This switch decides who else can reach Wavr, so it can only be " +
         "changed on the machine running it \u2014 in front of it.")
      : WavrT("This value comes from this machine's own configuration ({env}) " +
         "and can't be changed here.", {env: spec.env}), "locked");
  }

  async function put(value, consent){
    var body = { value: value };
    if(consent) body.consent = consent;
    var r;
    try{
      r = await WavrAPI.fetch("/api/settings/" + encodeURIComponent(spec.key), {method: "PUT", json: body});
    }catch(e){}
    if(r && r.ok){
      showNote(spec.restart_required ? WavrT("Saved — takes effect the next time Wavr starts.") : WavrT("Saved."));
      return true;
    }
    var msg = await failureText(r, WavrT("save that setting"));
    showNote(msg, "err");
    return false;
  }

  // Sensitive: reveal the setting's OWN description again inside an explicit confirm step
  // (it is written for exactly this) and only PUT on Confirm, carrying the consent phrase.
  // Not sensitive: PUT immediately. Either way resolves to whether the save succeeded, so
  // every control kind can repaint itself (or revert) from one return value.
  function requestChange(value, changeLabel, revert){
    if(spec.locked) return Promise.resolve(false);
    if(!spec.sensitive) return put(value).then(function(ok){ if(!ok && revert) revert(); return ok; });
    if(confirmBox){ confirmBox.remove(); confirmBox = null; }
    confirmBox = document.createElement("div"); confirmBox.className = "setting-confirm";
    var p = document.createElement("p"); p.textContent = WavrT(spec.description);
    var actions = document.createElement("div"); actions.className = "controls-row";
    var yes = document.createElement("button"); yes.type = "button"; yes.className = "ctl small primary";
    yes.textContent = WavrT("Confirm — {change}", {change: changeLabel});
    var no = document.createElement("button"); no.type = "button"; no.className = "ctl small off";
    no.textContent = WavrT("Cancel");
    actions.appendChild(yes); actions.appendChild(no);
    confirmBox.appendChild(p); confirmBox.appendChild(actions);
    row.appendChild(confirmBox);
    return new Promise(function(resolve){
      no.onclick = function(){ confirmBox.remove(); confirmBox = null; if(revert) revert(); resolve(false); };
      yes.onclick = async function(){
        yes.disabled = true; no.disabled = true;
        var ok = await put(value, SETTINGS_CONSENT_PHRASE);
        confirmBox.remove(); confirmBox = null;
        if(!ok && revert) revert();
        resolve(ok);
      };
    });
  }

  // Visible whenever there is a stored override to remove. Starts from the page-load
  // snapshot, but a successful save/reset within THIS session also flips it — otherwise the
  // button would only reflect reality again after a reload, which reads as broken.
  function buildResetBtn(applyDefault){
    if(spec.locked) return null;
    var reset = document.createElement("button");
    reset.type = "button"; reset.className = "ctl small off"; reset.textContent = WavrT("Reset to default");
    reset.hidden = spec.source !== "stored";
    reset.onclick = async function(){
      reset.disabled = true;
      var r;
      try{ r = await WavrAPI.fetch("/api/settings/" + encodeURIComponent(spec.key), {method: "DELETE"}); }catch(e){}
      reset.disabled = false;
      if(r && r.ok){ applyDefault(); reset.hidden = true; showNote(WavrT("Reset to default.")); }
      else showNote(WavrT("couldn't reset — try again"), "err");
    };
    return reset;
  }

  if(spec.kind === "bool"){
    var on = spec.value === "1" || spec.value === true || spec.value === "true";
    var btn = document.createElement("button");
    btn.type = "button"; btn.setAttribute("role", "switch");
    function paint(nowOn){
      on = nowOn;
      btn.className = "ctl switch small " + (on ? "on" : "off");
      btn.setAttribute("aria-checked", on ? "true" : "false");
      btn.textContent = WavrT(on ? "{label}: On" : "{label}: Off", {label: WavrT(spec.label)});
    }
    paint(on);
    btn.disabled = !!spec.locked;
    btn.onclick = function(){
      if(confirmBox) return;                       // a confirm is already open on this row
      var next = !on;
      btn.disabled = true;
      requestChange(next, WavrT(next ? "turn on" : "turn off")).then(function(ok){
        btn.disabled = !!spec.locked;
        if(ok){ paint(next); if(rb1) rb1.hidden = false; }
      });
    };
    top.appendChild(btn);
    var rb1 = buildResetBtn(function(){ paint(spec.default === "1"); });
    if(rb1) top.appendChild(rb1);
  } else if(spec.kind === "choice"){
    var label = document.createElement("label"); label.className = "pair-role";
    label.appendChild(document.createTextNode(WavrT(spec.label)));
    var sel = document.createElement("select"); sel.setAttribute("aria-label", WavrT(spec.label));
    (spec.choices || []).forEach(function(c){
      var o = document.createElement("option"); o.value = c; o.textContent = c;
      if(c === spec.value) o.selected = true;
      sel.appendChild(o);
    });
    sel.disabled = !!spec.locked;
    var prevVal = spec.value;
    sel.onchange = function(){
      var next = sel.value;
      sel.disabled = true;
      requestChange(next, WavrT("set to {value}", {value: next}), function(){ sel.value = prevVal; }).then(function(ok){
        sel.disabled = !!spec.locked;
        if(ok){ prevVal = next; if(rb2) rb2.hidden = false; }
      });
    };
    label.appendChild(sel);
    top.appendChild(label);
    var rb2 = buildResetBtn(function(){ sel.value = spec.default; prevVal = spec.default; });
    if(rb2) top.appendChild(rb2);
  } else {
    // int / float / str: a plain input + an explicit Save — typing never autosubmits.
    var label2 = document.createElement("label"); label2.className = "pair-role";
    label2.appendChild(document.createTextNode(WavrT(spec.label)));
    var inp = document.createElement("input");
    inp.type = (spec.kind === "int" || spec.kind === "float") ? "number" : "text";
    if(spec.kind === "float") inp.step = "any";
    if(spec.minimum != null) inp.min = spec.minimum;
    if(spec.maximum != null) inp.max = spec.maximum;
    if(spec.kind === "str") inp.maxLength = 128;
    inp.value = spec.value;
    inp.disabled = !!spec.locked;
    inp.setAttribute("aria-label", WavrT(spec.label));
    label2.appendChild(inp);
    top.appendChild(label2);
    var saveBtn = document.createElement("button");
    saveBtn.type = "button"; saveBtn.className = "ctl small"; saveBtn.textContent = WavrT("Save");
    saveBtn.disabled = !!spec.locked;
    saveBtn.onclick = function(){
      saveBtn.disabled = true;
      requestChange(inp.value, WavrT("save this value"), function(){}).then(function(ok){
        saveBtn.disabled = !!spec.locked;
        if(ok && rb3) rb3.hidden = false;
      });
    };
    top.appendChild(saveBtn);
    var rb3 = buildResetBtn(function(){ inp.value = spec.default; });
    if(rb3) top.appendChild(rb3);
  }

  row.appendChild(top);
  row.appendChild(desc);
  row.appendChild(note);
  return row;
}

async function renderCoreSettings(){
  if(MODE !== "live") return;                 // live-only; never companion or Plano B (demo)
  var tile = document.getElementById("settingsTile");
  var note = document.getElementById("settingsNote");
  if(!tile || !note) return;
  var probe;
  try{ probe = await WavrAPI.fetch("/api/settings"); }
  catch{ note.hidden = false; note.textContent = WavrT("Couldn't reach the hub — try again in a moment."); return; }
  if(!probe.ok){
    note.hidden = false;
    note.textContent = WavrT("Core settings need local admin access on this hub.");
    return;
  }
  var payload; try{ payload = await probe.json(); }catch{ payload = {}; }
  note.hidden = true;
  tile.hidden = false;

  var restartNote = document.getElementById("settingsRestartNote");
  if(restartNote) restartNote.textContent = payload.restart_required_note ? WavrT(payload.restart_required_note) : "";

  var normalList = document.getElementById("settingsNormalList");
  var advList = document.getElementById("settingsAdvancedList");
  var advDetails = document.getElementById("settingsAdvanced");
  normalList.textContent = ""; advList.textContent = "";
  var settings = Array.isArray(payload.settings) ? payload.settings : [];
  var advancedCount = 0;
  settings.forEach(function(spec){
    var row = buildSettingRow(spec);
    if(SETTINGS_ADVANCED_KEYS.indexOf(spec.key) !== -1){ advList.appendChild(row); advancedCount++; }
    else normalList.appendChild(row);
  });
  if(advDetails) advDetails.hidden = advancedCount === 0;
  wireSettingsFilter();
  try{ window.dispatchEvent(new CustomEvent("wavr:settings-rendered")); }
  catch(e){ /* a browser without CustomEvent is not a target */ }
}

// Find anything in Settings by typing its name.
//
// Asked for twice by the first person to use this screen, after failing to find
// a switch that three other surfaces had told him to go and turn on. The first
// attempt put the box inside the "Core settings" block, which was the wrong
// place for a reason already written in this codebase: at desktop width every
// section renders into one long scrolling column, so a box inside one block is
// invisible to somebody scrolling the other ten. He scrolled straight past it
// and said so.
//
// It filters at two levels, because those are the two things a person means:
// the BLOCK (a tile with a heading) and, inside a block of switches, the ROW.
// Typing "other devices" should leave one block with one row on screen, not
// carry you to it.
//
// Plain substring matching on purpose: no ranking, no fuzziness. A search that
// guesses is worse than one that misses, because you cannot tell which happened.
function wireSettingsFilter(){
  var box = document.getElementById("settingsFilter");
  if(!box) return;
  var empty = document.getElementById("settingsFilterEmpty");
  var overlay = document.getElementById("gearOverlay");
  if(!overlay) return;

  function texto(el){
    // `innerText` is what is rendered, which is what somebody is looking for.
    // `dataset.search` adds the raw key for the rows that carry one, so
    // `lan_access` still finds "Let other devices connect".
    return ((el.dataset && el.dataset.search ? el.dataset.search + " " : "") +
            (el.innerText || "")).toLowerCase();
  }

  // The filter hides things with an attribute of its own and NEVER writes
  // `hidden`. In this overlay `hidden` is the product's word for "this cannot
  // work right now" — `renderPairing()` leaves the PAIR DEVICE panel hidden when
  // other devices are not allowed to connect, because its endpoints are not even
  // mounted — and a search box has no business overruling that.
  //
  // The first version of this function did exactly that. It ran once on wiring,
  // with an empty query, and its `bloco.hidden = !bate` line set `hidden = false`
  // on every tile in the overlay. Opening Settings on a Core with LAN access off
  // therefore produced a complete, confident pairing flow — access levels, "New
  // code now", instructions — against a backend with no `/api/pair-code`. The
  // measurement that found it was looking for something else entirely.
  //
  // An element the product has hidden is not a candidate: it is not shown, not
  // counted, and not touched.
  function esconder(el, some){
    if(some) el.setAttribute("data-filtered", "");
    else el.removeAttribute("data-filtered");
  }

  function apply(){
    var q = (box.value || "").trim().toLowerCase();
    var blocos = overlay.querySelectorAll(".tile");
    var visiveis = 0;

    blocos.forEach(function(bloco){
      if(bloco.hidden){ esconder(bloco, false); return; }
      var linhas = bloco.querySelectorAll(".setting-row");
      var bate = !q || texto(bloco).indexOf(q) !== -1;

      if(linhas.length){
        // A block of switches: filter its rows, and keep the block only if at
        // least one survives. Matching the heading alone would answer a
        // one-word query with fourteen rows.
        var vivas = 0;
        linhas.forEach(function(linha){
          if(linha.hidden){ esconder(linha, false); return; }
          var ok = !q || texto(linha).indexOf(q) !== -1;
          esconder(linha, !ok);
          if(ok) vivas++;
        });
        bate = !q || vivas > 0;
      }
      esconder(bloco, !bate);
      if(bate) visiveis++;
    });

    var VIVO = ":not([hidden]):not([data-filtered])";

    // A collapsed section would hide answers while searching.
    overlay.querySelectorAll("details").forEach(function(d){
      if(q && d.querySelector(".tile" + VIVO + ", .setting-row" + VIVO)){
        d.open = true;
      }
    });

    // A section heading with nothing left under it is a promise of content that
    // was just filtered away.
    overlay.querySelectorAll(".settings-section").forEach(function(sec){
      var temAlgo = !!sec.querySelector(".tile" + VIVO);
      sec.querySelectorAll(":scope > h2, :scope > h3").forEach(function(h){
        esconder(h, !!q && !temAlgo);
      });
    });

    if(empty){
      empty.hidden = !(q && visiveis === 0);
      if(!empty.hidden){
        empty.textContent = WavrT("Nothing in Settings matches \u201c{q}\u201d.",
                                  {q: box.value.trim()});
      }
    }
  }

  if(!box.dataset.wired){
    box.dataset.wired = "1";
    box.addEventListener("input", apply);
    // Several of these panels render asynchronously, so a query typed before
    // one arrives has to be re-applied when it does.
    window.addEventListener("wavr:settings-rendered", apply);
  }
  apply();
}

renderCoreSettings();

