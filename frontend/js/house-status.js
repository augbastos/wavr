// ==========================================================================
// house-status.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Build A10: House status (unified network+physical "is everything OK?" signal) ----
// GET /api/house-status: derived-only composition of signals already rendered elsewhere
// (Rede's rogue-device/rogue-DHCP/gateway-identity alerts + Watch's intrusion room +
// occupancy routine anomaly) -- this tile never introduces a new raw field, only a ranked
// summary + evidence trail of what already exists. Same MODE gate/poll shape as renderNetwork.
/* Translate the sentence; leave the bracketed value alone.
 *
 * `house_status.py` composes a reason as "<sentence> (<value>)", the value
 * being a vendor name, an IP or a hostname. Handing the whole thing to `WavrT`
 * looked up "unrecognized device on the network (Microsoft)" — a string that
 * exists in no source file and therefore in no catalogue. The sentence stayed
 * English on a Portuguese screen, and a household's network data was being
 * used as a translation key.
 *
 * The backend keeps the sentence and the value as separate literals now, so
 * the sentence IS in the catalogue. This splits on the last " (" — the only
 * place the composer puts one — and translates the half that is copy.
 */
function sentenceThenData(text) {
  const s = String(text || "");
  const at = s.lastIndexOf(" (");
  if (at < 1 || !s.endsWith(")")) return WavrT(s);
  return WavrT(s.slice(0, at)) + s.slice(at);
}

