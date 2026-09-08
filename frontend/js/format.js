/* Dates, times and numbers, in the reader's own conventions.
 *
 * ## The problem this replaces
 *
 * Nine call sites hard-coded `"en-GB"`. Somebody in Lisbon, São Paulo or
 * Chicago read British day-month-year and a 24-hour clock, in an interface
 * otherwise written in their language — and there was no way to change it,
 * because the locale was not a setting, it was nine string literals.
 *
 * ## What this is, and what it deliberately is not
 *
 * It is formatting. It is not a translation system: the strings in this product
 * are still English, and pretending otherwise by shipping a locale switch that
 * only changes date separators would be worse than honest monolingualism.
 *
 * Externalising the strings is a real, separate piece of work. This is the half
 * that can be done correctly now and that a translation layer will need anyway.
 *
 * That separate piece of work has since been done — `i18n.js` is the other
 * half, and the two resolve the same locale from the same stored key so a
 * person cannot read Portuguese sentences over British dates. The paragraph
 * above is kept as written because `i18n.js` quotes it; what it describes is
 * history, not the current shape.
 *
 * ## A number inside a translated sentence still belongs here
 *
 * A translated sentence with a `{pct}` slot in it interpolates whatever
 * `String(n)` gives, and
 * `String(0.82)` is "0.82" in every locale on earth — an English decimal POINT,
 * dropped into the middle of a Portuguese sentence, in the one product that
 * went to the trouble of translating the sentence around it. `number()` and
 * `metres()` had ZERO callers for exactly that reason: they are easy to forget,
 * because forgetting them looks right to whoever is reading in English.
 *
 * So the rule is: a value that can carry a fraction — a confidence, a
 * reliability factor, a breathing rate, a distance — goes through `number()` or
 * `metres()` BEFORE it becomes a `{slot}`, never `toFixed` and never raw.
 *
 * ## Where the locale comes from
 *
 * The browser, which already knows — `navigator.language` is the answer the
 * person's operating system gave, and asking them again for something their
 * machine already knows is a setting nobody wants to fill in.
 *
 * An explicit override wins, for the case the browser is wrong: a shared panel
 * on a kitchen wall in a bilingual house, or a Core Panel whose OS is in one
 * language and whose household speaks another.
 *
 * ## Why it is loaded first
 *
 * The shell calls these while parsing, so this file must be evaluated before
 * any of it. That is also why it defines a plain global rather than a module:
 * the surrounding code is classic scripts sharing one scope, and a module here
 * would be a second loading model for no benefit.
 *
 * ## Load position: third, ahead of every module that renders a value
 *
 * `WavrFmt` is called from module top level, not only from event handlers,
 * so it must be installed before the first module that formats anything.
 * It is also precached by the service worker for the same reason the shell
 * is: an offline launch without it is a blank page, not a degraded one.
 */

