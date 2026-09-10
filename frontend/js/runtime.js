/* Runtime presence — the always-visible answer to "is Wavr actually working?".
 *
 * NO INVISIBLE SUCCESS. A household that believes their home is being watched
 * over, over a Core that died three days ago, is the worst failure this product
 * can have. Every other screen answers a question somebody thought to ask; this
 * one answers the question nobody thinks to ask until it is too late.
 *
 * ## Two rules, and everything here follows from them
 *
 * **1. The Core decides, this renders.** The state, the words and the severity
 * all come from `/api/runtime` (`wavr/runtime_status.py`). This file computes no
 * health of its own. Two implementations of "is it healthy" eventually disagree
 * in front of somebody with no way to tell which is right — and the tray, the
 * header and the Android notification are three of them.
 *
 * **2. No answer is a bad answer, never the previous one.** A failed request
 * renders "not responding". A header that stays green because the last poll
 * succeeded is precisely the invisible failure this exists to prevent, and it
 * is the easy mistake: leaving the last good state up looks like resilience.
 *
 * ## Why it is a button
 *
 * Because the chip is a summary, and a summary that cannot be opened is a claim
 * a person has to take on trust. Clicking goes to the detail — the same detail,
 * not a second rendering of it.
 *
 * ## Load position: after the modules whose hooks it wraps
 *
 * The chip chains onto `window.__wavrRS` and the reconnection state that
 * `core-connection.js` installs, by wrapping whatever handler is already
 * there. Moved above them, the wrap silently attaches to nothing: no
 * error, no chip, and the one screen whose absence is itself a silent
 * failure is the screen that goes quiet.
 */

