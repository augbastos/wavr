/* The words, in the reader's own language.
 *
 * ## What this is, and what `format.js` already did
 *
 * `WavrFmt` localises dates, times and numbers. Its own docstring is blunt
 * about the limit: "It is not a translation system: the strings in this
 * product are still English, and pretending otherwise by shipping a locale
 * switch that only changes date separators would be worse than honest
 * monolingualism." This is the other half. The two share one locale, resolved
 * once, so a person cannot end up reading Portuguese sentences over British
 * dates.
 *
 * ## The key IS the English string
 *
 * There is no `space.title.heading` here. A key like that has to be looked up
 * before anybody can review a change, it drifts from the text it names, and a
 * missing one renders as an identifier in front of a household. So the source
 * English sentence is the key:
 *
 *     WavrT("Who's home")        -> "Quem está em casa"
 *     el.dataset.i18n = "Who's home"
 *
 * A missing translation therefore falls back to correct English rather than to
 * `undefined` or to a dotted path. That is the single most important property
 * of this design: the worst failure is a screen that is partly in the other
 * language, never a screen that is broken.
 *
 * ## Interpolation and plurals
 *
 *     WavrT("{n} rooms", {n: 3})
 *     WavrT("{n} device|{n} devices", {n: 1})   -> singular before the pipe
 *
 * Plural selection is deliberately two-form. English and Portuguese both need
 * exactly two, and a general CLDR plural engine for two languages that agree
 * on the rule is machinery nobody here can review.
 *
 * ## Applying it
 *
 * `WavrI18n.apply(root)` walks `[data-i18n]` (text), `[data-i18n-attr]`
 * (attributes, `attr:Source text` pairs separated by `|`) and translates in
 * place. It is idempotent: the ORIGINAL English is kept in the dataset, so
 * switching language twice does not translate a translation.
 *
 * ## Missing keys are visible, on purpose, in one place
 *
 * Every lookup that misses is recorded. `WavrI18n.missing()` returns them, and
 * a test asserts the set is empty for the shipped locales — because "we have
 * Portuguese" is exactly the kind of claim that quietly stops being true one
 * string at a time.
 *
 * ## Load position: FIRST, before every other script
 *
 * Several modules call `WavrT(...)` at parse time, while their own
 * `<script>` is still executing. A translation runtime that arrives after
 * its first caller does not throw — `WavrT` is simply undefined, the call
 * site raises, and the module below it never finishes. `test_shell_modules
 * .py` pins this to tag one.
 */

