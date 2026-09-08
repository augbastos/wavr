// ==========================================================================
// pairing.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Pairing panel (Plano A / live only, multidevice-gated) ----
// Mints one-time multi-device pairing codes and lists/revokes paired devices.
// The backend routes (/api/pair-code, /api/devices) only exist when WAVR_MULTIDEVICE=1;
// when they 404 (or the backend is unreachable) the whole panel stays hidden, so
// single-device installs never see it. Never shows in Plano B (demo).
// Lazy-load the self-hosted QR generator (vendor/qrcode.js, MIT — no CDN) ONCE, only when the
// pairing panel actually needs to draw a QR. Resolves to the global `qrcode` factory. Kept out
// of the boot path so a single-device install (no pairing) never fetches it.
let _qrLibPromise = null;
function _ensureQRCode(){
  if(window.qrcode) return Promise.resolve(window.qrcode);
  if(_qrLibPromise) return _qrLibPromise;
  _qrLibPromise = new Promise((resolve, reject)=>{
    const s = document.createElement("script");
    s.src = "vendor/qrcode.js";           // same origin — nothing leaves home
    s.onload = ()=> window.qrcode ? resolve(window.qrcode) : reject(new Error("qrcode global missing"));
    s.onerror = ()=> { _qrLibPromise = null; reject(new Error("failed to load vendor/qrcode.js")); };
    document.head.appendChild(s);
  });
  return _qrLibPromise;
}

