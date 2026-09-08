// ==========================================================================
// core-lock.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Core Panel lock admin (Plano A / live only) ----
// Sets/changes the PIN that gates the ambient touchscreen build's (?core / window.WAVR_CORE)
// wake-to-dashboard step — see the lock module in that gated script block near the end of this
// file. This dashboard only ever WRITES the PIN (POST /api/core/pin, loopback-admin, same
// X-Wavr-Local guard as every other write here); the touchscreen itself only ever VERIFIES
// against it (POST /api/core/pin/verify) and never calls this route.
async function renderCoreLockAdmin(){
  const tile = document.getElementById("coreLockPanel");
  if(!tile) return;
  if(MODE!=="live") return;   // admin-only, loopback; never in companion/demo
  tile.hidden = false;
  const statusEl = document.getElementById("coreLockAdminStatus");
  const form = document.getElementById("coreLockAdminForm");
  const pinInput = document.getElementById("coreLockAdminPin");
  const fb = document.getElementById("coreLockAdminFb");
  // Fix #3 / finding #19 (lock-mode chooser): [No lock] / [PIN] are the two real,
  // mutually-exclusive backend-controlled states. "Device unlock" is surfaced separately as
  // a non-interactive hint line (#lockModeBiomNote), shown only when the native bridge
  // reports biometric hardware — never as a selectable third mode, because the ambient
  // wake() gate already tries biometric BEFORE any PIN whenever WavrNative.hasBiometric()
  // is true (see the lock module near the end of this file), regardless of the No-lock/PIN
  // choice here; there is no backend state for a tap on "Device unlock" to set, so
  // presenting it as a selectable mode would show a mode that can't work.
  const modeNoneBtn = document.getElementById("lockModeNone");
  const modePinBtn  = document.getElementById("lockModePin");
  const modeBiomNote = document.getElementById("lockModeBiomNote");
  const hasBiometric = !!(window.WavrNative && typeof window.WavrNative.hasBiometric === "function"
                          && window.WavrNative.hasBiometric());
  if(modeBiomNote) modeBiomNote.hidden = !hasBiometric;
  // Finding #14: track the last CONFIRMED backend state so the chooser can't keep showing
  // "PIN" selected (or fire a spurious DELETE) once the user taps a button without ever
  // submitting/completing the change — only refresh() (GET /api/core/pin/status) is allowed
  // to assert this is what the backend actually has.
  let backendPinSet = false;
  function setMode(mode){
    [modeNoneBtn, modePinBtn].forEach(b=>{
      if(!b) return;
      const active = b.getAttribute("data-mode") === mode;
      b.classList.toggle("on", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if(form) form.hidden = (mode !== "pin");
  }
  async function refresh(){
    let s;
    try{ s = await (await WavrAPI.fetch("/api/core/pin/status")).json(); }
    catch{ if(statusEl) statusEl.textContent = WavrT("couldn't reach the hub — try again"); return; }
    const pinSet = !!(s && s.pin_set);
    backendPinSet = pinSet;
    if(statusEl) statusEl.textContent = pinSet
      ? WavrT("A PIN is set for the Core Panel touchscreen.")
      : WavrT("No PIN set — the Core Panel touchscreen wakes straight to the dashboard.");
    setMode(pinSet ? "pin" : "none");   // default-selected from GET /api/core/pin/status
  }
  if(modeNoneBtn) modeNoneBtn.onclick = async ()=>{
    if(!backendPinSet){ setMode("none"); return; }   // already unlocked — no DELETE needed, no spurious "✓ lock removed"
    modeNoneBtn.disabled = true;
    let r;
    try{ r = await WavrAPI.fetch("/api/core/pin", {method: "DELETE"}); }catch{}
    modeNoneBtn.disabled = false;
    if(r && r.ok){ actionFeedback(fb, true, null, "✓ lock removed"); refresh(); }
    else actionFeedback(fb, false, WavrT("couldn't remove the lock — try again"));
  };
  if(modePinBtn) modePinBtn.onclick = ()=>{
    setMode("pin");
    pinInput?.focus();
    // Selecting PIN client-side doesn't set one — keep the status line honest about the
    // pending transition instead of contradicting reality until Save actually completes it.
    if(!backendPinSet && statusEl) statusEl.textContent = WavrT("Enter a 4–12 digit PIN and save it to turn the lock on.");
  };
  form.onsubmit = async (e)=>{
    e.preventDefault();
    const pin = pinInput.value;
    if(!/^[0-9]{4,12}$/.test(pin)){ actionFeedback(fb, false, WavrT("PIN must be 4–12 digits")); return; }
    let r;
    try{
      r = await WavrAPI.fetch("/api/core/pin", {method: "POST", json: {pin}});
    }catch{}
    if(r && r.ok){
      form.reset();
      actionFeedback(fb, true, null, "✓ PIN saved");
      refresh();
    } else {
      actionFeedback(fb, false, WavrT("couldn't save the PIN — try again"));
    }
  };
  // Core camera: on-device MJPEG streamer (native launcher bridge) + the core-cam
  // source that reads it. Native bridge only exists inside the Wavr launcher.
  const camBtn = document.getElementById("coreCamBtn");
  const camStatus = document.getElementById("coreCamStatus");
  function camState(){
    if(!(window.WavrNative && WavrNative.getCameraState)) return null;
    try{ return JSON.parse(WavrNative.getCameraState()); }catch{ return null; }
  }
  function refreshCam(){
    if(!camStatus) return;
    if(!(window.WavrNative && WavrNative.setCamera)){ camStatus.textContent=WavrT("camera: launcher only"); if(camBtn) camBtn.disabled=true; return; }
    const st=camState();
    camStatus.textContent = "camera: " + (st&&st.on ? ("on ("+(st.lens||"?")+")") : "off");
  }
  if(camBtn) camBtn.onclick = async ()=>{
    const st=camState(); const turnOn = !(st && st.on);
    if(window.WavrNative && WavrNative.setCamera) WavrNative.setCamera(turnOn);
    try{ await WavrAPI.fetch("/api/sources/core-cam/toggle", {method: "POST"}); }catch{}
    setTimeout(refreshCam, 700);
  };
  refreshCam();
  refresh();
}
renderCoreLockAdmin();

