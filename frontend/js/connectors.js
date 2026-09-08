// ==========================================================================
// connectors.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Connectors & services: the single outward-facing surface ----
// (project_wavr_connectors_vision) Wavr is local and MUTE by default; this is the ONLY screen
// that manages anything reaching outward, and everything is OFF by default. Live/central only:
// GET /api/connectors is router require_central (a multidevice 'user' peer 403s -> panel stays
// hidden, like the identity registry); the enable toggle adds require_local (X-Wavr-Local CSRF).
// Descriptor shape from the backend: {id,kind,direction,label,available,active,suppressed,
// override,env_active,needs,enforcement,scope,env_flag}. Enforcement decides the control:
//   * 'registry-overlay' (built-in) -> a REAL On/Off toggle that PERSISTS an admin override.
//     `override` ("on"/"off"/null) is the persisted choice; the switch is on iff override=="on"
//     OR (no override AND env_active) -- i.e. the env flag turned it on. Turning it off writes a
//     kill-switch; turning it on enables the gate even with the env flag unset. `active` is the
//     HONEST truth (gate-on AND actually able to run); `needs` ("restart"/"config"/null) says why
//     an enabled connector is not live yet, so the UI never fakes "live".
//   * 'env' (bound in a separate process, e.g. MCP control) -> NO live toggle; the card reflects
//     the flag + names the env var to edit, rather than faking a dead switch.
//   * 'registry' (generic connector) -> the registry IS the full gate: "On"/"Off" flips it.
// Every label/scope is server/registry data -> textContent, never innerHTML. An absent registry
// row => all built-ins render off/ready => nothing egresses (byte-identical to today).
// A FUNCTION, not a constant.
//
// These reach the screen through `WavrT(CONN_DESC[id])`, so as a module-level
// table their literals were invisible to the key extractor: looked up at
// runtime, declared nowhere, therefore untranslatable — and nothing could
// report the gap, because a missed lookup falls back to correct English.
// Built per call, the literals sit inside `WavrT(...)` where they can be seen,
// and the table is rebuilt in the current language rather than frozen in
// whichever one was active when this file parsed.
function connDesc() {
  return {
    "narrator": WavrT("Turns live presence into a short natural-language summary with an LLM."),
    "ha-import": WavrT("Imports device and area names from Home Assistant on your LAN."),
    "ha-control": WavrT("Lets Wavr send control commands to your Home Assistant devices."),
    "mcp-read": WavrT("Exposes a read-only view of the Space (RoomState) to AI agents over MCP."),
    "mcp-http": WavrT("Exposes a read-only view of the Space (RoomState) and Home Assistant entity names to AI agents over HTTPS — paired and cert-pinned, LAN only."),
  };
}
async function renderConnectors(){
  if(MODE!=="live") return;                  // live/central only; never companion/demo
  let probe;
  try{ probe = await WavrAPI.fetch("/api/connectors"); }
  catch{ return; }                           // backend unreachable — stay hidden
  if(!probe.ok) return;                      // 403 (multidevice 'user') — stay hidden
  let list; try{ list = (await probe.json()).connectors; }catch{ list = []; }
  document.getElementById("connectors").hidden = false;

  const host = document.getElementById("connList");
  const summary = document.getElementById("connSummary");
  const fb = document.getElementById("connFb");

  function directionWord(dir){ return WavrT(dir === "inbound" ? "Reads in" : "Sends out"); }
  // Egress honesty: an outbound connector whose scope does NOT start with "local" leaves the box.
  function isEgress(c){ return c.direction === "outbound" && !/^local/i.test(c.scope || ""); }

  // Fix #4 (bare connector cards): never render a card with no "what this does" line.
  // Prefers a server-provided plain-language `description` field if the descriptor ever
  // carries one (it doesn't today, per the shape comment above — this is forward-compatible,
  // not an assumed backend change), falls back to the known-id map, and as a last resort
  // builds an honest generic sentence from the label + direction so an id outside CONN_DESC
  // (e.g. a future generic connector) is never left unexplained.
  function connDescription(c){
    if(typeof c.description === "string" && c.description.trim()) return WavrT(c.description.trim());
    var _d = connDesc(); if(_d[c.id]) return _d[c.id];
    return WavrT(c.direction === "inbound"
      ? "{name} reads data into Wavr from your network."
      : "{name} can send data out from Wavr.", {name: c.label || c.id});
  }

  function stateFor(c){                       // honest per-enforcement status chip
    if(c.suppressed) return {word:"revoked", cls:"revoked"};
    if(c.active)     return {word:"active",  cls:"active"};
    if(c.enforcement === "env" && !c.env_flag) return {word:"read-only · separate MCP server", cls:""};
    // Enabled by the admin but not live yet — say so honestly, never fake "active".
    if(c.needs === "restart") return {word:"enabled · needs a hub restart", cls:"setup"};
    if(c.needs === "config")  return {word:"enabled · needs setup", cls:"setup"};
    if(!c.available) return {word:"needs setup", cls:"setup"};
    return {word:"ready", cls:""};            // configured, but its enable toggle is still off
  }
  // Honest one-line note for an enabled-but-not-live connector (behind the switch).
  function needsNote(c){
    if(c.needs === "restart"){
      return c.id === "mcp-http"
        ? WavrT("Enabled — restart the hub with multi-device on to activate.")
        : WavrT("Enabled — restart the hub to activate.");
    }
    if(c.needs === "config"){
      if(c.id === "narrator") return WavrT("Enabled — add a provider API key, then restart the hub.");
      if(c.id === "ha-import") return WavrT("Enabled — set WAVR_HA_URL + WAVR_HA_TOKEN in the hub config.");
      return WavrT("Enabled — finish configuring it to go live.");
    }
    return "";
  }

  function buildCard(connectors, c){
    const NS = "http://www.w3.org/2000/svg";
    const card = document.createElement("div"); card.className = "conn-card";

    const top = document.createElement("div"); top.className = "conn-top";
    const nm = document.createElement("h4"); nm.className = "conn-name";
    nm.textContent = c.label || c.id;        // textContent — server/registry data (never innerHTML)
    top.appendChild(nm);
    // (should | Connectors | 7): the raw inbound/outbound jargon pill is dropped here —
    // directionWord() below is the single plain-language direction indicator.
    card.appendChild(top);

    const descP = document.createElement("p"); descP.className = "conn-desc";
    descP.textContent = connDescription(c);   // Fix #4 — guaranteed, never skipped
    card.appendChild(descP);

    // Must | Connectors | 7: explicit words+icon egress badge — replaces the color-only
    // .conn-scope.egress tint so privacy is legible without relying on color vision.
    const egress = isEgress(c);
    const badge = document.createElement("span");
    badge.className = "conn-egress-badge " + (egress ? "leaves" : "stays");
    const bsvg = document.createElementNS(NS, "svg"); bsvg.setAttribute("aria-hidden", "true");
    // #ic-egress = the UNIVERSAL "this leaves your home" pictogram (see the sprite comment).
    const buse = document.createElementNS(NS, "use"); buse.setAttribute("href", egress ? "#ic-egress" : "#ic-lock");
    bsvg.appendChild(buse); badge.appendChild(bsvg);
    badge.appendChild(document.createTextNode(egress ? WavrT("Leaves your network") : WavrT("Stays on your network")));
    card.appendChild(badge);

    if(c.scope){
      const sc = document.createElement("p"); sc.className = "conn-scope";
      const b = document.createElement("b"); b.textContent = directionWord(c.direction) + ": ";
      sc.appendChild(b); sc.appendChild(document.createTextNode(c.scope)); // textContent path — hostile scope is data
      card.appendChild(sc);
    }

    const foot = document.createElement("div"); foot.className = "conn-foot";
    const st = stateFor(c);
    if(c.enforcement === "env"){
      // No live toggle: the gate lives in a separate process/flag. State chip stays honest
      // about the current value; must | Connectors | 3: a calm "managed elsewhere" chip
      // (expected behavior, not a fault) with the exact env var + restart instruction
      // demoted behind a Details disclosure instead of primary card text.
      const stateChip = document.createElement("span"); stateChip.className = "conn-state " + st.cls;
      stateChip.textContent = WavrT(st.word);
      foot.appendChild(stateChip);
      const managed = document.createElement("span"); managed.className = "conn-managed-chip";
      managed.textContent = WavrT("Managed by the hub — not switchable here");
      foot.appendChild(managed);
      if(c.env_flag){
        const det = document.createElement("details"); det.className = "conn-env-details";
        const sum = document.createElement("summary"); sum.textContent = WavrT("Details");
        det.appendChild(sum);
        const p = document.createElement("p");
        p.textContent = WavrT("Set {flag} and restart the hub to change this.", {flag: c.env_flag});
        det.appendChild(p);
        foot.appendChild(det);
      }
    } else {
      // registry (generic full gate) OR registry-overlay (built-in override): a REAL,
      // persisting On/Off toggle. The switch is on when the admin enabled it (override
      // "on") OR the hub's env flag turned it on with no override. Flipping it POSTs a
      // persisted override that survives restart and revokes immediately when turned off.
      const isGeneric = c.enforcement === "registry";
      const on = isGeneric ? c.active
                           : (c.override === "on" || (c.override == null && c.env_active === true));
      const tog = document.createElement("button"); tog.type = "button";
      tog.className = "ctl switch" + (on ? " on" : "");
      tog.setAttribute("aria-pressed", on ? "true" : "false");
      tog.setAttribute("aria-label", WavrT(on ? "turn off {name}" : "turn on {name}", {name: c.label || c.id}));
      tog.textContent = on ? WavrT("On") : WavrT("Off");
      tog.onclick = async ()=>{
        tog.disabled = true;
        let r;
        try{ r = await WavrAPI.fetch("/api/connectors/"+encodeURIComponent(c.id)+"/enable", {method: "POST", json: {enabled: !on}}); }catch{}
        if(r && r.ok){
          let j; try{ j = await r.json(); }catch{ j = {}; }
          const updated = j.connector;
          if(updated){
            const idx = connectors.findIndex(x => x.id === updated.id);
            if(idx >= 0) connectors[idx] = updated;
          }
          actionFeedback(fb, true, null, on ? WavrT("✓ turned off") : WavrT("✓ turned on"));
          render(connectors);               // re-render from the authoritative descriptor
        } else {
          tog.disabled = false;
          actionFeedback(fb, false,
            (r && r.status === 409) ? WavrT("controlled by an environment flag") : WavrT("couldn't change"));
        }
      };
      foot.appendChild(tog);
      const stch = document.createElement("span"); stch.className = "conn-state " + st.cls;
      stch.textContent = WavrT(st.word);
      foot.appendChild(stch);
      // Enabled-but-not-live: a plain-language note about what's still needed (restart /
      // key / HA creds). This is the honesty requirement — an enabled connector that
      // can't yet run says so, and reaches nowhere until it's configured.
      const note = needsNote(c);
      if(note){
        const np = document.createElement("p"); np.className = "conn-note";
        np.textContent = note;              // textContent — no interpolation of untrusted data
        foot.appendChild(np);
      }
      // When it's off and configurable via the hub too, offer the env alternative behind
      // a Details disclosure so jargon never sits in the primary card text.
      if(!on && c.env_flag){
        const det = document.createElement("details"); det.className = "conn-env-details";
        const sum = document.createElement("summary"); sum.textContent = WavrT("Details");
        det.appendChild(sum);
        const p = document.createElement("p");
        p.textContent = WavrT("Prefer config files? Set {flag}=1 in the hub and restart — same effect as this switch.", {flag: c.env_flag});
        det.appendChild(p);
        foot.appendChild(det);
      }
    }
    card.appendChild(foot);
    return card;
  }

  function render(connectors){
    connectors = Array.isArray(connectors) ? connectors : [];
    host.textContent = "";
    let activeN = 0;
    // Must | Connectors | 7: partition into two plainly-labeled groups via the existing
    // isEgress() helper — an empty group never renders a header (no dangling "0 items").
    const groups = [
      {title: WavrT("Can reach outside your network"), items: []},
      {title: WavrT("Stays on your local network"), items: []},
    ];
    connectors.forEach(c => {
      if(c.active) activeN++;
      (isEgress(c) ? groups[0] : groups[1]).items.push(c);
    });
    groups.forEach(g => {
      if(!g.items.length) return;
      const h = document.createElement("h3"); h.className = "conn-group-h"; h.textContent = g.title;
      host.appendChild(h);
      g.items.forEach(c => host.appendChild(buildCard(connectors, c)));
    });
    // "Nothing is reaching outward" stays pinned in the static #connSummary above #connList.
    summary.textContent = activeN === 0
      ? WavrT("Nothing is reaching outward right now — every connector is off.")
      : WavrT("{n} connector is active.|{n} connectors are active.", {n: activeN});
  }
  render(list);
}
renderConnectors();