(function () {
  "use strict";

  var STORE_KEY = "wavr.locale";
  var CATALOGUES = {};          // "pt" -> {english: translated}
  var missing = {};             // english -> true, for the audit

  // The source language. Strings are authored in it, so it needs no catalogue.
  var SOURCE = "en";

  function baseTag(tag) {
    return String(tag || "").toLowerCase().split(/[-_]/)[0];
  }

  function stored() {
    try {
      return (window.localStorage && localStorage.getItem(STORE_KEY)) || "";
    } catch (e) {
      // Private windows and locked-down panels throw on storage access; that
      // is not an error, it just means no override.
      return "";
    }
  }

  function resolve() {
    // Exactly the resolution `WavrFmt` uses — a stored choice, then what the
    // operating system already said, then a last resort. Two different answers
    // to "what language is this person reading" is how a product ends up with
    // Portuguese words and British dates.
    var saved = stored();
    if (saved) return saved;
    return (navigator.languages && navigator.languages[0])
      || navigator.language || "en-GB";
  }

  var current = resolve();

  function catalogue() {
    return CATALOGUES[baseTag(current)] || null;
  }

  function lookup(text) {
    var cat = catalogue();
    if (!cat) return text;                       // source language, or unknown
    if (Object.prototype.hasOwnProperty.call(cat, text)) return cat[text];
    // Recorded rather than guessed at. A key that misses renders as correct
    // English; the audit is how anybody finds out it did.
    missing[text] = true;
    return text;
  }

  function interpolate(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(vars, name)
        ? String(vars[name]) : whole;
    });
  }

  function t(text, vars) {
    if (text === null || text === undefined) return "";
    var out = lookup(String(text));
    if (out.indexOf("|") !== -1 && vars &&
        Object.prototype.hasOwnProperty.call(vars, "n")) {
      var forms = out.split("|");
      out = (Number(vars.n) === 1 ? forms[0] : forms[1]) || forms[0];
    }
    return interpolate(out, vars);
  }

  // -- Applying to a tree ------------------------------------------------------

  function normalise(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function sourceText(el) {
    // The ORIGINAL English, kept on the element the first time it is seen.
    // Without this, applying a second locale would translate the previous
    // translation and every switch after the first would be a miss.
    if (!el.dataset.i18nSrc) el.dataset.i18nSrc = el.textContent;
    return el.dataset.i18nSrc;
  }

  function applyText(el) {
    // Whitespace is COLLAPSED before the lookup, because markup wraps.
    //
    //     <p data-i18n>Building an application on top of Wavr. Nothing here
    //       is needed to use it — turn the switch on…</p>
    //
    // `textContent` returns that with its newline and indentation intact,
    // while the catalogue is keyed on the sentence a person reads. Trimming
    // the ends is not enough; a browser collapses runs of whitespace when it
    // renders, so collapsing them here is what makes the key match what the
    // screen shows. Sixteen strings fell back to English on this alone.
    var key = normalise(el.dataset.i18n || sourceText(el));
    el.textContent = t(key);
  }

  function applyAttrs(el) {
    // `data-i18n-attr="aria-label:Settings|title:Open settings"`
    var spec = el.dataset.i18nAttr;
    if (!spec) return;
    if (!el.dataset.i18nAttrSrc) el.dataset.i18nAttrSrc = spec;
    el.dataset.i18nAttrSrc.split("|").forEach(function (pair) {
      var at = pair.indexOf(":");
      if (at === -1) return;
      var name = pair.slice(0, at).trim();
      var text = pair.slice(at + 1).trim();
      if (name && text) el.setAttribute(name, t(normalise(text)));
    });
  }

  function apply(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach(applyText);
    scope.querySelectorAll("[data-i18n-attr]").forEach(applyAttrs);
    var html = document.documentElement;
    if (html) html.setAttribute("lang", baseTag(current) || "en");
  }

  // -- The public surface -------------------------------------------------------

  var I18n = {
    /** Translate one source string. */
    t: t,

    /** The active locale tag, exactly as resolved. */
    locale: function () { return current; },

    /** The two-letter base, which is what catalogues are keyed by. */
    base: function () { return baseTag(current); },

    /** Every locale this build can actually render, source language first. */
    available: function () {
      return [SOURCE].concat(Object.keys(CATALOGUES).filter(function (k) {
        return k !== SOURCE;
      }));
    },

    /** Register a catalogue. Called by `locale-*.js`, never by feature code. */
    register: function (tag, table) {
      CATALOGUES[baseTag(tag)] = table || {};
    },

    /**
     * Switch language and repaint. Persists the choice, because a person who
     * sets their language on a wall panel should not have to set it again
     * every morning.
     */
    setLocale: function (tag) {
      current = tag || resolve();
      try {
        if (window.localStorage) localStorage.setItem(STORE_KEY, current);
      } catch (e) { /* see `stored` */ }
      apply(document);
      // Everything that renders text from JS re-renders from here rather than
      // each module polling a locale. One event, one repaint.
      try {
        window.dispatchEvent(new CustomEvent("wavr:locale", {
          detail: { locale: current },
        }));
      } catch (e) { /* very old engines: the DOM half above still applied */ }
      return current;
    },

    /** Re-translate a subtree — for markup a module just built. */
    apply: apply,

    /** Source strings that had no translation in the active catalogue. */
    missing: function () { return Object.keys(missing).sort(); },

    /** For tests: forget what has been recorded so far. */
    resetMissing: function () { missing = {}; },
  };

  window.WavrI18n = I18n;
  // `WavrT`, not `t`.
  //
  // A one-letter global is unusable in a file that shares a single scope
  // across eighteen thousand lines: this shell declares a local `t` in
  // thirty-five places — loop variables, parameters, `var t = tag` — and every
  // one of them shadows it, so a call inside any of those scopes throws
  // `t is not a function`. That is not a hypothetical; it is what the first
  // version of this did, and only a browser test found it, because the call
  // site reads perfectly.
  //
  // Loaded before every other script because the shell's own top-level code
  // calls this while parsing.
  window.WavrT = t;

  /* Snapshot each marked element's ENGLISH the moment it is parsed.
   *
   * `sourceText` records the original the first time it sees an element, so a
   * second locale translates the ORIGINAL rather than the previous
   * translation. That works — as long as the first time it sees the element is
   * before anything else has written to it.
   *
   * It is not. A shell script running at PARSE time does
   * `el("swTitle").textContent = WavrT("Loading…")` long before
   * `DOMContentLoaded`, so `apply` later meets an element whose text is
   * already "Carregando…", records THAT as the source, and looks it up. The
   * lookup misses, the screen stays correct — and the household's own language
   * quietly fills `missing()`, which is the surface used to audit whether this
   * product is fully translated. A measurement polluted by the thing it
   * measures.
   *
   * A MutationObserver closes it exactly. Observer callbacks run at the
   * microtask checkpoint BETWEEN script blocks, so an element is captured
   * after the parser inserts it and before the next `<script>` can touch it.
   * Disconnected at `DOMContentLoaded`: after that no parse-time writer is
   * left, `apply` owns the marked elements, and an observer running over the
   * whole document for the life of a kiosk page is a cost with no buyer.
   */
  function snapshot(root) {
    if (!root || root.nodeType !== 1) return;
    if (root.hasAttribute && root.hasAttribute("data-i18n")
        && !root.dataset.i18nSrc) {
      root.dataset.i18nSrc = root.textContent;
    }
    if (root.querySelectorAll) {
      root.querySelectorAll("[data-i18n]").forEach(function (el) {
        if (!el.dataset.i18nSrc) el.dataset.i18nSrc = el.textContent;
      });
    }
  }

  if (typeof MutationObserver === "function") {
    var watcher = new MutationObserver(function (records) {
      records.forEach(function (r) {
        Array.prototype.forEach.call(r.addedNodes, snapshot);
      });
    });
    try {
      watcher.observe(document.documentElement || document,
                      { childList: true, subtree: true });
      document.addEventListener("DOMContentLoaded", function () {
        snapshot(document.body);        // anything the last batch missed
        watcher.disconnect();
      });
    } catch (e) { /* no document yet: `apply` below still does the right thing */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { apply(document); });
  } else {
    apply(document);
  }
})();