(function () {
  "use strict";

  var OVERRIDE_KEY = "wavr.locale";

  function chosen() {
    // A stored choice wins; otherwise the browser's own answer; otherwise a
    // last resort that at least formats consistently.
    try {
      var saved = window.localStorage && localStorage.getItem(OVERRIDE_KEY);
      if (saved) return saved;
    } catch (e) {
      // Private windows and locked-down panels throw on storage access. That is
      // not an error worth surfacing — it just means no override.
    }
    return (navigator.languages && navigator.languages[0])
      || navigator.language || "en-GB";
  }

  var locale = chosen();

  function safe(fn, fallback) {
    // An invalid stored locale must not take a screen down. A wrong date format
    // is a nuisance; a blank dashboard because somebody typed "pt_BR" with an
    // underscore is a fault.
    try {
      return fn(locale);
    } catch (e) {
      try { return fn("en-GB"); } catch (e2) { return fallback; }
    }
  }

  function asDate(value) {
    var d = value instanceof Date ? value : new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  var Fmt = {
    /** The locale actually in use, so a screen can show what it resolved to. */
    locale: function () { return locale; },

    /** Override it. Persisted, because a panel on a wall is not reconfigured
     *  every morning. */
    setLocale: function (tag) {
      locale = tag || chosen();
      try {
        if (tag) localStorage.setItem(OVERRIDE_KEY, tag);
        else localStorage.removeItem(OVERRIDE_KEY);
      } catch (e) { /* storage unavailable: the choice lasts this session */ }
      return locale;
    },

    /** "14:32" — or whatever that is where the reader lives. */
    time: function (value, opts) {
      var d = asDate(value);
      if (!d) return "";
      var o = opts || { hour: "2-digit", minute: "2-digit" };
      return safe(function (l) { return d.toLocaleTimeString(l, o); }, "");
    },

    /** A short date. Never assumes day-before-month. */
    date: function (value, opts) {
      var d = asDate(value);
      if (!d) return "";
      var o = opts || { day: "2-digit", month: "2-digit", year: "2-digit" };
      return safe(function (l) { return d.toLocaleDateString(l, o); }, "");
    },

    dateTime: function (value, opts) {
      var d = asDate(value);
      if (!d) return "";
      var o = opts || { day: "2-digit", month: "2-digit",
                        hour: "2-digit", minute: "2-digit" };
      return safe(function (l) { return d.toLocaleString(l, o); }, "");
    },

    /** A number, grouped the way the reader groups numbers. */
    number: function (value, opts) {
      if (value == null || isNaN(value)) return "";
      return safe(function (l) {
        return new Intl.NumberFormat(l, opts || {}).format(value);
      }, String(value));
    },

    /**
     * A distance, in the reader's units.
     *
     * Wavr's canonical unit is the metre and stays the metre — every stored
     * coordinate, every polygon, every anchor. This converts for DISPLAY only,
     * and only when the locale is one that genuinely uses feet, because
     * silently mixing units is how a floor plan ends up three times too big.
     */
    metres: function (value, opts) {
      if (value == null || isNaN(value)) return "";
      var imperial = /^en-US\b|^en-LR\b|^my\b/i.test(locale);
      if (!imperial) {
        return Fmt.number(value, opts || { maximumFractionDigits: 1 }) + " m";
      }
      return Fmt.number(value * 3.28084,
                        opts || { maximumFractionDigits: 1 }) + " ft";
    },
  };

  window.WavrFmt = Fmt;
})();

// --------------------------------------------------------------------------
// Modality names, in the reader's language.
//
// It lives HERE, in the third script the shell loads, because it is called
// from the ninth and the seventeenth. It used to live in radar.js — the
// twenty-sixth — so those two threw `modalityLabel is not defined` on every
// load, and three other callers had each grown a `typeof` guard around it.
// One producer, ahead of its consumers, beats four workarounds.
// Every modality the wire can name, in the reader's language,
// falling back to the raw name for anything not yet mapped.
// A FUNCTION, not a table, and for the reason `kinds()` and `connDesc()` are
// functions too: `WavrT` answers in whatever language is active when it is
// CALLED, and a table built once at parse time would still be in the old
// language after somebody switches. More importantly the literals live at a
// `WavrT(...)` call site here, where the catalogue check can see them — inside
// a table they were invisible, so all seven rendered English in every language.
// The KEYS are the wire's own modality names and never translate.
function modalityLabel(name){
  switch(name){
    case "wifi_csi": return WavrT("Wi-Fi");
    case "ruview":   return WavrT("Wi-Fi CSI");
    case "camera":   return WavrT("Camera");
    case "network":  return WavrT("Network");
    case "sim":      return WavrT("Simulated");
    case "mmwave":   return WavrT("Radar");
    case "ble":      return WavrT("Bluetooth");
    // `wavr.nodes.SENSOR_MODALITY` maps a PIR sensor node to modality "pir" and
    // an unrecognised/generic presence node to "node" — both real wire values
    // this switch never had a case for, so a PIR node's room-card "why" row
    // read the raw token "Pir" (capitalize CSS on an un-translated fallback).
    // Same word discoveries.js already uses for the identical sensor type
    // ("Motion sensor (PIR)"), and the same word nodes.js already falls back
    // to for an unnamed node (`WavrT("node")`, translated "sensor") — reusing
    // both rather than inventing a third spelling of either.
    case "pir":      return WavrT("Motion sensor (PIR)");
    case "node":     return WavrT("node");
    default:         return String(name || "");
  }
}