// What Devices shows when nothing can pair.
//
// Hiding the pairing panel while other devices are shut out is right: its
// endpoints are not mounted and every control in it would fail. But hiding it
// and saying nothing turned the one screen a person opens in order to connect a
// phone into an empty space, while the website, the download page and the rescue
// script were all pointing at it. The first real user went looking, found
// nothing, and wrote "estou perdido".
//
// This says the plain thing and walks him to the switch. It deliberately does
// not carry a switch of its own — `lan_access` is sensitive and local-only, and
// a second control writing it would be a second place to keep that guard
// correct.
function _pairingUnavailable(){
  const tile = document.getElementById("pairingOff");
  if(!tile) return;
  tile.hidden = false;
  const btn = document.getElementById("pairingOffGo");
  if(!btn || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  btn.addEventListener("click", () => {
    // The row is built from SETTING_SPECS at render time and stamped with its
    // key, so this points at the setting rather than at a guess about where on
    // the page it ended up.
    const row = document.querySelector('.setting-row[data-key="lan_access"]');
    if(!row){
      // Core settings has not rendered yet (or the key is gone). Say so instead
      // of scrolling nowhere and looking broken.
      const fb = document.createElement("span");
      fb.className = "action-fb";
      fb.textContent = WavrT("Open “Core settings” below — the switch is in there.");
      btn.after(fb);
      return;
    }
    const sec = document.getElementById("gearSecSettings");
    if(sec && window.__wavrOpenGearSection) window.__wavrOpenGearSection("gearSecSettings");
    row.scrollIntoView({block: "center", behavior: "smooth"});
    row.classList.add("setting-row-pointed");
    setTimeout(() => row.classList.remove("setting-row-pointed"), 2600);
    const ctl = row.querySelector("button, input, select");
    if(ctl && ctl.focus) ctl.focus({preventScroll: true});
  });
}

async function renderPairing(){
  if(MODE!=="live") return;                 // live-only; never in Plano B (demo)
  if(await _wavrMultideviceOff){ _pairingUnavailable(); return; }   // proven off via /api/status — skip the doomed probe (no console 404)
  // Probe the multidevice API first. A 404 means the feature is off; a thrown fetch
  // means the backend is momentarily unreachable. Either way: leave the panel hidden.
  let probe;
  try{ probe = await WavrAPI.fetch("/api/devices"); }
  catch{ return; }                          // backend unreachable — stay hidden
  if(!probe.ok){ _pairingUnavailable(); return; }   // 404 → multidevice off → stay hidden
  let seed; try{ seed = (await probe.json()).devices; }catch{ seed = []; }
  const _off = document.getElementById("pairingOff");
  if(_off) _off.hidden = true;              // pairing works now — the stand-in has nothing to say
  document.getElementById("pairing").hidden = false;

  const devList = document.getElementById("pairDevList");
  const codeBox = document.getElementById("pairCodeBox");
  const codeEl  = document.getElementById("pairCode");
  const pairFb  = document.getElementById("pairFb");

  async function refresh(devices){
    if(devices === undefined){              // no cached list passed in — fetch a fresh one
      try{ devices = (await (await WavrAPI.fetch("/api/devices")).json()).devices; }
      catch{ return; }                      // backend momentarily unreachable — keep last view
    }
    devices = Array.isArray(devices) ? devices : [];
    devList.textContent = "";
    if(!devices.length){
      const e = document.createElement("div"); e.className = "empty";
      e.textContent = WavrT("no paired devices");
      devList.appendChild(e); return;
    }
    devices.forEach(d => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = d.name || WavrT("(unnamed)");  // textContent — server data
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      const seen = (typeof d.last_seen_ts==="string" && d.last_seen_ts.length>=19)
                   ? d.last_seen_ts.slice(11,19)
                   : (d.last_seen_ts!=null ? String(d.last_seen_ts) : "—");
      meta.textContent = " · " + (d.role || "?") + " · " + WavrT("seen {when}", {when: seen}) + (d.revoked ? WavrT(" · revoked") : "");
      left.appendChild(name); left.appendChild(meta);
      row.appendChild(left);
      const ctl = document.createElement("span"); ctl.className = "pair-dev-ctl";
      if(d.role === "guest"){
        // Guest devices are NOT promotable from this list — a guest row shows a plain
        // badge + its auto-expiry deadline instead of the User/Admin select. Promoting a
        // guest to User/Admin here would silently defeat the whole point of a time-boxed,
        // presence:write-only pass (see devices.py VALID_ROLES + auth.DEFAULT_SCOPES["guest"]).
        const guestBadge = document.createElement("span"); guestBadge.className = "pill off";
        guestBadge.textContent = WavrT("Guest");
        ctl.appendChild(guestBadge);
        if(typeof d.expires_at === "string" && d.expires_at){
          const dt = new Date(d.expires_at);
          if(!isNaN(dt.getTime())){
            const exp = document.createElement("span"); exp.className = "pair-dev-meta";
            exp.textContent = WavrT("expires {time}", {time: WavrFmt.time(dt)});
            ctl.appendChild(exp);
          }
        }
      } else {
        // Role control: promote/demote a paired device between Admin (stored 'central')
        // and User (stored 'user') without revoke+re-pair. UI labels differ from the
        // stored VALUES on purpose (backend auth keys off 'central'/'root').
        const roleSel = document.createElement("select"); roleSel.className = "pair-dev-role";
        roleSel.setAttribute("aria-label", WavrT("device role"));
        const optUser = document.createElement("option"); optUser.value = "user"; optUser.textContent = WavrT("User");
        const optAdmin = document.createElement("option"); optAdmin.value = "central"; optAdmin.textContent = WavrT("Admin");
        roleSel.appendChild(optUser); roleSel.appendChild(optAdmin);
        roleSel.value = (d.role === "central") ? "central" : "user";  // reflect current stored role
        roleSel.onchange = async ()=>{
          const newRole = roleSel.value;                              // 'user' | 'central'
          let r;
          try{ r = await WavrAPI.fetch("/api/devices/"+encodeURIComponent(d.device_id)+"/role", {method: "POST", json: {role:newRole}}); }catch{}
          if(r && r.ok){ actionFeedback(pairFb, true); refresh(); }   // re-sync after role change
          else { actionFeedback(pairFb, false, WavrT("couldn't change the role")); refresh(); }  // revert select to server truth
        };
        ctl.appendChild(roleSel);
      }
      const rm = document.createElement("button"); rm.type = "button"; rm.className = "rm";
      rm.textContent = WavrT("Revoke");
      rm.onclick = async ()=>{
        let r;
        try{ r = await WavrAPI.fetch("/api/devices/"+encodeURIComponent(d.device_id), {method: "DELETE"}); }catch{}
        if(r && r.ok){ actionFeedback(pairFb, true); refresh(); }   // re-sync after revoking
        else { actionFeedback(pairFb, false, WavrT("couldn't revoke the device")); }
      };
      ctl.appendChild(rm);
      row.appendChild(ctl); devList.appendChild(row);
    });
  }

  // ---- Auto-rotating pairing code (2FA-style) ----
  // The code re-mints itself before it can expire, so the operator never races the 120s
  // TTL while walking to each device. Re-minting does NOT invalidate a previously shown
  // code — each lives its own 120s window (backend pairing.py keeps codes independent) —
  // so a code being typed on a phone keeps working right through a refresh. SECURITY:
  // rotation runs ONLY while this panel is on screen; the tick() visibility guard stops it
  // the instant Settings is closed / another tab is shown, so we never leave a fresh valid
  // code standing while nobody is looking at it.
  const cdEl   = document.getElementById("pairCountdown");
  const cdBar  = document.getElementById("pairCountdownBar");
  const cdFill = cdBar ? cdBar.querySelector("i") : null;
  const fpBox  = document.getElementById("pairFingerprintBox");
  const fpEl   = document.getElementById("pairFingerprint");
  const verifyBox = document.getElementById("pairVerifyBox");
  const verifyEl  = document.getElementById("pairVerify6");
  const CODE_TTL_MS = 120000, REFRESH_AT_MS = 20000;   // mint a fresh code with ~20s left
  let rotTimer = null, rotExpiresAt = 0, rotRole = "user", rotBusy = false;

  function stopRotation(){ if(rotTimer){ clearInterval(rotTimer); rotTimer = null; } }

  async function mintOnce(){
    if(rotBusy) return;                     // never overlap a slow request with a tick
    rotBusy = true;
    let r, d;
    try{
      r = await WavrAPI.fetch("/api/pair-code", {method: "POST", json: {role:rotRole}});
      if(!r.ok) throw 0;                     // e.g. multidevice turned off mid-session
      d = await r.json();
    }catch{ actionFeedback(pairFb, false, WavrT("couldn't generate the code")); stopRotation(); rotBusy = false; return; }
    codeEl.textContent = String(d.code || "");   // textContent — 8-digit code from server
    codeBox.hidden = false;
    rotExpiresAt = Date.now() + CODE_TTL_MS;
    // Friendly 6-digit verify code (backend-derived, bound to THIS pair-code's short TTL) is
    // the primary glanceable anchor. Only rendered when the response actually carries it, so
    // a backend that doesn't send verify6 yet leaves this hidden and changes nothing else.
    if(verifyBox && verifyEl){
      const v6 = (typeof d.verify6 === "string" || typeof d.verify6 === "number")
                 ? String(d.verify6).replace(/\D/g,"").padStart(6,"0").slice(-6) : "";
      verifyEl.textContent = v6 ? (v6.slice(0,3) + " " + v6.slice(3)) : "";  // textContent — server value
      verifyBox.hidden = !v6;
    }
    // Out-of-band MitM defense (audit #1): show the live cert fingerprint so the operator
    // can verify it against the phone's browser certificate warning. Only shown when the
    // backend actually serves TLS and could read the cert (null on plain-HTTP loopback).
    if(fpBox && fpEl){
      const fp = (typeof d.cert_fingerprint === "string") ? d.cert_fingerprint : "";
      fpEl.textContent = fp;                 // textContent — server-provided fingerprint
      fpBox.hidden = !fp;
    }
    // QR strong anchor (chosen 2026-07-14): encode {url, FULL fingerprint, code} so the phone
    // can SCAN it and pin the full 256-bit cert (not offline-grindable) instead of typing a
    // short code. Only when the backend served a real fingerprint (TLS); on plain-HTTP loopback
    // it stays hidden and the typed 6-digit/8-digit flow is the path, changing nothing else.
    const qrBox = document.getElementById("pairQrBox");
    const qrImg = document.getElementById("pairQrImg");
    const noQrNote = document.getElementById("pairNoQrNote");
    if(qrBox && qrImg){
      const qrFp = (typeof d.cert_fingerprint === "string") ? d.cert_fingerprint : "";
      // No QR is a CORRECT state on a plain-HTTP Core (no certificate to encode), not a
      // broken one — say so, rather than leaving the box simply missing with no reason.
      if(noQrNote) noQrNote.hidden = !!qrFp;
      if(qrFp && d.code){
        // P2 self-contained QR: encode a phone-reachable base. Only override location.origin with
        // the backend's lan_url when the CURRENT origin is NOT already a routable LAN IP: a bare
        // loopback (127.x) or a hub-local hostname (dev.wavr.core kiosk, localhost, *.local) can't
        // be scanned cold, so use lan_url; but when the panel is browsed from a real LAN address
        // (192.168.x etc) that origin is proven-reachable by the viewing device -- keep it, so a
        // multi-NIC/VPN host doesn't swap it for a different-interface lan_url (adversarial-verify
        // hardening). lan_url is the same _local_ipv4()-derived address peers-admin uses, never
        // hardcoded; falls back to location.origin for an older backend that omits lan_url.
        const _oh = location.hostname;
        const _originRoutable = /^\d{1,3}(\.\d{1,3}){3}$/.test(_oh) && !/^127\./.test(_oh) && _oh !== "0.0.0.0";
        const qrUrl = (!_originRoutable && typeof d.lan_url === "string" && d.lan_url) ? d.lan_url : location.origin;
        const payload = JSON.stringify({v:1, u:qrUrl, fp:qrFp, c:String(d.code)});
        _ensureQRCode().then((qr)=>{
          const q = qr(0, "M");              // type 0 = auto-size for the data, error-correction M
          q.addData(payload); q.make();
          qrImg.src = q.createDataURL(5, 8); // cellSize 5px, quiet-zone 8 modules → ~184px, white bg
          // Say the address out loud. `qrUrl` is the phone-reachable base this
          // QR already encodes; printing it costs one line and answers "is this
          // thing actually open to my network?" without a second device and
          // without a scanner.
          const urlEl = document.getElementById("pairQrUrl");
          if(urlEl){
            urlEl.textContent = WavrT("This Core answers at {url}", {url: qrUrl});
            urlEl.hidden = false;
          }
          qrBox.hidden = false;
        }).catch(()=>{ qrBox.hidden = true; });   // lib missing → fall back to the typed code
      } else {
        qrBox.hidden = true;
      }
    }
    if(cdBar) cdBar.hidden = false;
    actionFeedback(pairFb, true);
    refresh();                              // a fresh code may register a slot; keep list current
    rotBusy = false;
  }

  function tick(){
    // Visibility guard: the pairing tile lives inside the Settings overlay (hidden ->
    // display:none -> offsetParent null). Stop the instant it isn't shown so no valid code
    // lingers unattended.
    const tile = document.getElementById("pairing");
    if(!tile || tile.hidden || !tile.offsetParent){ stopRotation(); return; }
    const leftMs = rotExpiresAt - Date.now();
    const leftS  = Math.max(0, Math.ceil(leftMs/1000));
    if(cdEl)   cdEl.textContent = leftS + "s";
    if(cdFill) cdFill.style.width = Math.max(0, Math.min(100, (leftMs/CODE_TTL_MS)*100)) + "%";
    if(leftMs <= REFRESH_AT_MS && !rotBusy) mintOnce();   // refresh before the current code dies
  }

  async function startRotation(role){
    stopRotation();
    rotRole = role;
    await mintOnce();
    rotTimer = setInterval(tick, 1000);
  }

  const pairForm = document.getElementById("pairForm");
  const pairTile = document.getElementById("pairing");

  // Zero-click auto-start: the moment the pairing panel becomes visible (operator opens
  // Settings), a live code starts rotating on its own — no button press. Just open Wavr
  // here and open the companion on the phone. tick()'s visibility guard still stops the
  // rotation when the panel hides, so a valid code exists ONLY while this panel is shown.
  if(pairTile && "IntersectionObserver" in window){
    const io = new IntersectionObserver((entries)=>{
      for(const en of entries){
        if(en.isIntersecting){ if(!rotTimer) startRotation(pairForm.role.value); }
        else stopRotation();
      }
    }, {threshold:0.01});
    io.observe(pairTile);
  }

  // Changing the access level (User/Admin) while a code is live re-mints under the new role.
  pairForm.role.onchange = ()=>{ if(rotTimer) startRotation(pairForm.role.value); };

  // The button is now just a manual "refresh now" (rotation is automatic) — kept for control.
  pairForm.onsubmit = (e)=>{
    e.preventDefault();
    startRotation(pairForm.role.value);
  };

  const copyBtn = document.getElementById("pairCopy");
  copyBtn.onclick = ()=>{
    const code = codeEl.textContent; if(!code) return;
    try{ if(navigator.clipboard) navigator.clipboard.writeText(code).catch(()=>{}); }catch{}
    copyBtn.textContent = WavrT("Copied"); setTimeout(()=>{ copyBtn.textContent = WavrT("Copy"); }, 1500);
  };

  refresh(seed);                            // seed the list from the probe response we already read
}
renderPairing();

