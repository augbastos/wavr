// ==========================================================================
// whats-new.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================

// ============================================================================
// What's New (release notes). One ordered list of UI releases. The FIRST time
// the app opens on a newer WAVR_APP_VERSION than the one last acknowledged
// (localStorage), the Core panel shows a takeover card OVER the lock screen;
// OK acknowledges it (never reappears for that version) and reveals the still-
// locked panel underneath. It stays permanently re-readable in Settings > System.
// WAVR_APP_VERSION is the version this list is CURRENT for, and it tracks the
// shipped product version -- `wavr.__version__` / `backend/pyproject.toml`, the
// same string About shows. It used to be a free-standing "UI-release marker,
// independent of the backend semver", and independent is exactly what it became:
// it sat at 0.2.0 through two minor versions of shipped, user-visible work,
// because nothing anywhere said the two had to move together. They do now, and
// `test_docs_match_the_code.py` fails when they disagree -- so bumping the
// product version without writing what changed is a red test, not a silent gap.
//
// Newest entry FIRST. Dates are ISO `YYYY-MM-DD` and are the day the notes were
// written; a date must never be in the future (the same test checks).
// ============================================================================
window.WAVR_APP_VERSION = "0.4.0";
window.WAVR_WHATS_NEW = [
  // 0.4.0: what a household can SEE. The connector reach
  // vocabulary, the pinned test addresses and the offline producer
  // are real work in this release and are not release notes.
  { version: "0.4.0", date: "2026-09-11", items: [
    WavrT("🏡 Wavr opens on your Space. The map is the first thing on the screen now, at the size it deserves, instead of sitting under three panels of settings."),
    WavrT("🔍 Tap a room and it opens beside the map, not below it — which sensor sees it, how sure each one is, and how long ago. The Space stays on screen while you read."),
    WavrT("📴 A Space Wavr has not mapped yet says so, and says what to do about it, instead of showing an empty grid."),
    WavrT("⚠️ Amber is kept for things that actually want you. Devices Wavr merely noticed are listed calmly, and each one now says which device it is instead of six identical lines."),
    WavrT("📺 The wall panel answers from across the room. What is happening in your Space is the largest thing on it; the clock is not."),
    WavrT("📱 The phone app speaks your language. Every screen in the "
          + "companion — finding your hub, checking its certificate, choosing "
          + "what the device does — follows the language you picked, instead of "
          + "staying in English."),
    WavrT("🤔 When two sensors disagree about a room, Wavr says it is less "
          + "sure. A camera that can see the room and reports nobody there now "
          + "lowers the confidence instead of being recorded and ignored."),
    WavrT("⏰ A sensor with the wrong clock is no longer believed over one "
          + "with the right clock. A device whose time is running ahead used to "
          + "look like the freshest thing in the house for as long as it was "
          + "wrong."),
    WavrT("🏠 Home Assistant on your own network is no longer listed as "
          + "something that leaves it. What leaves your Space is now decided by "
          + "where a connection actually goes, not by how its description is "
          + "worded."),
  ] },
  // 0.3.0 covers everything after 0.2.0: the modularisation of the shell, two
  // languages, the four-destination navigation, and the audit that followed
  // them (`_local/pending-commits/` units 25-28 are the written record). The
  // items below are the parts a household can SEE -- the 44-module split and
  // the SDK/protocol work are real and are not release notes.
  { version: "0.3.0", date: "2026-09-06", items: [
    WavrT("🌐 Wavr speaks Portuguese. Pick your language in Settings and every screen follows."),
    WavrT("🧭 Four places instead of ten: Space, Activity, Routines and Manage. Nothing was taken away — the setup and diagnostic screens moved under Manage."),
    WavrT("📱 The navigation fits a phone now: it wraps instead of running off the side, and every button is big enough to tap."),
    WavrT("📡 When the Core stops answering, the screen says so. It used to keep showing the last good reading as if nothing had happened."),
    WavrT("🛡️ Approving a new device keeps the access level you picked. The waiting list used to quietly reset it to the lowest one while you were still reading."),
    WavrT("📍 Named places: give a spot inside a room a name — a desk, a doorway, a workbench — under Manage, and apps built on Wavr can point at it."),
    WavrT("🗺️ A fresh install starts with your own floor plan instead of a sample one, and setup keeps the first room you name."),
    WavrT("👁️ A room that nothing is watching now says so, instead of looking exactly like a room that is simply empty."),
    WavrT("🏢 Wavr no longer assumes where it is. A workshop, a shop or an office is described in its own words."),
  ] },
  { version: "0.2.0", date: "2026-07-10", items: [
    WavrT("🏠 An easier-to-read home screen: rooms with people show on the right, and the house map opens when you tap it."),
    WavrT("🔒 You can now leave the panel with no passcode, if you prefer."),
    WavrT("🔌 The connections screen is tidier, and clearly shows what does and doesn't leave your home."),
    WavrT("🤝 Pairing two Wavr devices is safer now: just type the code shown on the other one's screen."),
  ] },
];
window.__wavrWhatsNewFor = function(v){
  var list = window.WAVR_WHATS_NEW || [];
  for(var i = 0; i < list.length; i++){ if(list[i].version === v) return list[i]; }
  return null;
};
// Fill a <ul> with an entry's items via textContent only (never HTML — these are
// static local strings, but the discipline matches every other list in this file).
window.__wavrRenderWhatsNewItems = function(ul, entry){
  if(!ul) return;
  ul.textContent = "";
  var items = (entry && entry.items) || [];
  for(var i = 0; i < items.length; i++){
    var li = document.createElement("li");
    li.textContent = String(items[i]);
    ul.appendChild(li);
  }
};
// Settings > System "What's New" tile: always shows the current version's notes,
// regardless of the seen-state. Runs in every mode (dashboard + panel).
(function initWhatsNewSettings(){
  function fill(){
    var ver = document.getElementById("whatsNewSetVer");
    var ul  = document.getElementById("whatsNewSetList");
    var entry = window.__wavrWhatsNewFor(window.WAVR_APP_VERSION);
    // Through the formatter, like the Core Panel's copy of this line: the
    // entry's `date` is an ISO day, and rendered raw it shows a Brazilian
    // reader 2026-09-06 in the middle of a Portuguese sentence.
    if(ver) ver.textContent = entry
      ? WavrT("Version {version} · {date}",
              {version: entry.version, date: WavrFmt.date(entry.date) || entry.date})
      : "";
    window.__wavrRenderWhatsNewItems(ul, entry);
  }
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", fill);
  else fill();
})();

