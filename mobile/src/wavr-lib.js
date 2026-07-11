/* Wavr pure-logic helpers — DOM-free, native-free, so node --test can exercise them and the
 * shim can consume them as window.WavrLib. No console, no side effects. */
(function(root){
  "use strict";
  function isPort(p){ var s = String(p); return /^\d{1,5}$/.test(s) && (+s) >= 1 && (+s) <= 65535; }
  function isHost(h){
    if(typeof h !== "string" || !h) return false;
    return /^(\d{1,3})(\.\d{1,3}){3}$/.test(h) || /^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}$/.test(h);
  }
  // Validate one zeroconf/NSD result. Accept only role=core with a usable host+port.
  // Accepts either {hostname|host|ipv4Addresses[0]} and {txtRecord|txt} shapes defensively.
  // PREFER the resolved numeric IPv4 over the SRV `hostname`: capacitor-zeroconf reports
  // `hostname = ServiceInfo.getServer()`, an mDNS `.local.` name (e.g. "android-abc.local.")
  // that the Android native TLS/DNS resolver does NOT resolve — so basing the connection on it
  // yields a Core that is discovered but unreachable. The jmDNS "resolved" event always carries
  // the concrete `ipv4Addresses`, which IS routable. Fall back to host/hostname only if absent.
  function parseCoreService(r){
    if(!r || typeof r !== "object") return null;
    var txt = r.txtRecord || r.txt || {};
    if(String(txt.role || "").toLowerCase() !== "core") return null;
    var host = (Array.isArray(r.ipv4Addresses) && r.ipv4Addresses[0]) || r.host || r.hostname || "";
    var port = r.port;
    if(!isHost(host) || !isPort(port)) return null;
    var name = (typeof r.name === "string" && r.name.trim()) ? r.name.trim() : "Wavr Core";
    return { name: name, host: host, port: +port };
  }
  // The single source of truth for what each consent level means for connection + presence.
  function consentToActions(level){
    if(level === "red")    return { attached: false, presence: "delete",   contribution: "none" };
    if(level === "yellow") return { attached: true,  presence: "register", contribution: "limited" };
    return { attached: true, presence: "register", contribution: "full" };   // green / default
  }
  var api = { isHost: isHost, isPort: isPort, parseCoreService: parseCoreService, consentToActions: consentToActions };
  if(typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WavrLib = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