// ---- Guest pass (feature #8, guest mode 2026-07-16) ----
// Host mints a TIME-BOXED guest invite: POST /api/guest/invite {hours}. SAME require_local +
// admin gate as /api/pair-code above, so this control is visible under the IDENTICAL condition
// as the Pairing panel — MODE==="live" only, never shown to any companion (admin or not),
// mirroring renderPairing()'s own gate exactly. The guest scans the SAME QR/verify6/fingerprint
// compare as a normal pairing code (byte-identical MitM defense, reusing _ensureQRCode()) and
// redeems at the SAME POST /api/pair — the response just additionally carries `expires_at`/
// `role:"guest"`. Unlike the always-on Pairing panel (which auto-mints the moment it scrolls
// into view), minting here only starts on an explicit "Create guest pass" tap — a guest pass is
// an occasional, deliberate action, not a standing invite. Once started, the code auto-refreshes
// before its own ~2-min redemption window expires (same tick()/mintOnce() idiom as renderPairing
// above) and the "expires at" preview recomputes on every refresh; rotation stops the instant the
// tile leaves the screen (same visibility guard as renderPairing's own tick()).
function renderGuestInvite(){
  if(MODE!=="live") return;                 // live-only; never companion or Plano B (demo) — mirrors renderPairing()'s own gate
  const tile = document.getElementById("guestInvite");
  if(!tile) return;                         // additive: a stale cached page without this markup just no-ops
  (async ()=>{
    if(await _wavrMultideviceOff) return;   // /api/guest/invite is mounted only under the SAME `if cfg.multidevice` block as /api/pair-code — proven off via /api/status, no doomed probe
    tile.hidden = false;

    const form       = document.getElementById("guestForm");
    const fb         = document.getElementById("guestFb");
    const qrBox       = document.getElementById("guestQrBox");
    const qrImg       = document.getElementById("guestQrImg");
    const verifyBox   = document.getElementById("guestVerifyBox");
    const verifyEl    = document.getElementById("guestVerify6");
    const codeBox     = document.getElementById("guestCodeBox");
    const codeEl      = document.getElementById("guestCode");
    const cdEl        = document.getElementById("guestCountdown");
    const cdBar       = document.getElementById("guestCountdownBar");
    const cdFill      = cdBar ? cdBar.querySelector("i") : null;
    const fpBox       = document.getElementById("guestFingerprintBox");
    const fpEl        = document.getElementById("guestFingerprint");
    const copyBtn     = document.getElementById("guestCopy");
    const expiryNote  = document.getElementById("guestExpiryNote");

    const CODE_TTL_MS = 120000, REFRESH_AT_MS = 20000;   // same TTL as pair-code — mint a fresh code with ~20s left
    let rotTimer = null, rotExpiresAt = 0, rotBusy = false;

    function stopRotation(){ if(rotTimer){ clearInterval(rotTimer); rotTimer = null; } }

    async function mintOnce(){
      if(rotBusy) return;                   // never overlap a slow request with a tick
      rotBusy = true;
      const hours = Math.max(0.25, Math.min(24, Number(form.hours.value) || 4));  // mirrors the backend's own server-side clamp
      let r, d;
      try{
        r = await WavrAPI.fetch("/api/guest/invite", {method: "POST", json: {hours:hours}});
        if(!r.ok) throw 0;                   // e.g. multidevice turned off mid-session
        d = await r.json();
      }catch{ actionFeedback(fb, false, WavrT("couldn't create the guest pass")); stopRotation(); rotBusy = false; return; }

      codeEl.textContent = String(d.code || "");   // textContent — 8-digit code from server
      codeBox.hidden = false;
      rotExpiresAt = Date.now() + CODE_TTL_MS;

      const v6 = (typeof d.verify6 === "string" || typeof d.verify6 === "number")
                 ? String(d.verify6).replace(/\D/g,"").padStart(6,"0").slice(-6) : "";
      verifyEl.textContent = v6 ? (v6.slice(0,3) + " " + v6.slice(3)) : "";  // textContent — server value
      verifyBox.hidden = !v6;

      const fp = (typeof d.cert_fingerprint === "string") ? d.cert_fingerprint : "";
      fpEl.textContent = fp;                 // textContent — server-provided fingerprint
      fpBox.hidden = !fp;

      const noQrNote = document.getElementById("guestNoQrNote");
      // Same correct-not-broken state as renderPairing()'s own #pairNoQrNote.
      if(noQrNote) noQrNote.hidden = !!fp;
      if(fp && d.code){
        const _oh = location.hostname;
        const _originRoutable = /^\d{1,3}(\.\d{1,3}){3}$/.test(_oh) && !/^127\./.test(_oh) && _oh !== "0.0.0.0";
        const qrUrl = (!_originRoutable && typeof d.lan_url === "string" && d.lan_url) ? d.lan_url : location.origin;
        const payload = JSON.stringify({v:1, u:qrUrl, fp:fp, c:String(d.code)});
        _ensureQRCode().then((qr)=>{
          const q = qr(0, "M");
          q.addData(payload); q.make();
          qrImg.src = q.createDataURL(5, 8);
          qrBox.hidden = false;
        }).catch(()=>{ qrBox.hidden = true; });   // lib missing → fall back to the typed code
      } else {
        qrBox.hidden = true;
      }

      // "Guest access expires at HH:MM" — plain local time, recomputed on every refresh (the
      // preview always reads "if they pair right now, access runs until…"; the redeemed
      // device's real deadline is stamped from the SAME `hours` at the moment they actually
      // scan, within this code's own ~2-min window — see pairing.py's mint_guest_code).
      if(typeof d.expires_at === "string" && d.expires_at){
        const dt = new Date(d.expires_at);
        if(!isNaN(dt.getTime())){
          expiryNote.hidden = false;
          expiryNote.textContent = WavrT("Guest access expires at {time}.", {time: WavrFmt.time(dt)});
        } else expiryNote.hidden = true;
      } else expiryNote.hidden = true;

      if(cdBar) cdBar.hidden = false;
      actionFeedback(fb, true, "", WavrT("✓ guest pass ready"));
      rotBusy = false;
    }

    function tick(){
      // Visibility guard: stop the instant the tile isn't shown (Settings closed, or this
      // section scrolled out via display:none at the panel form factor) — same idiom as
      // renderPairing()'s own tick(), so a valid guest code never lingers unattended.
      if(tile.hidden || !tile.offsetParent){ stopRotation(); return; }
      const leftMs = rotExpiresAt - Date.now();
      const leftS  = Math.max(0, Math.ceil(leftMs/1000));
      if(cdEl)   cdEl.textContent = leftS + "s";
      if(cdFill) cdFill.style.width = Math.max(0, Math.min(100, (leftMs/CODE_TTL_MS)*100)) + "%";
      if(leftMs <= REFRESH_AT_MS && !rotBusy) mintOnce();   // refresh before the current code dies
    }

    function startRotation(){
      stopRotation();
      mintOnce().then(()=>{ rotTimer = setInterval(tick, 1000); });
    }

    form.onsubmit = (e)=>{ e.preventDefault(); startRotation(); };
    // Changing the duration while a pass is already live re-mints under the new hours
    // immediately — same UX as renderPairing()'s role-change re-mint.
    form.hours.onchange = ()=>{ if(rotTimer) startRotation(); };

    copyBtn.onclick = ()=>{
      const code = codeEl.textContent; if(!code) return;
      try{ if(navigator.clipboard) navigator.clipboard.writeText(code).catch(()=>{}); }catch{}
      copyBtn.textContent = WavrT("Copied"); setTimeout(()=>{ copyBtn.textContent = WavrT("Copy"); }, 1500);
    };
  })();
}
renderGuestInvite();