(function () {
  "use strict";

  var POLL_MS = 10000;
  // Longer than the poll, so one slow response does not flap the chip; short
  // enough that a stopped Core is visible within about half a minute.
  var STALE_MS = 35000;
  // A liveness request that has not answered in this long has answered
  // nothing. Deliberately shorter than STALE_MS so a dark host produces a
  // failed request BEFORE the watchdog has to step in — the tick's own
  // rendering is the better path, and the watchdog is the backstop for the
  // case where even the abort does not fire.
  var REQ_TIMEOUT_MS = 8000;
  // Independent of POLL_MS on purpose: this exists precisely for the case
  // where the poll is not completing.
  var WATCHDOG_MS = 5000;

  var el = function (id) { return document.getElementById(id); };

  // -- Which Space is this? ----------------------------------------------------
  //
  // Naming a Space and then never seeing the name is half a feature. It goes
  // beside the wordmark and into the window title, so a household running two
  // Cores can tell one browser tab from the other.
  //
  // THE ONE WRITER, and it lives here rather than in the setup wizard because
  // that wizard returns early for every mode but "live". A phone paired to two
  // Cores, and the Core's own kiosk face, would both have shown a room map with
  // no statement of which home it belongs to -- which is how somebody dismisses
  // an intrusion alert for the wrong house.
  //
  // Three callers: the wizard's boot probe (loopback), the shell's /api/status
  // poll (everybody, because that route carries presence:read), and the rename
  // form. All three used to write the slot themselves, and the rename path set
  // the slot but not the title, so a renamed Space kept its old name in the
  // browser tab until somebody reloaded.
  function showSpaceName(space) {
    if (!space || !space.name) return;
    // The slot carries no `data-i18n`: what lands in it is a name somebody
    // typed, and running a person's Space name through a translation
    // catalogue is a category error. Its PLACEHOLDER is copy, and is
    // translated where it is set, not here.
    var slot = el("brandSpace");
    // textContent, never innerHTML: the Space name is operator input and this
    // is the one place it lands in the shell's own chrome.
    if (slot) {
      // The slot stops being copy the instant it holds a name. Dropping the
      // mark is what keeps a repaint from looking a person's Space name up in
      // the catalogue — and from recording it as a "missing translation",
      // which is how a household's private name would end up in an audit.
      slot.removeAttribute("data-i18n");
      delete slot.dataset.i18nSrc;
      slot.textContent = space.name;
    }
    document.title = space.name + " \u2014 Wavr";
  }
  window.__wavrShowSpace = showSpaceName;

  // -- Getting to a surface ----------------------------------------------------
  //
  // ONE function, because three callers had their own copy and two of them were
  // wrong: the chip queried an attribute the dashboard does not use, and the
  // tray navigated to `#tab-novos` on a page with no hash routing at all — so
  // the tray's "Needs attention" item reloaded the dashboard on whatever tab was
  // default and looked, to the person who clicked it, like nothing happened.
  //
  // A target is either a tab id or a settings-overlay section id. Both are real
  // things in the shell; neither was reachable by name from outside it.
  function goTo(target) {
    if (!target) return false;
    var tab = el(target.indexOf("tab-") === 0 ? target : "tab-" + target);
    if (tab) { tab.click(); return true; }
    // A settings section: open the overlay, then its rail item.
    var rail = document.querySelector('[data-section="' + target + '"]');
    if (rail) {
      var gear = el("gearNavBtn");
      if (gear) gear.click();
      rail.click();
      return true;
    }
    return false;
  }

  // Deep links, so a tray item, an OS notification or a bookmark can name a
  // surface. Applied on load and on every change — `hashchange` alone misses
  // the case the tray actually produces, which is a fresh navigation to a URL
  // that already carries the fragment.
  function routeFromHash() {
    var want = (location.hash || "").replace(/^#/, "");
    if (want) goTo(want);
  }

  // The one-word label per state. Deliberately short: this sits in the chrome
  // beside the product name, and a sentence there becomes furniture nobody
  // reads. The sentence lives in the tooltip and in the detail panel.
  // One literal per entry, resolved at paint time so a language switch repaints
  // rather than freezing whatever was current when this file parsed.
  var LABEL = {
    healthy: function () { return WavrT("Live"); },
    starting: function () { return WavrT("Starting"); },
    updating: function () { return WavrT("Updating"); },
    paused: function () { return WavrT("Paused"); },
    degraded: function () { return WavrT("Needs a look"); },
    attention: function () { return WavrT("Needs attention"); },
    unavailable: function () { return WavrT("Not responding"); },
  };

  function label(body) {
    // ONE VOCABULARY, ON EVERY SURFACE.
    //
    // This chip used to render a bare count for `degraded` and `attention` —
    // "1 issue", "3 issues" — while the desktop tray, reading the same state
    // from the same producer, said "needs a look" and "needs attention". Two
    // vocabularies for one condition, and the tray's own comment had already
    // made the argument for which one is right: a number with no verdict
    // attached READS AS FINE. "3 issues" is a quantity. Somebody glancing at a
    // chip needs to be told whether it is their problem.
    //
    // So the chip says the verdict, like the tray, like the Android
    // notification, like the CLI. The count is not lost: it is in the detail
    // this chip opens and in the description it already carries for screen
    // readers, which is where a number belongs — beside the things it counts.
    var state = (body && body.state) || "unavailable";
    return LABEL[state] ? LABEL[state]() : state;
  }

  /* The runtime headline, translated, WITHOUT the Space's name in the key.
   *
   * `WavrT(body.headline)` sent "Wavr — running · Casa de Teste · everything
   * reporting" through the catalogue: no entry could ever match it, and the
   * household's private name for their home was recorded as a missing
   * translation — which is how a private name ends up in an audit. The same
   * leak `#brandSpace` and `<title>` had, arriving through a third door.
   *
   * The Core now also emits `headline_template`, the identical sentence with
   * `{space}` where the name goes. The words are translated; the name is
   * substituted here and never leaves this machine's DOM.
   */
  function headlineOf(body) {
    if (!body) return "";
    var tpl = body.headline_template || body.headline || "";
    if (!tpl) return "";
    // `space` for the runtime headline, `n` for the attention one. Passing
    // both is harmless — an unused slot simply is not in the string — and it
    // keeps ONE helper rather than two that drift.
    var head = WavrT(tpl, { space: body.space || "",
                            n: (body.total === undefined ? 0 : body.total) });
    // In a bad state the head names the state and this appends WHAT is wrong,
    // translated on its own. The Core used to append the finding's composed
    // English to the template itself, which put a count inside the lookup key
    // and left this tooltip untranslated in every state but the healthy ones.
    var worst = (body.findings || []).filter(function (f) {
      return f.state === body.state;
    })[0];
    var detail = worst ? findingWords(worst) : "";
    return detail ? head + " · " + detail : head;
  }

  var lastOk = 0;

  /* Draw "not responding". Reachable from the tick AND from the watchdog.
   *
   * One function, because two ways of rendering the worst state is two things
   * to keep in step, and the one that drifts is the one nobody is watching.
   */
  function renderUnavailable(chip) {
    chip.hidden = false;
    chip.dataset.state = "unavailable";
    el("runtimeText").textContent = LABEL.unavailable();
    chip.setAttribute(
      "data-tip",
      WavrT("Wavr is not answering on this machine. It may have stopped."));
    // The accessible name carries the state as words. A screen reader gets
    // the same information the dot carries visually — colour is never the
    // only thing saying what is happening.
    chip.setAttribute("aria-label", WavrT("Wavr is not responding"));
    var panelOut = el("coreRuntime");
    if (panelOut) {
      // Written here too, or a Core that stopped answering leaves the panel
      // showing whatever it last said — the exact failure this exists to
      // make impossible.
      panelOut.hidden = false;
      panelOut.dataset.state = "unavailable";
      panelOut.textContent = WavrT("Wavr is not answering on this device.");
    }
    // This chip's own two watchdogs (the 8s request deadline, the 5s clock
    // check against STALE_MS=35s) are ALWAYS the fastest way this page learns
    // the Core stopped answering — faster than the live-stream's own
    // open-but-silent watchdog (WS_SILENCE_MS=60s in core-connection.js),
    // which only reacts to the SAME fact from a slower angle. Without this,
    // a red "Not responding" chip sat for up to 25+ seconds beside a green
    // "live" badge and a "Someone is here" panel that both kept affirming
    // presence off a frame that had stopped arriving — the reconnecting
    // state existed, the fastest signal that should have raised it just
    // never called it. `setReconnecting` is defined in core-connection.js,
    // which this file's own header already says loads first.
    if (typeof setReconnecting === "function") setReconnecting(true);
  }

  /* Staleness measured against the clock, not against the poll.
   *
   * The guard this replaces lived inside `tick` and read
   * `Date.now() - lastOk > STALE_MS` on the line after `lastOk = Date.now()`.
   * It compared a timestamp with itself, so it could never fire — and it could
   * not have helped if it had, because it only ran on a path that had just
   * succeeded.
   *
   * The case it was written for is a Core that goes DARK: power cut, laptop
   * suspended, Wi-Fi dropped. A dark host does not refuse the connection, it
   * says nothing, so `await fetch(...)` never settles, `tick` never reaches
   * its render, and the chip keeps saying "Live" for as long as the page stays
   * open. Ninety seconds of that was reproduced in a browser.
   *
   * Two changes make the reading honest. The request carries a deadline —
   * for a LIVENESS reading, a late answer is not an answer — and this runs on
   * its own interval, so it neither waits for a tick nor can be blocked by
   * one. What it renders is derived from an observation, the time of the last
   * real answer, and never from the absence of bad news.
   */
  function watchdog() {
    var chip = el("runtimeChip");
    if (!chip) return;
    if (!lastOk || Date.now() - lastOk > STALE_MS) renderUnavailable(chip);
  }

  async function tick() {
    var chip = el("runtimeChip");
    if (!chip) return;
    var body = null;
    try {
      var r = await WavrAPI.fetch("/api/runtime",
                                  { timeoutMs: REQ_TIMEOUT_MS });
      if (r.ok) { body = await r.json(); lastOk = Date.now(); }
    } catch (e) {
      body = null;
    }

    if (!body) {
      renderUnavailable(chip);
      return;
    }

    // The Core Panel's own line, when this page is being used as one. Rendered
    // from the SAME answer as the chip so an ambient panel and a dashboard
    // cannot disagree — and hidden while healthy, because a face that always
    // carries a badge cannot signal that something changed.
    var panel = el("coreRuntime");
    if (panel) {
      var bad = ["degraded", "attention", "unavailable", "paused", "updating"];
      if (bad.indexOf(body.state) === -1) {
        panel.hidden = true;
      } else {
        panel.hidden = false;
        panel.dataset.state = body.state;
        // The worst finding, in full, because there is room here and a person
        // across the room cannot hover a tooltip.
        var worst = (body.findings || []).filter(function (f) {
          return f.state === body.state;
        })[0];
        //  last, and it is not decoration. A paused Core has no
        // FINDING (being paused is not a fault) and the Core sends no headline
        // for it, so both of the first two answer with an empty string — and a
        // panel that is shown with no text has no height, which is the same
        // silence as being hidden to the person across the room. This is the
        // defect this whole panel exists to prevent, in its own renderer.
        panel.textContent = findingWords(worst) || headlineOf(body) || label(body);
      }
    }

    chip.hidden = false;
    chip.dataset.state = body.state || "starting";
    var text = label(body);
    el("runtimeText").textContent = (body.space ? body.space + " · " : "") + text;
    var tip = headlineOf(body) || text;
    chip.setAttribute("data-tip", tip);
    chip.setAttribute("aria-label", WavrT("{headline}. Click for detail.", {headline: tip}));
    // The Core reports "unavailable": raise the reconnecting claim here rather
    // than waiting for the WS watchdog, which is a full silence window behind.
    //
    // It RAISES and never lowers, and the asymmetry is the point. Lowering
    // means "the live stream is delivering again", and the only thing that can
    // know that is something that saw a frame -- `handle()` in render.js, or
    // the watchdog when frames resume. This is an HTTP poll of `/api/runtime`;
    // it answering proves the Core is alive, not that the stream is flowing.
    //
    // Written as one line for both directions, it regressed the kiosk: the
    // wall panel's hub pill leaves "not known yet" on the first call from
    // either side, so a healthy `/api/runtime` painted a green `Hub ✓` over a
    // socket that had never delivered anything -- which is precisely the case
    // of a hub dying quietly, and precisely what the pill exists to refuse.
    if (typeof setReconnecting === "function" && body.state === "unavailable") {
      setReconnecting(true);
    }
  }

  // -- The action inbox --------------------------------------------------------
  //
  // Same poll, same rule: no answer means the chip goes away rather than
  // showing a stale count. A badge that says "2 need you" over a Core that
  // stopped answering is worse than no badge, because it is specific.

  // The count on the Manage item. Hidden at zero for the same reason the chip
  // is: a permanent "0" trains people to stop seeing the spot.
  function markManage(total) {
    var badge = el("navManageCount");
    if (!badge) return;
    badge.textContent = String(total);
    badge.hidden = !total;
    var btn = el("tab-manage");
    if (btn) {
      btn.setAttribute(
        "aria-label",
        total
          ? WavrT("Manage — {n} thing needs you|Manage — {n} things need you",
                  {n: total})
          : WavrT("Manage"));
    }
  }

  async function tickAttention() {
    var chip = el("attnChip");
    if (!chip) return;
    var body = null;
    try {
      // Same deadline as the runtime poll, for the same reason. This badge
      // makes a SPECIFIC claim — "2 need you" — and a specific claim left
      // standing over a Core that went dark is worse than a vague one: a
      // person reads it, believes there are exactly two, and acts on it.
      var r = await WavrAPI.fetch("/api/attention",
                                  { timeoutMs: REQ_TIMEOUT_MS });
      if (r.ok) body = await r.json();
    } catch (e) {
      body = null;
    }
    renderAttention(body);
    // Demoting Network, System and the decision queue behind "Manage" is only
    // honest if work inside it still reaches a person who is looking at Space.
    // Same payload as the chip — one producer, so the chrome and the level
    // marker can never disagree about how many things are waiting.
    markManage(body && body.total ? body.total : 0);
    if (!body || !body.total) {
      // Hidden, not zero. A permanent "0" is furniture, and furniture is
      // exactly what people stop seeing.
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    chip.dataset.band = body.blocking ? "blocking" : "degraded";
    el("attnCount").textContent = String(body.total);
    // The word has to agree with the number standing next to it. The count was
    // dynamic and the word was a fixed "need you", so one thing waiting read
    // "1 need you" — in the chrome, at every width, on the screen somebody sees
    // first. `WavrT` already splits on `|` when it is handed `n`; the plural
    // pipe is the product's own machinery and it simply was not reaching here.
    var attnLabel = el("attnLabel");
    if (attnLabel) {
      attnLabel.textContent = WavrT("thing needs you|things need you",
                                    { n: body.total });
    }
    var attnTip = headlineOf(body);
    chip.setAttribute("data-tip", attnTip);
    chip.setAttribute("aria-label",
      WavrT("{headline}. Click to see them.", { headline: attnTip }));
  }

  function escape(t) {
    return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // How long ago, said the way somebody says it out loud. The BACKEND already
  // does this for the runtime headline; this is the list, where each row has its
  // own age, and shipping seven ages per response would be noise on the wire.
  function since(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return WavrT("just now");
    return humanAge(s);
  }

  // A duration in seconds, in words, in the reader's language.
  //
  // The plural pipe matters here and was missing: "{n} minutes" with n = 1
  // rendered "1 minutes", and a language whose plural rule is not English's had
  // no way to express its own.
  //
  // This exists because the BACKEND cannot produce it. `runtime_status._human`
  // builds "3 minutes" in English, and a finding that arrived with that already
  // baked in would drop an English fragment into the middle of a Portuguese
  // sentence. So a finding ships the raw seconds and this turns them into words
  // on the side that knows the language.
  // FLOOR, not round, and that is not a style choice: `runtime_status._human`
  // floors (`int(seconds)`, then `//`), and the tray, the CLI and the Android
  // notification all read its composed English. Rounding here made the browser
  // say "2 minutes" where the tray said "1 minute" about the same reading —
  // two answers to one question, in front of somebody with no way to tell
  // which is right, which is the rule this whole module opens with.
  function humanAge(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return WavrT("{n} second|{n} seconds", {n: s});
    if (s < 3600) return WavrT("{n} minute|{n} minutes", {n: Math.floor(s / 60)});
    if (s < 86400) return WavrT("{n} hour|{n} hours", {n: Math.floor(s / 3600)});
    return WavrT("{n} day|{n} days", {n: Math.floor(s / 86400)});
  }

  // A finding's sentence, translated.
  //
  // This was `WavrT(worst.text)` on a composed sentence, so the lookup key was
  // "3 of 5 sensors are not reporting" — a key with a count in it, which no
  // catalogue can hold. The wall panel's one line of prose stayed English in
  // every language, and the headline beside it had carried a `{n}` slot for
  // exactly this reason since it was fixed.
  //
  // An argument whose key ends in `_s` is a duration in seconds and becomes a
  // localised phrase under the key without the suffix: `age_s` fills `{age}`.
  // `text` is the composed English, kept as the fallback for a payload from an
  // older Core.
  function findingWords(f) {
    if (!f) return "";
    if (!f.text_template) return f.text || "";
    var args = {};
    var raw = f.text_args || {};
    Object.keys(raw).forEach(function (k) {
      if (k.length > 2 && k.slice(-2) === "_s") args[k.slice(0, -2)] = humanAge(raw[k]);
      else args[k] = raw[k];
    });
    return WavrT(f.text_template, args);
  }

  // A row's wording, in the one shape that keeps the household's own words out
  // of the catalogue.
  //
  // This used to be `WavrT(it.title)` on a finished sentence, so the lookup key
  // was "hall-cam needs its stream address" or "Seen in sala" — a camera's name
  // and a room's name, inside a key no catalogue can ever hold. Every row of
  // this inbox therefore rendered in English however the language was set, and
  // every miss recorded a private name into the missing-translation audit.
  //
  // `template` + `args` is the sentence Wavr wrote, translated with the values
  // slotted in afterwards. `text` is wording this Core did not write — a
  // discovery card's own title, an install's update instructions — and is
  // rendered verbatim, because looking it up is the leak. `composed` is the
  // finished English, kept as the fallback for a payload from an older Core.
  function rowWords(template, args, text, composed) {
    if (template) return WavrT(template, args || {});
    return text || composed || "";
  }

  // The one place the list is rendered. The tray, the chip and this panel all
  // read the SAME `/api/attention`, so a count in the chrome and a list on the
  // page cannot disagree about how many things are waiting.
  function renderAttention(body) {
    var tile = el("attnTile");
    var list = el("attnList");
    if (!tile || !list) return;
    var items = (body && body.items) || [];
    var blind = (body && body.could_not_check) || [];
    if (!items.length && !blind.length) {
      // Nothing waiting means nothing rendered. An empty inbox with a cheerful
      // heading is furniture, and furniture is what people stop seeing.
      tile.hidden = true;
      return;
    }
    if (!items.length) {
      // Nothing came back AND a source failed to read. This used to return
      // above, hiding the tile and the chip — so three pairing requests sitting
      // in an unreadable inbox looked exactly like a calm house. The one case
      // the "could not check" note was written for was the one case it could
      // not reach.
      tile.hidden = false;
      el("attnHint").textContent = headlineOf(body);
      list.innerHTML = "";
    }
    tile.hidden = false;
    el("attnHint").textContent = headlineOf(body);

    list.innerHTML = items.map(function (it) {
      var waited = since(it.since);
      var title = rowWords(it.title_template, it.title_args, it.title_text, it.title);
      var detail = rowWords(it.detail_template, it.detail_args, it.detail_text, it.detail);
      // Evidence sits outside the sentence, and only when the sentence came
      // from this payload's own template or text: the composed `detail` we fall
      // back to already carries it, and appending twice reads as a stutter.
      if ((it.detail_template || it.detail_text) && it.evidence) {
        detail = (detail ? detail + " " : "") + "(" + it.evidence + ")";
      }
      return '<div class="attn-row" data-band="' + escape(it.band) + '">'
        + '<span class="attn-band" aria-hidden="true"></span>'
        + '<div class="attn-body">'
        + '<div class="attn-title">' + escape(title)
        + (it.count > 1 ? " <span class=\"attn-since\">×"
                          + it.count + "</span>" : "")
        + "</div>"
        + (detail ? '<div class="attn-detail">' + escape(detail) + "</div>" : "")
        + (waited ? '<div class="attn-since">' + escape(WavrT("Waiting {age}", {age: waited})) + "</div>" : "")
        + "</div>"
        + (it.action
            ? '<button type="button" class="ctl small attn-go" data-where="'
              + escape(it.where) + '">' + escape(WavrT(it.action)) + "</button>"
            : "")
        + "</div>";
    }).join("");

    // Each row goes to the screen that can actually resolve it. The list never
    // resolves anything itself: a second place to approve a pairing request is
    // a second place for that logic to be wrong.
    // Each target checked against the tab that actually holds the thing.
    // `discoveries` pointed at `tab-novos` — a DIFFERENT tab, listing Wi-Fi
    // devices from /api/inventory — so "Review" on a discovered camera opened a
    // screen that does not contain it. `coverage` pointed at the System tab,
    // where the coverage list does not live either: it is inside the settings
    // overlay's Space section.
    var WHERE = {
      devices: { tab: "tab-dispositivos" },
      discoveries: { tab: "tab-discoveries" },
      alerts: { tab: "tab-inicio" },
      system: { tab: "tab-sistema" },
      // Not a tab at all. The coverage list lives in the gear overlay.
      coverage: { gear: "gearSecSpace" },
    };
    list.querySelectorAll("[data-where]").forEach(function (b) {
      b.addEventListener("click", function () {
        var target = WHERE[b.dataset.where] || WHERE.alerts;
        goTo(target.gear || target.tab);
      });
    });

    var note = el("attnNote");
    if (note) {
      // Said out loud when a source could not be read. "Nothing needs your
      // attention" over a source that failed is the exact lie this surface
      // exists to prevent, so a partial answer never renders as a complete one.
      if (body.could_not_check && body.could_not_check.length) {
        note.hidden = false;
        note.textContent = WavrT("Wavr could not check: {sources}. There may be more "
          + "waiting than this list shows.", {sources: body.could_not_check.join(", ")});
      } else {
        note.hidden = true;
      }
    }
  }

  function wire() {
    var chip = el("runtimeChip");
    if (!chip) return;
    // The public demo makes NO backend calls. That is not a preference — it is
    // stated to the reader on the Privacy screen ("this page makes no backend
    // calls"), in the README twice, and in the repository's own CLAUDE.md, and
    // it is the reason somebody can open the demo without trusting it.
    //
    // This module never knew about MODE, so `tick()` and `tickAttention()`
    // polled /api/runtime and /api/attention for ever in simulated mode: the
    // promise was false, and the requests 404'd against a host with no Core, so
    // nothing looked wrong from the inside.
    //
    // The chip stays hidden rather than reporting: a demo has no runtime, and a
    // green "Live" over a simulator would be the same lie in the other
    // direction. `#demoPill` in the topbar is what says where you are.
    if (typeof MODE !== "undefined" && MODE !== "live" && MODE !== "companion") {
      return;
    }
    chip.addEventListener("click", function () {
      // Straight to the surface that explains it, rather than a second
      // rendering of the same summary in a popover.
      // Through the one navigator, so the chip cannot drift from the tray and
      // the hash router the way it already had.
      goTo("gearSecTrust");
    });
    var attn = el("attnChip");
    if (attn) {
      // The landing surface, where the tile now lives. It used to point at the
      // New devices tab because the tile was inside it — one of the inbox's own
      // sources holding the aggregate.
      attn.addEventListener("click", function () { goTo("tab-inicio"); });
    }
    // Deep links last, so the shell's own tabs exist before one is selected.
    window.addEventListener("hashchange", routeFromHash);
    routeFromHash();
    tick();
    tickAttention();
    setInterval(function () { tick(); tickAttention(); }, POLL_MS);
    // Its own timer, and that is the whole point: a poll that is hanging
    // cannot schedule the check that notices it is hanging.
    setInterval(watchdog, WATCHDOG_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
