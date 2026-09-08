// ==========================================================================
// new-devices.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ============================================================================
// New devices (feature #3): a calm, layperson-facing answer to "is there
// anything on my Wi-Fi I don't recognize?" for a non-technical user. Zero new
// fetch/poll of its own — chains onto window.__wavrInventory, the SAME
// /api/inventory payload renderNetwork()'s own 15s poll already feeds to the
// Stage-2 "Detected" hardware-catalog hook and the Stage-3 dev-list
// search/profile-reattach hook (this is a THIRD chain onto that identical
// one-liner idiom — see window.__wavrInventory near "Stage-2 hook" above).
// Filters to known===false, sorted most-recent-first (last_seen, falling back
// to first_seen). Per-row actions call the SAME backend routes the Network
// tab's own row controls already use:
//   - "That's mine"       -> POST /api/inventory/known {mac, known:true},
//                             byte-identical body to renderNetwork()'s own
//                             "this is mine"/knownBtn control.
//   - "Block this device" -> POST /api/block {mac, action:"block",
//                             confirm:true}. Disruptive AND loopback-root
//                             only (app.py's require_root rejects even a
//                             paired multidevice 'central' peer), so the
//                             control only ever renders in MODE==="live", and
//                             is gated behind a reveal-then-confirm row —
//                             never a native confirm() (matches the Unpair
//                             pattern elsewhere in this file). A 503
//                             (WAVR_NET_BLOCKING off, or no elevated
//                             raw-socket/npcap transport on this host) is
//                             surfaced honestly, in plain language — never a
//                             silent no-op.
// Honesty gate: the reassuring "everything is recognized" copy only ever
// paints once a real, non-empty /api/inventory payload has actually landed —
// a totally empty inventory (the opt-in scan not running) gets its own
// distinct "not detected yet" copy, and demo mode gets an explicit "this
// demo doesn't access any network" note instead of either claim.
// ============================================================================
(function(){
  "use strict";
  var tile = document.getElementById("novTile");
  if(!tile) return;   // additive: a stale cached page without this markup just no-ops
  var loadingEl = document.getElementById("novLoading");
  var emptyEl = document.getElementById("novEmpty");
  var listEl = document.getElementById("novList");
  var note = document.getElementById("novNote");

  var allDevices = [];

  function novLabel(d){
    // Same "most human identity first" precedence renderNetwork()'s own
    // resolvedIdentity uses (name -> display_name -> hostname), extended
    // with make/model/vendor per this tab's spec, "Unknown device" last —
    // never a raw MAC/IP as the headline.
    if(d.name) return String(d.name);
    if(d.display_name) return String(d.display_name);
    if(d.hostname) return String(d.hostname);
    if(d.make && d.model) return String(d.make) + " " + String(d.model);
    if(d.make) return String(d.make);
    if(d.vendor) return String(d.vendor);
    return WavrT("Unknown device");
  }
  // Whether novLabel() had to fall back. Asked as a question about the DATA rather than by
  // comparing the returned label against "Unknown device" — that comparison stops matching
  // the moment the label is translated, and would silently print the fallback twice.
  function novUnnamed(d){
    return !(d.name || d.display_name || d.hostname || d.make || d.vendor);
  }
  function novTs(d){
    var t = Date.parse(d.last_seen || d.first_seen || "");
    return isNaN(t) ? 0 : t;
  }

  function buildRow(d){
    var row = document.createElement("div"); row.className = "dev-row";
    // Audit M1 invariant carried over from renderNetwork(): every row here IS
    // known===false by construction, so the type icon/label ALWAYS renders
    // capped/"guessed" (rogue=true) — never the solid confirmed-fact treatment.
    row.appendChild(dtypeIconEl(d.device_type, d.type_confidence, false, true));
    var left = document.createElement("div"); left.className = "dev-left";

    var label = novLabel(d);
    var hasRealIdentity = !!(d.name || d.display_name || d.hostname);
    var nameLine = document.createElement("div"); nameLine.className = "dev-name-line";
    var main = document.createElement("span");
    if(hasRealIdentity){
      main.className = "dev-main named"; main.textContent = label;   // textContent only — device data is untrusted
      var sub = document.createElement("span"); sub.className = "dev-sub";
      sub.textContent = " · " + (d.vendor || WavrT("unknown vendor")) + " · ";
      sub.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, true));
      nameLine.appendChild(main); nameLine.appendChild(sub);
    } else {
      main.className = "dev-main";
      main.textContent = novUnnamed(d) ? "" : (label + " · ");
      main.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, true));
      nameLine.appendChild(main);
    }
    left.appendChild(nameLine);

    var metaLine = document.createElement("div"); metaLine.className = "dev-meta-line";
    var firstRel = fmtRelative(d.first_seen);
    var meta = document.createElement("span"); meta.className = "dev-meta";
    meta.textContent = (firstRel ? WavrT("first seen {when}", {when: firstRel})
                                 : WavrT("first seen unknown")) + " " + MIDDOT + " ";
    metaLine.appendChild(meta);
    // MAC rides small/secondary at most — masked, never the headline (matches maskMac()'s
    // existing privacy convention: vendor OUI visible, device-specific half hidden).
    var macEl = document.createElement("span"); macEl.className = "mac";
    macEl.textContent = maskMac(d.mac);
    metaLine.appendChild(macEl);
    left.appendChild(metaLine);

    if(MODE === "live" && d.mac){
      var mineBtn = document.createElement("button");
      mineBtn.type = "button"; mineBtn.className = "dev-rename-btn dev-known-btn";
      mineBtn.textContent = WavrT("That's mine");
      mineBtn.setAttribute("aria-label", WavrT("mark {what} as known", {what: label}));
      var mineFb = document.createElement("span"); mineFb.className = "action-fb";
      var mineWrap = document.createElement("span"); mineWrap.className = "dev-rename-form";
      mineWrap.appendChild(mineBtn); mineWrap.appendChild(mineFb);
      left.appendChild(mineWrap);

      var blockCtl = document.createElement("span"); blockCtl.className = "dev-rename-form";
      var blockBtn = document.createElement("button");
      blockBtn.type = "button"; blockBtn.className = "dev-rename-btn dev-block-btn";
      blockBtn.textContent = WavrT("Block this device");
      blockBtn.setAttribute("aria-label", WavrT("block {what} from the Wi-Fi", {what: label}));
      var blockFb = document.createElement("span"); blockFb.className = "action-fb";
      var confirmWrap = null;
      blockCtl.appendChild(blockBtn); blockCtl.appendChild(blockFb);
      left.appendChild(blockCtl);

      mineBtn.onclick = function(){
        mineBtn.disabled = true; blockBtn.disabled = true;
        (async function(){
          var r;
          try{
            r = await WavrAPI.fetch("/api/inventory/known", {method: "POST", json: {mac: d.mac, known: true}});
          }catch(e){}
          if(r && r.ok){
            // Optimistic: the row drops off immediately (this IS the success feedback,
            // per spec) — then a manual refresh (no new poll, the SAME __wavrNetRefresh
            // hook renderNetwork() already exposes) keeps the Network tab/bulk-trust
            // count in sync right away instead of waiting up to 15s.
            allDevices = allDevices.filter(function(x){ return x.mac !== d.mac; });
            row.remove();
            if(!listEl.children.length) emptyEl.hidden = false;
            if(window.__wavrNetRefresh) window.__wavrNetRefresh();
          } else {
            var msg = await failureText(r, WavrT("update that device"));
            actionFeedback(mineFb, false, msg);
            mineBtn.disabled = false; blockBtn.disabled = false;
          }
        })();
      };

      // Two-step reveal-then-confirm: the first click swaps the button for a
      // Confirm/Cancel pair; only Confirm sends the disruptive POST. Never a
      // native confirm() (matches the Unpair pattern elsewhere in this file).
      // The row is edited IN PLACE (never a full list rebuild) so a
      // success/error message next to THIS device survives — a full
      // renderList() would otherwise destroy the very feedback node it just set.
      blockBtn.onclick = function(){
        if(confirmWrap) return;
        blockBtn.hidden = true;
        confirmWrap = document.createElement("span"); confirmWrap.className = "dev-rename-form";
        var warn = document.createElement("span"); warn.className = "nov-block-warn";
        warn.textContent = WavrT("Block {what}? It loses Wi-Fi access on this network immediately.", {what: label});
        var yes = document.createElement("button"); yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm block");
        var no = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
        no.onclick = function(){ confirmWrap.remove(); confirmWrap = null; blockBtn.hidden = false; };
        yes.onclick = function(){
          yes.disabled = true; no.disabled = true;
          (async function(){
            var r;
            try{
              r = await WavrAPI.fetch("/api/block", {method: "POST", json: {mac: d.mac, action: "block", confirm: true}});
            }catch(e){}
            confirmWrap.remove(); confirmWrap = null; blockBtn.hidden = false;
            if(r && r.ok){
              actionFeedback(blockFb, true, null, WavrT("✓ blocked — no Wi-Fi access now"));
            } else if(r && r.status === 503){
              // Honest 503: never pretend the block worked. Plain-language — the toggle
              // lives on the hub (WAVR_NET_BLOCKING), never a control on this screen.
              actionFeedback(blockFb, false,
                WavrT("Blocking isn't turned on for this hub — it's enabled by whoever manages the hub, not from this screen."));
            } else {
              var msg = await failureText(r, WavrT("block this device"));
              actionFeedback(blockFb, false, msg);
            }
          })();
        };
        confirmWrap.appendChild(warn); confirmWrap.appendChild(yes); confirmWrap.appendChild(no);
        blockCtl.appendChild(confirmWrap);
      };
    } else if(d.mac){
      // Companion (view-only, paired token): no X-Wavr-Local CSRF header, and /api/block is
      // loopback-root only regardless — same live-only gate renderNetwork()'s own
      // rename/known-toggle/assign-person row controls already use.
      note.hidden = false;
      note.textContent = WavrT("Marking a device as yours or blocking it needs the hub — not available in this demo or on a view-only companion device.");
    }

    row.appendChild(left);
    return row;
  }

  function renderList(){
    listEl.textContent = "";
    loadingEl.hidden = true;
    note.hidden = true;   // re-computed per row below (companion-only) so a stale note never lingers after a refresh
    if(!allDevices.length){
      // Distinct from "everything recognized": the opt-in inventory scan may simply not be
      // running yet (see renderNetwork()'s own #netHint, same honest wording).
      emptyEl.hidden = true;
      var l0 = document.createElement("p"); l0.className = "empty";
      l0.textContent = WavrT("No devices detected on the network yet.");
      listEl.appendChild(l0);
      return;
    }
    var unknown = allDevices.filter(function(d){ return d.known === false; })
      .sort(function(a, b){ return novTs(b) - novTs(a); });
    if(!unknown.length){
      emptyEl.hidden = false;
      return;
    }
    emptyEl.hidden = true;
    var frag = document.createDocumentFragment();
    unknown.forEach(function(d){ frag.appendChild(buildRow(d)); });
    listEl.appendChild(frag);
  }

  if(MODE === "simulated"){
    // Illustrative-only mode never reaches window.__wavrInventory (renderNetwork() itself
    // returns before firing it) — say so honestly instead of hanging on "Loading…" forever.
    loadingEl.hidden = true;
    note.hidden = false;
    note.textContent = WavrT("New devices are found by your Space's Wavr hub; this demo doesn't access any network.");
  }

  var _inv = window.__wavrInventory;
  window.__wavrInventory = function(devs){
    try{ if(_inv) _inv(devs); }catch(e){}
    if(MODE !== "live" && MODE !== "companion") return;   // demo never reaches this hook anyway
    allDevices = Array.isArray(devs) ? devs : [];
    renderList();
  };
})();