// ---- Approve-on-Core pairing banner (live only, multidevice-gated, design 2026-07-11) ----
// A companion that hasn't got a code yet asks the Core to let it in (POST /api/pair-request,
// unauth/in-subnet, companion-side — not built here). This banner polls the loopback-root list
// and lets the LOCAL operator Approve/Deny, with an explicit two-tap "does this fingerprint
// match?" confirm — the out-of-band MitM defense (reuses .pair-fp-box/.pair-fp-label/.pair-fp/
// .pair-fp-warn verbatim, same as the pairing-code fingerprint box above). The fingerprint shown
// is THIS hub's own live cert (server-provided by /api/pending-pairings), never the companion's
// self-reported value. /api/pending-pairings only exists when multidevice is on; a 404 (or
// unreachable backend) leaves the banner and its header pill hidden forever — byte-identical to
// today. The 8-digit /api/pair + /api/pair-code flow (Pairing panel, above) is completely
// untouched and remains the fallback. Every name/ip/platform below is untrusted network data
// rendered via textContent only, never innerHTML.
async function renderPairApprove(){
  if(MODE!=="live") return;                 // live-only; never Plano B (demo) or companion
  if(await _wavrMultideviceOff) return;     // proven off via /api/status — skip the doomed probe
  // Probe the pending list first. A 404 means the feature is off; a thrown fetch means the
  // backend is momentarily unreachable. Either way: leave the banner/pill hidden.
  let probe;
  try{ probe = await WavrAPI.fetch("/api/pending-pairings"); }
  catch{ return; }                          // backend unreachable — stay hidden
  if(!probe.ok) return;                     // 404 → feature off → stay hidden forever
  let seed; try{ seed = await probe.json(); }catch{ seed = {}; }

  const banner  = document.getElementById("pairApprove");
  // Fix F5: the header pill is gone -- a badge-dot on BOTH gear entry points (phone topbar +
  // desktop/tablet sidenav) replaces it; the banner itself is unchanged (already the first
  // thing inside <main>, aria-live="assertive", so it's never actually hidden inside Settings).
  const gearBadges = [document.getElementById("gearTopBtn"), document.getElementById("gearNavBtn")]
    .filter(Boolean);
  const setGearBadge = (on)=> gearBadges.forEach(b => b.classList.toggle("has-pending", on));
  const paH     = document.getElementById("paH");
  const paMeta  = document.getElementById("paMeta");
  const paCode  = document.getElementById("paCode");
  const paFp    = document.getElementById("paFp");
  const paRole  = document.getElementById("paRole");
  const confirmBox = document.getElementById("paConfirmBox");
  const actionsBox = document.getElementById("paActions");
  const paYes   = document.getElementById("paYes");
  const paConfirmInput = document.getElementById("paConfirmInput");
  const paNo    = document.getElementById("paNo");
  const paDeny  = document.getElementById("paDeny");
  const paApprove = document.getElementById("paApprove");
  const paFb    = document.getElementById("paFb");
  const paMore  = document.getElementById("paMore");
  if(!banner || !paH || !paMeta || !paCode || !paFp || !confirmBox || !actionsBox ||
     !paYes || !paNo || !paDeny || !paApprove || !paMore || !paConfirmInput) return;

  let requests = [];        // last-polled pending list, oldest-first (server insertion order)
  // Two different questions, and collapsing them into one variable is what let a
  // two-second poll wipe the operator's access-level choice. `heroId` is which
  // request is SELECTED (the operator can promote one from the waiting list);
  // `paintedId` is which request's details are currently in the DOM. The hero
  // card is rebuilt when, and only when, those disagree.
  let heroId   = null;      // request_id selected as the full-ceremony hero card
  let paintedId = null;     // request_id whose details are on screen right now
  let paintedMore = null;   // the waiting-list ids currently rendered, joined
  let coreFp   = "";        // this hub's own live cert fp — the operator's compare anchor

  function relTime(iso){
    const t = Date.parse(iso);
    if(!isFinite(t)) return WavrT("just now");
    const s = Math.max(0, Math.round((Date.now()-t)/1000));
    if(s < 60) return WavrT("{n}s ago", {n: s});
    return WavrT("{n}m ago", {n: Math.round(s/60)});
  }
  function metaText(rec){
    const bits = [];
    if(rec.platform) bits.push(String(rec.platform));
    if(rec.source_ip) bits.push(String(rec.source_ip));
    bits.push(WavrT("asked {age}", {age: relTime(rec.created_at)}));
    return bits.join(" · ");
  }
  function renderHero(rec){
    heroId = rec.request_id;
    paintedId = rec.request_id;
    paH.textContent = WavrT("{name} wants to connect", {name: rec.requester_name || WavrT("(unnamed device)")});  // textContent — untrusted
    paMeta.textContent = metaText(rec);
    paCode.textContent = rec.compare_code || "——————"; // per-request anchor — unique, shown on the phone too
    paFp.textContent = coreFp || "—";       // THIS hub's own cert fp — never the companion's reported_fp
    if(paRole) paRole.value = "user";       // least-privilege default on every fresh hero
    confirmBox.hidden = true;
    paCode.hidden = false;                  // SECURITY (#9): restore the compare code — this is a fresh hero, not a confirm-in-progress
    actionsBox.hidden = false;
  }
  function renderMore(rest){
    // Same devices waiting as last poll? Leave the rows where they are. Every
    // field these rows show (name, IP, compare code) is fixed for the life of a
    // request, so rebuilding them produced identical markup — and destroyed the
    // focused row twice a second, which is why a keyboard could never come to
    // rest on one and press Enter to promote it.
    const sig = rest.map(r=>r.request_id).join("|");
    if(sig === paintedMore) return;
    paintedMore = sig;
    paMore.textContent = "";
    if(!rest.length){ paMore.hidden = true; return; }
    paMore.hidden = false;
    const h = document.createElement("p"); h.className = "pa-more-head";
    h.textContent = WavrT("{n} more device waiting — decide one at a time"
                          + "|{n} more devices waiting — decide one at a time", {n: rest.length});
    paMore.appendChild(h);
    // Capped so a flood (backend bounds at 20 global / 3 per IP) can't blow up the DOM — the
    // hero above is always the oldest; this is a heads-up list, decided one at a time. Rows are
    // clickable so the operator can promote the request matching the number on THEIR phone into
    // the hero slot, whatever its position in the queue.
    rest.slice(0,5).forEach(r=>{
      const row = document.createElement("div"); row.className = "pa-more-row pa-more-row-btn";
      row.tabIndex = 0; row.setAttribute("role","button");
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = r.requester_name || WavrT("(unnamed device)");
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      meta.textContent = " · " + (r.source_ip || "?") + " · " + (r.compare_code || "——————");
      left.appendChild(name); left.appendChild(meta);
      row.appendChild(left);
      const pick = ()=>{ heroId = r.request_id; render(); };
      row.onclick = pick;
      row.onkeydown = (e)=>{ if(e.key==="Enter" || e.key===" "){ e.preventDefault(); pick(); } };
      paMore.appendChild(row);
    });
  }
  function render(){
    if(!requests.length){
      banner.hidden = true;
      setGearBadge(false);
      heroId = null;
      paintedId = null;
      paintedMore = null;
      return;
    }
    const hero = requests.find(r=>r.request_id===heroId) || requests[0];
    // Rebuild the card only when a DIFFERENT request is in the hero slot — a new
    // device, one promoted from the waiting list, or the previous one expiring or
    // being decided elsewhere. For the same request, refresh the "asked Ns ago"
    // and nothing else.
    //
    // The old condition also required `!confirmBox.hidden`, which protected the
    // two-tap confirm and nothing before it: every two seconds, an operator still
    // choosing an access level had `renderHero` reset the select to its
    // least-privilege default underneath them. Choosing Admin and not looking
    // again approved the device as User, silently. That default belongs on a
    // FRESH hero, which is where it still runs.
    if(hero.request_id === paintedId){
      paMeta.textContent = metaText(hero);
    } else {
      renderHero(hero);
    }
    renderMore(requests.filter(r=>r.request_id!==hero.request_id));
    banner.hidden = false;
    setGearBadge(true);
  }
  async function poll(){
    if(document.hidden) return;             // perf: skip network work while this tab isn't visible
    let r;
    try{ r = await WavrAPI.fetch("/api/pending-pairings"); }
    catch{ return; }                        // backend momentarily unreachable — keep last view
    if(!r.ok) return;
    let d; try{ d = await r.json(); }catch{ return; }
    requests = Array.isArray(d && d.requests) ? d.requests : [];
    if(typeof (d && d.cert_fingerprint) === "string") coreFp = d.cert_fingerprint;
    render();
  }

  paApprove.onclick = ()=>{
    if(!heroId) return;
    actionsBox.hidden = true;
    confirmBox.hidden = false;              // operator now TYPES the number shown on the device
    // SECURITY (#9): hide the Core-side compare code while the confirm box is open — the phone
    // must be the ONLY source of the number the operator types, or the "type the number from the
    // phone" check degrades to "copy the number already on this screen" for a LAN impostor.
    paCode.hidden = true;
    paConfirmInput.value = "";
    try{ paConfirmInput.focus(); }catch{}
  };
  paNo.onclick = ()=>{
    confirmBox.hidden = true;
    paCode.hidden = false;                  // SECURITY (#9): restore now that the confirm step is over
    actionsBox.hidden = false;
  };
  paYes.onclick = async ()=>{
    if(!heroId) return;
    const id = heroId;
    const rec = requests.find(r=>r.request_id===id);
    if(!rec){ actionFeedback(paFb, false, WavrT("That request expired — ask the device to try again.")); confirmBox.hidden = true; paCode.hidden = false; poll(); return; }
    // SECURITY (Stage-4 #1): send exactly the digits the operator TYPED off the phone — NEVER
    // rec.compare_code. The server's constant-time match then fails closed unless the operator
    // truly read the number off the right device; auto-filling the record's own code would make
    // the numeric comparison cosmetic and let an inattentive Approve admit a LAN impostor.
    const code = (paConfirmInput.value || "").replace(/\s+/g,"");
    if(!code){ actionFeedback(paFb, false, WavrT("Type the number shown on the device first.")); try{ paConfirmInput.focus(); }catch{}; return; }
    const role = (paRole && paRole.value==="central") ? "central" : "user";
    paYes.disabled = true; paNo.disabled = true;
    let r;
    try{
      r = await WavrAPI.fetch("/api/pending-pairings/"+encodeURIComponent(id)+"/approve", {method: "POST", json: {role:role, confirm_code:code}});
    }catch{}
    paYes.disabled = false; paNo.disabled = false;
    if(r && r.ok) actionFeedback(paFb, true, null, WavrT("Paired — the device is connecting now."));
    else if(r && r.status===404) actionFeedback(paFb, false, WavrT("That request expired, or the numbers didn't match — ask the device to try again."));
    else actionFeedback(paFb, false, WavrT("Couldn't pair — try again."));
    confirmBox.hidden = true;
    paCode.hidden = false;                  // SECURITY (#9): restore now that the confirm step is over
    poll();                                 // re-sync: the decided request drops off the list
  };
  paDeny.onclick = async ()=>{
    if(!heroId) return;
    const id = heroId;
    paDeny.disabled = true;
    let r;
    try{
      r = await WavrAPI.fetch("/api/pending-pairings/"+encodeURIComponent(id)+"/deny", {method: "POST"});
    }catch{}
    paDeny.disabled = false;
    if(r && r.ok) actionFeedback(paFb, true, null, WavrT("Declined."));
    else actionFeedback(paFb, false, WavrT("Couldn't decline — try again."));
    confirmBox.hidden = true; paCode.hidden = false; actionsBox.hidden = false;
    poll();
  };
  // Fix F5: no more pill to scroll-to-on-click -- the banner is already the first thing inside
  // <main> (visible on every tab, aria-live="assertive"), so the gear badge is a pure attention
  // cue rather than a navigation target.

  requests = Array.isArray(seed && seed.requests) ? seed.requests : [];
  coreFp = (typeof (seed && seed.cert_fingerprint) === "string") ? seed.cert_fingerprint : "";
  render();                                 // seed the view from the probe response we already read
  setInterval(poll, 2000);                  // devices can ask to pair at any time this tab is open
}
renderPairApprove();

