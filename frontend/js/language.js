/* The language control, and the one repaint everything else hangs off.
 *
 * Separate from `i18n.js` on purpose: that file is the mechanism and has to
 * load before the shell's first `t(...)` call, while this is a screen and can
 * load with the other feature modules. Keeping them apart means the mechanism
 * has no DOM dependencies at all.
 *
 * ## Only languages this build can actually render
 *
 * The list comes from `WavrI18n.available()`, which is derived from the
 * catalogues that registered themselves. A build shipped without the
 * Portuguese catalogue offers one choice and cannot offer a language it would
 * then fail to render — the failure mode a hardcoded `<option>` list has.
 *
 * ## Changing the language RELOADS the page, and that is the design
 *
 * Half of this product's text is built in JavaScript, not markup, so
 * translating the DOM is only half the job. This file used to claim "every
 * module that renders text listens once and re-renders". Nothing did: two
 * hooks were called by hand here and the other forty-two modules were left to
 * "their own data tick", which most of them do not have. Switching to
 * Portuguese repainted the chrome and left Connectors, the Assistant, the
 * device catalogue, the routines builder and every settings panel in English
 * until something unrelated happened to re-render them. Two hundred strings,
 * measured in a browser.
 *
 * A registry was the other option: one `onLocale(render)` per module. Forty-two
 * registrations is forty-two chances to forget one permanently, and the thing
 * forgotten is invisible — a panel in the wrong language reads as a missing
 * translation, not as a missing subscription.
 *
 * So the page reloads. Every string repaints because every module runs again,
 * and there is no discipline left to maintain. Changing language is a rare and
 * deliberate act; reloading after one is what most software does. The hash is
 * set first, so the reader lands back on the screen they were reading.
 *
 * `wavr:locale` still fires, and a change from ELSEWHERE — a companion, a
 * future system bridge — repaints in place instead of reloading. A page that
 * reloads itself because something else changed is hostile, and the person may
 * be mid-sentence in a form.
 *
 * ## Load position: LAST
 *
 * The selector is filled from `WavrI18n.available()`, which lists the
 * catalogues registered so far. Loaded earlier it renders a menu with one
 * language in it and reports nothing wrong — the failure is a control that
 * looks finished and offers less than the product has.
 */

(function () {
  "use strict";

  // The endonym: a person looking for their own language recognises it written
  // in that language, not translated into the one they cannot read.
  var NAMES = {
    en: "English",
    pt: "Português (Brasil)",
  };

  function el(id) { return document.getElementById(id); }

  function fill() {
    var sel = el("langSelect");
    if (!sel || !window.WavrI18n) return;
    var active = WavrI18n.base();
    sel.textContent = "";
    WavrI18n.available().forEach(function (tag) {
      var o = document.createElement("option");
      o.value = tag;
      o.textContent = NAMES[tag] || tag;
      if (tag === active) o.selected = true;
      sel.appendChild(o);
    });
    // One choice is not a choice. Say so rather than showing a dropdown that
    // does nothing when opened.
    sel.disabled = WavrI18n.available().length < 2;
  }

  /* Reload, landing on the screen the reader was already looking at.
   *
   * `runtime.js` routes `#tab-*` and `#gearSec*` fragments on load, so setting
   * the hash first turns a reload from "you are back at the dashboard" into
   * "the page changed language". The language control lives in Settings, so
   * that is where the fragment points.
   *
   * Skipped while first-run setup is open: the wizard holds unsaved answers in
   * memory, and reloading would throw away what somebody just typed to change
   * the language they are being asked the questions in.
   */
  function reloadIntoTheSameScreen() {
    var wiz = document.getElementById("setupWizard");
    if (wiz && !wiz.hidden && wiz.classList.contains("show")) {
      repaintWhatWeCan();
      return;
    }
    // Where to come back to.
    //
    // This asked for `.gear-section.active`, and no element in the product has
    // ever carried that class — the sections are `.settings-section` and the
    // rail buttons are `.gear-rail-item`. The selector matched nothing, the
    // hash was never set, and switching language from inside Settings reloaded
    // to the landing screen. The reader lost their place every single time, on
    // the one control whose whole job is to change the page under them gently.
    //
    // The rail BUTTON is the right thing to read, not the section: `.active` is
    // meaningful on a section only under the panel media query, and at desktop
    // widths several carry it at once — one of them straight from the markup.
    // `aria-pressed` is written by `showGearSection` and is true for exactly
    // one button whenever the overlay has a current section.
    try {
      var overlay = document.getElementById("gearOverlay");
      if (overlay && !overlay.hidden) {
        var pressed = document.querySelector(
          '#gearRail .gear-rail-item[aria-pressed="true"]');
        var id = pressed && pressed.getAttribute("data-section");
        if (id) location.hash = id;
      }
    } catch (e) { /* no overlay open: reload to wherever the hash points */ }
    location.reload();
  }

  /* The in-place repaint, for a locale change this control did not make.
   *
   * Deliberately partial, and named so nobody mistakes it for complete: it
   * repaints the markup (`WavrI18n.apply` has already run) and the two panels
   * with no data tick of their own. Everything else waits for its next render.
   * The complete answer is the reload above; this is what is safe to do to
   * somebody who did not ask for it.
   */
  function repaintWhatWeCan() {
    fill();
    try {
      if (window.__wavrRenderTrust) window.__wavrRenderTrust();
      if (window.__wavrRenderPrivacyData) window.__wavrRenderPrivacyData();
    } catch (e) { /* a panel that is not mounted is not an error */ }
  }

  function wire() {
    var sel = el("langSelect");
    if (!sel || !window.WavrI18n) return;
    fill();
    sel.addEventListener("change", function () {
      WavrI18n.setLocale(sel.value);      // persists, and fires wavr:locale
      // The selector itself is markup, so `apply` already relabelled it; the
      // option list is built here and has to be rebuilt with it. Done before
      // the reload so the control reads correctly for the moment it is still
      // on screen.
      fill();
      reloadIntoTheSameScreen();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  // A locale change from somewhere that is NOT this control — a companion, a
  // future system bridge, a test. Repaint in place; do not reload a page the
  // person did not ask to have reloaded.
  window.addEventListener("wavr:locale", repaintWhatWeCan);
})();
