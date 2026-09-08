// ==========================================================================
// nodes.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Nodes — headless sensor boxes (live/central only, nodes-gated) ----
// A node PUSHES presence telemetry over the LAN using its OWN per-node bearer token — it is
// NOT a companion device (DeviceStore) and NOT a peer hub (PeerStore). The /api/nodes* routes
// only exist when the nodes feature is on; when GET /api/nodes 404s (or the backend is
// momentarily unreachable) the whole panel stays hidden — same probe-gate as Peers/My devices.
// REMOTE-OFF-NEVER-ON (kill-switch invariant): Disable is remote-reachable; there is NO Enable
// control anywhere in this panel and NO enable endpoint — a disabled node can only be
// re-enabled by a physical button press on the device itself, which this UI cannot do and does
// not pretend to. Every node field (name/room/modality/transport/state/timestamp) is server
// data rendered via textContent — NEVER innerHTML.
async function renderNodes(){
  if(MODE!=="live") return;                 // live/central only; never Plano B (demo) or companion
  if(await _wavrMultideviceOff) return;     // proven off via /api/status — skip the doomed probe (no console 404)
  let probe;
  try{ probe = await WavrAPI.fetch("/api/nodes"); }
  catch{ return; }                           // backend unreachable — stay hidden
  if(!probe.ok) return;                      // 404 → nodes feature off → stay hidden
  let seed; try{ seed = (await probe.json()).nodes; }catch{ seed = []; }
  document.getElementById("nodes").hidden = false;

  const list    = document.getElementById("nodeList");
  const fb      = document.getElementById("nodesFb");
  const addFb   = document.getElementById("nodeAddFb");
  const codeBox = document.getElementById("nodeCodeBox");
  const codeEl  = document.getElementById("nodeCode");

  // Chip colour per kill-switch state — the design tokens, not an ad-hoc palette.
  const NODE_STATE_COLOR = {active:"#3db54a", disabled:"#e8a13a", revoked:"#9AA4AD"};

  function renderList(nodesArr){
    const arr = Array.isArray(nodesArr) ? nodesArr : [];
    list.textContent = "";
    if(!arr.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("No nodes enrolled yet");
      list.appendChild(e); return;
    }
    arr.forEach(n => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = n.name || WavrT("(unnamed node)"); // textContent — server data
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      const room = n.room != null ? String(n.room) : "";
      // The word the rest of the product uses for this sensor. Raw, this line
      // put `pir` in the node list while the room card called the same sensor
      // "Motion sensor (PIR)". Guarded: `modalityLabel` is a global out of
      // radar.js, which loads after this file.
      const modRaw = n.modality != null ? String(n.modality) : "";
      const modality = (modRaw && typeof modalityLabel === "function")
        ? (modalityLabel(modRaw) || modRaw) : modRaw;
      const transport = n.transport != null ? String(n.transport) : "";
      const seenRel = fmtRelative(n.last_seen_ts);
      meta.textContent = " · " + [room, modality, transport].filter(Boolean).join(" · ")
        + (seenRel ? " · " + WavrT("last seen {when}", {when: seenRel}) : WavrT(" · never seen"));  // textContent — server data
      left.appendChild(name); left.appendChild(meta);
      const chip = document.createElement("span"); chip.className = "node-chip";
      const state = n.state != null ? String(n.state) : "";
      chip.textContent = WavrT(state);                                      // textContent — never innerHTML
      chip.style.color = NODE_STATE_COLOR[state] || "var(--dim)";
      left.appendChild(chip);
      row.appendChild(left);

      const ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
      if(state === "disabled"){
        // KILL-SWITCH INVARIANT (remote-OFF-never-ON): there is deliberately NO Enable
        // button anywhere in this panel. Re-enabling requires a physical button press on
        // the node itself — the absence of a remote control IS the invariant.
        const note = document.createElement("span"); note.className = "pair-dev-meta";
        note.textContent = WavrT("Press the button on the device to re-enable");
        ctl.appendChild(note);
      } else if(state === "active"){
        const disableBtn = document.createElement("button"); disableBtn.type = "button";
        disableBtn.className = "ctl small"; disableBtn.textContent = WavrT("Disable");
        disableBtn.setAttribute("aria-label", WavrT("disable {name}", {name: n.name || WavrT("node")}));
        disableBtn.onclick = async ()=>{
          disableBtn.disabled = true;
          let r;
          try{ r = await WavrAPI.fetch("/api/nodes/"+encodeURIComponent(n.node_id)+"/disable", {method: "POST"}); }catch{}
          if(r && r.ok){ actionFeedback(fb, true, null, WavrT("✓ disabled")); refreshNodes(); }
          else { actionFeedback(fb, false, WavrT("couldn't disable")); disableBtn.disabled = false; }
        };
        ctl.appendChild(disableBtn);
      }
      if(state !== "revoked"){
        // Two-step reveal-then-confirm — never a native confirm(), mirrors the Peers "Unpair"
        // affordance: the first click swaps in a Confirm/Cancel pair; only Confirm DELETEs.
        const rmBtn = document.createElement("button"); rmBtn.type = "button"; rmBtn.className = "rm";
        rmBtn.textContent = WavrT("Remove");
        rmBtn.setAttribute("aria-label", WavrT("remove {name}", {name: n.name || WavrT("node")}));
        rmBtn.onclick = ()=>{
          ctl.textContent = "";
          const warn = document.createElement("span"); warn.className = "pair-dev-meta"; warn.textContent = WavrT("Remove? ");
          const yes = document.createElement("button"); yes.type = "button"; yes.className = "rm"; yes.textContent = WavrT("Confirm");
          const no  = document.createElement("button"); no.type = "button"; no.className = "ctl small off"; no.textContent = WavrT("Cancel");
          no.onclick = ()=>{ renderList(arr); };
          yes.onclick = async ()=>{
            yes.disabled = true; no.disabled = true;
            let r;
            try{ r = await WavrAPI.fetch("/api/nodes/"+encodeURIComponent(n.node_id), {method: "DELETE"}); }catch{}
            if(r && r.ok){ actionFeedback(fb, true, null, WavrT("✓ removed")); refreshNodes(); }
            else { actionFeedback(fb, false, WavrT("couldn't remove")); renderList(arr); }
          };
          ctl.appendChild(warn); ctl.appendChild(yes); ctl.appendChild(no);
        };
        ctl.appendChild(rmBtn);
      }
      row.appendChild(ctl);
      list.appendChild(row);
    });
  }

  async function refreshNodes(){
    let r; try{ r = await WavrAPI.fetch("/api/nodes"); }catch{ return; }
    if(!r.ok) return;
    let j; try{ j = (await r.json()).nodes; }catch{ return; }
    renderList(j);
  }

  document.getElementById("nodeAddForm").onsubmit = async (e)=>{
    e.preventDefault();
    const f = e.target;
    const body = {
      name: f.name.value.trim(),
      sensor_type: f.sensor_type.value,
      room: f.room.value.trim(),
      transport: f.transport.value,
    };
    const submitBtn = f.querySelector('button[type="submit"]');
    if(submitBtn) submitBtn.disabled = true;
    let r;
    try{
      r = await WavrAPI.fetch("/api/nodes/enroll-code", {method: "POST", json: body});
    }catch{}
    if(submitBtn) submitBtn.disabled = false;
    if(r && r.ok){
      let j; try{ j = await r.json(); }catch{ j = {}; }
      codeEl.textContent = (j && typeof j.code==="string") ? j.code : "—";   // textContent — server data
      codeBox.hidden = false;
      actionFeedback(addFb, true, null, WavrT("✓ code minted"));
      f.reset();
    } else {
      let detail=""; try{ detail=(await r.json()).detail||""; }catch{}
      actionFeedback(addFb, false, detail || WavrT("couldn't mint a code"));
    }
  };

  renderList(seed);                            // seed the list from the probe we already read
}
renderNodes();