function renderHouseStatus(){
  if(MODE!=="live" && MODE!=="companion") return;     // never in Plano B (demo)
  if(MODE==="companion" && !companionToken()) return; // companion pairing state: no token yet
  const auth = MODE==="companion" ? {"Authorization":"Bearer "+companionToken()} : null;
  const tile = document.getElementById("houseStatusTile");
  const line = document.getElementById("houseStatusLine");
  const details = document.getElementById("houseStatusDetails");
  const summary = document.getElementById("houseStatusSummary");
  const list = document.getElementById("houseStatusReasons");
  const infoNote = document.getElementById("houseStatusInfoNote");
  if(!tile || !line) return;
  // One literal per entry, resolved at paint time: the catalogue check reads
  // `WavrT("...")` call sites, and a layer this build has never heard of falls
  // through to whatever the Core called it rather than to an empty cell.
  const LAYER_LABEL = {network: () => WavrT("Network"), physical: () => WavrT("Space")};
  /* Two consecutive misses, then say so. Never keep a verdict.
   *
   * This used to `return` on a failed fetch with the comment "keep last view",
   * and the last view is a green dot and "Everything looks normal." So: the
   * Core dies, the chrome chip correctly turns red and says "Not responding",
   * and a hundred pixels below it — in a bigger tile, in green — the landing
   * screen goes on saying everything is fine. Two health verdicts on one
   * screen contradicting each other, and the reassuring one is the larger.
   * A person reads the reassurance and walks away.
   *
   * Two misses rather than one, because a single dropped request at a 20-second
   * poll is not evidence of anything and a tile that flickers between "fine"
   * and "cannot tell" is its own kind of lie. Forty seconds of silence is.
   */
  let misses = 0;
  function cannotTell(){
    misses += 1;
    if(misses < 2) return;                    // one dropped request proves nothing
    tile.hidden = false;
    line.className = "house-status-line unknown";
    line.textContent = WavrT("Wavr is not answering, so this cannot be checked. Check that the Core is running.");
    details.hidden = true;
    if(infoNote) infoNote.hidden = true;
    list.textContent = "";
  }

  async function refresh(){
    let hs;
    try{
      // A DEADLINE, because a refused request was never the hard case.
      //
      // A Core that goes dark — power cut, suspend, Wi-Fi dropped — does not
      // refuse the connection, it says nothing. `await fetch(...)` then never
      // settles, this function never reaches its render, and the tile keeps
      // whatever it last drew: a green dot and "Everything looks normal."
      // Handling the failed fetch alone left that untouched, and a browser
      // test holding the request is what showed it.
      const r = await WavrAPI.fetch("/api/house-status",
                                    {headers: auth || undefined, timeoutMs: 8000});
      if(auth && (r.status===401||r.status===403)){ companionAuthFailed(); return; }
      if(!r.ok){ cannotTell(); return; }       // refused or absent: no verdict either way
      hs = await r.json();
    }catch{ cannotTell(); return; }
    misses = 0;
    tile.hidden = false;
    const reasons = Array.isArray(hs.reasons) ? hs.reasons : [];
    const status = (typeof hs.status === "string" && hs.status) ? hs.status : "ok";
    // AMBER IS A PROMISE, and this line was breaking it six times over.
    //
    // `notice` is everything below `alert` on the severity ladder, so it covers
    // `info` and `note` — an observation nobody has to act on — as well as
    // `watch`. All of them painted amber, the colour this product uses for
    // "needs attention", and the sentence underneath then had to spend three
    // lines taking it back: "these are things Wavr noticed, not a to-do list —
    // none of them needs a decision from you". A screen that has to argue with
    // its own colour has the colour wrong, and every amber spent on something
    // that needs nothing is amber that means less the day something does.
    //
    // The ladder is not touched: `severity` is already on every reason, and the
    // question "is any of these above a note?" is a presentation question, so
    // it is answered here. `watch` and up keep the amber they earn.
    const LADDER = ["info", "note", "watch", "alert", "critical"];
    const worstRank = reasons.reduce(
      (m, r) => Math.max(m, LADDER.indexOf(r && r.severity)), -1);
    const nothingToDo = status === "notice" && worstRank <= LADDER.indexOf("note");
    line.className = "house-status-line " + status + (nothingToDo ? " noted" : "");
    // "unknown" is not a severity between the others, it is a refusal to
    // answer: nothing is wrong AND nothing is watching. "Everything looks
    // normal." there is the most damaging sentence this product can produce —
    // reassurance nobody earned. A status this build has never heard of lands
    // here too, rather than in the old "Worth a glance" fallback, which would
    // quietly invent a verdict for a value it cannot interpret.
    if(status === "ok"){
      line.textContent = WavrT("Everything looks normal.");
    } else if(status === "unknown"){
      line.textContent = WavrT("Nothing is being checked — no network monitor, no Watch and no history, so Wavr has nothing to report either way.");
    } else {
      // The count sits INSIDE the sentence rather than being glued onto a
      // translated head: a language that orders the clause differently has to
      // be able to move it.
      const head = status === "alert" ? WavrT("Needs attention")
        : status === "notice" ? WavrT("Worth a glance")
        : WavrT("Wavr reported a state this screen does not recognise ({status})", {status: status});
      line.textContent = WavrT("{head} — {n} thing|{head} — {n} things",
                               {head: head, n: reasons.length});
    }
    details.hidden = reasons.length === 0;
    // Disambiguates this tile from Manage → Needs attention (see the comment on
    // #houseStatusInfoNote in index.html): both can be true — reasons here, and
    // nothing there — without either being wrong.
    if(infoNote) infoNote.hidden = reasons.length === 0;
    summary.textContent = WavrT("show details");
    list.textContent = "";
    reasons.forEach(rs => {
      const sev = (typeof rs.severity === "string" && rs.severity) ? rs.severity : "note";
      const li = document.createElement("li"); li.className = "hsr-row sev-" + sev;
      const layerEl = document.createElement("span"); layerEl.className = "hsr-layer";
      layerEl.textContent = LAYER_LABEL[rs.layer] ? LAYER_LABEL[rs.layer]() : (rs.layer || "");
      const badge = document.createElement("span"); badge.className = "sev-badge sev-" + sev;
      badge.textContent = WavrT(sev);          // textContent — untrusted
      const what = document.createElement("span"); what.className = "hsr-what";
      // textContent — untrusted. `sentenceThenData` translates the sentence and
      // leaves the bracketed value alone; see its own comment.
      what.textContent = rs.what ? sentenceThenData(rs.what) : "";
      li.appendChild(layerEl); li.appendChild(badge); li.appendChild(what);
      const rel = fmtRelative(rs.ts);
      if(rel){
        const at = document.createElement("span"); at.className = "hsr-at";
        at.textContent = " · " + rel;
        li.appendChild(at);
      }
      list.appendChild(li);
    });
  }
  refresh(); setInterval(refresh, 20000);
}
renderHouseStatus();

