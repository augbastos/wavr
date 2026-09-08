/* Discovery Inbox — lifted verbatim out of index.html.
   A classic script, NOT a module: it calls `actionFeedback`,
   `window.switchTab` and `window.__wavrOpenGearSection`, all defined
   earlier in the document's shared global scope.
 *
 * ## Load position: after the shell chrome it calls into
 *
 * A classic script, not an ES module, and that is load-bearing: it reaches
 * `actionFeedback`, `window.switchTab` and `window.__wavrOpenGearSection`
 * through the global scope every classic script shares. Function hoisting
 * does NOT cross a `<script>` boundary, so a tag moved above `shell-nav.js`
 * breaks the navigation calls at the moment somebody presses the card.
 */

// ============================================================================
// Discoveries tab: the decision inbox (§15 "discover aggressively, activate conservatively"
// made concrete — backend/wavr/discovery_inbox.py). GET /api/discoveries?status=, POST
// .../accept, .../dismiss. The backend decides which actions are renderable per item
// (item.actions, via ACTIONS_FOR_KIND) — this renders ONLY those, never invents a button.
// "dismiss" calls its own endpoint; every OTHER action id routes the admin to the relevant
// existing screen and THEN calls accept — accepting never performs the privileged action
// itself, that stays behind its own already-gated endpoint (adding a camera, approving a
// node, naming a device are separate calls this screen does not invent).
// Loopback-root or an admin-scoped device only (require_local + require_scope("admin")),
// live-only — companion/demo get an honest explanation instead of the list, same convention
// as the New Devices tab just above. Every title/timestamp below is server data ->
// textContent, never innerHTML.
// ============================================================================
(function(){
  "use strict";
  var tile = document.getElementById("discTile");
  if(!tile) return;   // additive: a stale cached page without this markup just no-ops
  var loadingEl = document.getElementById("discLoading");
  var emptyEl = document.getElementById("discEmpty");
  var listEl = document.getElementById("discList");
  var note = document.getElementById("discNote");
  var filterSel = document.getElementById("discFilter");
  var countsEl = document.getElementById("discCounts");
  var navBtn = document.getElementById("tab-discoveries");
  var navCount = document.getElementById("discNavCount");

  // Where each non-"dismiss" action id sends the admin. "acknowledge" has nowhere to route
  // to — it IS the whole decision — so it is deliberately absent and falls through to a
  // plain accept. window.__wavrOpenGearSection is the gear-overlay's own exported hook (see
  // its definition next to openGear()); window.switchTab is the tab router's.
  var ROUTE_FOR_ACTION = {
    // Unused for a real `camera_found` card -- that path is handled above and
    // calls /api/discoveries/{id}/add-camera. Kept for any other kind that ever
    // reuses this action id.
    add_camera:    function(){ if(window.switchTab) window.switchTab("dispositivos"); },
    name:          function(){ if(window.switchTab) window.switchTab("rede"); },
    assign_person: function(){ if(window.__wavrOpenGearSection) window.__wavrOpenGearSection("gearSecSpace"); },
    open_cores:    function(){ if(window.__wavrOpenGearSection) window.__wavrOpenGearSection("gearSecSpace"); },
    // Unused for a real `node_pending` card -- that path is handled above and
    // calls /api/nodes/{id}/approve. Kept for any other kind that ever reuses
    // this action id.
    approve_node:  function(){ if(window.__wavrOpenGearSection) window.__wavrOpenGearSection("gearSecDevices"); },
    pair_core:     function(){ if(window.__wavrOpenGearSection) window.__wavrOpenGearSection("gearSecDevices"); }
  };

  function confBand(c){
    if(c >= 0.8) return WavrT("high confidence");
    if(c >= 0.5) return WavrT("medium confidence");
    return WavrT("low confidence");
  }

  // Sensor archetypes the backend understands (backend/wavr/nodes.py
  // SENSOR_MODALITY). The operator picks one; the node never gets to.
  // A function, not a constant: `WavrT` answers in whatever language is active
  // when it is CALLED, and a list built once at load would still be in the old
  // language after somebody switches. The VALUES are the backend's own
  // identifiers and never translate; only the labels do.
  function sensorTypes(){
    return [
      ["ld2450", WavrT("24GHz radar (HLK-LD2450)")],
      ["mmwave", WavrT("Other mmWave radar")],
      ["pir",    WavrT("Motion sensor (PIR)")],
      ["ble_beacon", WavrT("Bluetooth beacon")],
      ["generic", WavrT("Something else that senses presence")],
      ["environmental", WavrT("Temperature / humidity (not presence)")]
    ];
  }

  // A pending sensor needs a REAL decision, not a card dismissal. Approving one
  // requires a name, a sensor type and a room -- all from the human, because the
  // node is deliberately not allowed to declare them (nodes.py's anti-spoof
  // rule). So the Approve button opens a small form instead of firing a generic
  // accept, and Deny genuinely denies.
  function buildNodeApproval(item, fb, onDone){
    var nodeId = (item.detail && item.detail.node_id) || item.subject;
    var box = document.createElement("div");
    box.className = "tile";
    box.style.marginTop = "10px";

    var hint = document.createElement("p");
    hint.className = "hint";
    // One sentence with two slots, not four fragments concatenated: the hints
    // are the node's own words and stay as it typed them, but the sentence
    // around them is ours to translate whole.
    hint.textContent = WavrT("This sensor says it is \u201c{name}\u201d and has a \u201c{kind}\u201d sensor. Those are its own claims \u2014 what you set below is what Wavr will trust.", {
      name: (item.detail && item.detail.name_hint) || WavrT("unnamed"),
      kind: (item.detail && item.detail.sensor_hint) || WavrT("unknown")
    });
    box.appendChild(hint);

    function field(labelText, el){
      var wrap = document.createElement("label");
      wrap.style.display = "block";
      wrap.style.marginTop = "8px";
      wrap.textContent = labelText;
      wrap.appendChild(el);
      return wrap;
    }

    var name = document.createElement("input");
    name.type = "text"; name.maxLength = 64;
    name.value = (item.detail && item.detail.name_hint) || "";
    name.placeholder = WavrT("e.g. Kitchen radar");

    var kind = document.createElement("select");
    sensorTypes().forEach(function(t){
      var o = document.createElement("option");
      o.value = t[0]; o.textContent = t[1];
      if((item.detail && item.detail.sensor_hint) === t[0]) o.selected = true;
      kind.appendChild(o);
    });

    var room = document.createElement("input");
    room.type = "text"; room.maxLength = 64;
    room.placeholder = WavrT("which room is it in?");

    box.appendChild(field(WavrT("What should Wavr call it?"), name));
    box.appendChild(field(WavrT("What kind of sensor is it?"), kind));
    box.appendChild(field(WavrT("Where is it?"), room));

    var go = document.createElement("button");
    go.type = "button"; go.className = "ctl small primary";
    go.style.marginTop = "10px";
    go.textContent = WavrT("Approve sensor");
    go.onclick = function(){
      if(!name.value.trim() || !room.value.trim()){
        actionFeedback(fb, false, WavrT("a name and a room are required"));
        return;
      }
      go.disabled = true;
      (async function(){
        var ok = false;
        try{
          var r = await WavrAPI.fetch("/api/nodes/" +
                              encodeURIComponent(nodeId) + "/approve", {method: "POST", json: {name: name.value.trim(),
                                  sensor_type: kind.value,
                                  room: room.value.trim()}});
          ok = r.ok;
          if(!ok){
            var body = await r.json().catch(function(){ return {}; });
            actionFeedback(fb, false, body.detail || WavrT("couldn't approve it"));
          }
        }catch(e){ actionFeedback(fb, false, WavrT("couldn't reach Wavr")); }
        if(ok){
          // Not `actionFeedback` + immediate onDone: onDone rebuilds the whole
          // list, so that message would be destroyed before it painted. This
          // replaces the form itself, which lives until the refresh.
          while(box.firstChild) box.removeChild(box.firstChild);
          var done = document.createElement("p");
          done.className = "hint";
          done.textContent = WavrT("\u2713 {name} is approved. It picks up its credential the next time it checks in, so give it a minute before expecting readings.", { name: name.value.trim() });
          box.appendChild(done);
          setTimeout(onDone, 4000);
        } else {
          go.disabled = false;
        }
      })();
    };
    box.appendChild(go);
    return box;
  }

  // A discovered camera needs a room and, almost always, the credentials the
  // owner set on the camera itself. Wavr asks the camera for its stream address
  // over ONVIF, so nobody types a URL -- that is what makes this a product
  // feature rather than a text box.
  function buildCameraAdd(item, fb, onDone){
    var box = document.createElement("div");
    box.className = "tile";
    box.style.marginTop = "10px";

    var d = item.detail || {};
    var where = document.createElement("p");
    where.className = "hint";
    where.textContent = WavrT("Found at {where}. Wavr will ask it for its video address itself.", {
      where: (d.ip || WavrT("an address on your network")) + (d.vendor ? " \u00b7 " + d.vendor : "")
    });
    box.appendChild(where);

    function field(labelText, el){
      var wrap = document.createElement("label");
      wrap.style.display = "block";
      wrap.style.marginTop = "8px";
      wrap.textContent = labelText;
      wrap.appendChild(el);
      return wrap;
    }

    var room = document.createElement("input");
    room.type = "text"; room.maxLength = 64;
    room.placeholder = WavrT("e.g. Hall");

    var user = document.createElement("input");
    user.type = "text"; user.maxLength = 64; user.autocomplete = "off";
    user.placeholder = WavrT("usually admin");

    var pass = document.createElement("input");
    pass.type = "password"; pass.maxLength = 128; pass.autocomplete = "off";

    box.appendChild(field(WavrT("Which room is it in?"), room));
    box.appendChild(field(WavrT("The camera\u2019s own username"), user));
    box.appendChild(field(WavrT("The camera\u2019s own password"), pass));

    var note = document.createElement("p");
    note.className = "hint";
    note.style.marginTop = "8px";
    note.textContent = WavrT("These are the login you set on the camera, not a Wavr account. Wavr uses them to fetch the video address and does not keep them anywhere else. The camera is added switched OFF.");
    box.appendChild(note);

    var go = document.createElement("button");
    go.type = "button"; go.className = "ctl small primary";
    go.style.marginTop = "10px";
    go.textContent = WavrT("Add camera");
    go.onclick = function(){
      if(!room.value.trim()){
        actionFeedback(fb, false, WavrT("a room is required"));
        return;
      }
      go.disabled = true;
      (async function(){
        var body = null, ok = false;
        try{
          var r = await WavrAPI.fetch("/api/discoveries/" +
                              encodeURIComponent(item.discovery_id) + "/add-camera", {method: "POST", json: {room: room.value.trim(),
                                  username: user.value, password: pass.value}});
          body = await r.json().catch(function(){ return {}; });
          ok = r.ok;
        }catch(e){ actionFeedback(fb, false, WavrT("couldn’t reach Wavr")); }

        // Clear the password from the DOM either way: it has served its purpose
        // and there is no reason for it to sit in a form field afterwards.
        pass.value = "";

        if(!ok){
          actionFeedback(fb, false, (body && body.detail) || WavrT("couldn\u2019t add it"));
          go.disabled = false;
          return;
        }
        // The route answers 200 for "it wants credentials" and "wrong password"
        // too -- wanting a login is the normal case, not an error -- so the
        // STATUS decides, not the HTTP code.
        if(body.status !== "added"){
          actionFeedback(fb, false, body.message || WavrT("couldn\u2019t reach the camera"));
          go.disabled = false;
          return;
        }
        while(box.firstChild) box.removeChild(box.firstChild);
        var done = document.createElement("p");
        done.className = "hint";
        done.textContent = WavrT("\u2713 Added to {room}, switched off. Turn it on in Devices when you want it watching.", { room: body.room || room.value.trim() });
        box.appendChild(done);

        // The one thing that lifts this camera from counting people to placing
        // them. Offered here because this is the moment someone is thinking
        // about this camera; the wizard itself already exists.
        var more = document.createElement("p");
        more.className = "hint";
        more.textContent = WavrT("Wavr can also learn WHERE in the room people are, by having you walk to a few spots once. Takes a couple of minutes.");
        box.appendChild(more);
        var calBtn = document.createElement("button");
        calBtn.type = "button"; calBtn.className = "ctl small";
        calBtn.style.marginTop = "8px";
        calBtn.textContent = WavrT("Set up positioning");
        calBtn.onclick = function(){
          window.__wavrCalibrateNext = body.name || null;
          if(window.switchTab) window.switchTab("dispositivos");
          onDone();
        };
        box.appendChild(calBtn);
        setTimeout(onDone, 12000);
      })();
    };
    box.appendChild(go);
    return box;
  }

  function buildActionBtn(item, action, fb, onDone){
    var btn = document.createElement("button"); btn.type = "button";
    btn.className = "ctl small" + (action.kind === "primary" ? " primary" : action.kind === "danger" ? " off" : "");
    btn.textContent = action.label;                    // textContent — server data
    btn.onclick = function(){
      // A pending sensor is a real decision with a real endpoint behind it, not
      // a card to tidy away. Approve opens the form; Deny denies the NODE (the
      // backend clears the card itself, so no /dismiss is needed here).
      // A discovered camera is configured here, not "accepted" and forgotten.
      if(item.kind === "camera_found" && action.id === "add_camera"){
        if(btn.parentNode && !btn.parentNode.querySelector(".cam-add")){
          var camForm = buildCameraAdd(item, fb, onDone);
          camForm.classList.add("cam-add");
          btn.parentNode.appendChild(camForm);
        }
        return;
      }
      if(item.kind === "node_pending"){
        var nodeId = (item.detail && item.detail.node_id) || item.subject;
        if(action.id === "approve_node"){
          if(btn.parentNode && !btn.parentNode.querySelector(".node-approve")){
            var form = buildNodeApproval(item, fb, onDone);
            form.classList.add("node-approve");
            btn.parentNode.appendChild(form);
          }
          return;
        }
        if(action.id === "dismiss"){
          btn.disabled = true;
          (async function(){
            var ok = false;
            try{
              var r = await WavrAPI.fetch("/api/nodes/" +
                                  encodeURIComponent(nodeId) + "/deny", {method: "POST"});
              ok = r.ok;
            }catch(e){}
            if(ok){ onDone(); }
            else {
              btn.disabled = false;
              actionFeedback(fb, false, WavrT("couldn't deny it — try again"));
            }
          })();
          return;
        }
      }
      btn.disabled = true;
      (async function(){
        var path = "/api/discoveries/" + encodeURIComponent(item.discovery_id) +
          (action.id === "dismiss" ? "/dismiss" : "/accept");
        var r;
        try{ r = await WavrAPI.fetch(path, {method: "POST"}); }catch(e){}
        if(r && r.ok){
          if(action.id !== "dismiss"){
            var route = ROUTE_FOR_ACTION[action.id];
            if(route) route();
          }
          onDone();
        } else {
          btn.disabled = false;
          actionFeedback(fb, false, WavrT("couldn't update — try again"));
        }
      })();
    };
    return btn;
  }

  function buildDiscRow(item){
    var row = document.createElement("div"); row.className = "dev-row";
    var left = document.createElement("div"); left.className = "dev-left";

    var nameLine = document.createElement("div"); nameLine.className = "dev-name-line";
    var main = document.createElement("span"); main.className = "dev-main named";
    main.textContent = item.title;                      // textContent — server data
    nameLine.appendChild(main);
    left.appendChild(nameLine);

    var metaLine = document.createElement("div"); metaLine.className = "dev-meta-line";
    var conf = typeof item.confidence === "number" ? item.confidence : 0;
    var confSpan = document.createElement("span"); confSpan.className = "disc-conf";
    // Never rounded up to a yes/no — the number and its band both render, per §16.
    confSpan.textContent = Math.round(conf * 100) + "% (" + confBand(conf) + ")";
    metaLine.appendChild(confSpan);
    metaLine.appendChild(document.createTextNode(" " + MIDDOT + " "));
    var seenSpan = document.createElement("span"); seenSpan.className = "pair-dev-meta";
    seenSpan.textContent = WavrT("first seen {first} \u00b7 last seen {last}", {
      first: fmtRelative(item.first_seen) || WavrT("unknown"),
      last: fmtRelative(item.last_seen) || WavrT("unknown")
    });
    metaLine.appendChild(seenSpan);
    left.appendChild(metaLine);

    if(item.status !== "pending"){
      var statusBadge = document.createElement("span"); statusBadge.className = "pill off";
      statusBadge.textContent = item.status;
      left.appendChild(statusBadge);
    } else {
      var actionsRow = document.createElement("div"); actionsRow.className = "controls-row";
      var fb = document.createElement("span"); fb.className = "action-fb"; fb.setAttribute("aria-live", "polite");
      (item.actions || []).forEach(function(a){
        actionsRow.appendChild(buildActionBtn(item, a, fb, loadDiscoveries));
      });
      actionsRow.appendChild(fb);
      left.appendChild(actionsRow);
    }

    row.appendChild(left);
    return row;
  }

  function paintCounts(counts){
    counts = counts || {};
    if(countsEl){
      countsEl.textContent = WavrT("{pending} pending \u00b7 {accepted} accepted \u00b7 {dismissed} dismissed", {
        pending: counts.pending || 0,
        accepted: counts.accepted || 0,
        dismissed: counts.dismissed || 0
      });
    }
    var n = counts.pending || 0;
    if(navCount){ navCount.hidden = n === 0; navCount.textContent = n > 99 ? "99+" : String(n); }
    if(navBtn) navBtn.setAttribute("aria-label",
      n ? WavrT("Discoveries — {n} pending", { n: n }) : WavrT("Discoveries"));
  }

  async function loadDiscoveries(){
    if(MODE !== "live"){
      loadingEl.hidden = true;
      note.hidden = false;
      note.textContent = (MODE === "companion")
        ? WavrT("Discoveries need the hub itself — not available on a view-only companion device.")
        : WavrT("Discoveries are found by your Space's Wavr hub; this demo doesn't access any network.");
      return;
    }
    var status = filterSel ? filterSel.value : "pending";
    var r;
    try{
      r = await WavrAPI.fetch("/api/discoveries?status=" + encodeURIComponent(status));
    }catch(e){
      loadingEl.hidden = true; note.hidden = false;
      note.textContent = WavrT("Couldn't reach the hub — try again in a moment.");
      return;
    }
    loadingEl.hidden = true;
    if(!r.ok){
      note.hidden = false;
      note.textContent = WavrT("Discoveries need local admin access on this hub.");
      return;
    }
    note.hidden = true;
    var j; try{ j = await r.json(); }catch(e){ j = {}; }
    var items = Array.isArray(j.discoveries) ? j.discoveries : [];
    paintCounts(j.counts);
    listEl.textContent = "";
    if(!items.length){
      emptyEl.hidden = false;
      // The filter offers exactly four values and the first branch takes two of
      // them, so "accepted" and "dismissed" are the whole of the second. Spelt
      // out rather than interpolated, because a status word dropped into an
      // English sentence would stay English inside a Portuguese one.
      emptyEl.textContent = (status === "pending" || status === "all")
        ? WavrT("Nothing needs your attention.")
        : (status === "dismissed"
            ? WavrT("No dismissed discoveries yet.")
            : WavrT("No accepted discoveries yet."));
      return;
    }
    emptyEl.hidden = true;
    var frag = document.createDocumentFragment();
    items.forEach(function(it){ frag.appendChild(buildDiscRow(it)); });
    listEl.appendChild(frag);
  }

  if(filterSel) filterSel.addEventListener("change", loadDiscoveries);
  loadDiscoveries();
  // Keeps the nav badge accurate even while the admin is on a different tab — the counts
  // field is independent of the selected filter (DiscoveryInbox.counts() always returns all
  // three), so polling here (rather than only on tab-visit) is what makes the badge honest.
  if(MODE === "live") setInterval(loadDiscoveries, 20000);
})();
