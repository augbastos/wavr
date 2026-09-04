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
 */
(function () {
  "use strict";

  var POLL_MS = 10000;
  // Longer than the poll, so one slow response does not flap the chip; short
  // enough that a stopped Core is visible within about half a minute.
  var STALE_MS = 35000;

  var el = function (id) { return document.getElementById(id); };

  // The one-word label per state. Deliberately short: this sits in the chrome
  // beside the product name, and a sentence there becomes furniture nobody
  // reads. The sentence lives in the tooltip and in the detail panel.
  var LABEL = {
    healthy: "Live",
    starting: "Starting",
    updating: "Updating",
    paused: "Paused",
    degraded: "1 issue",
    attention: "Needs attention",
    unavailable: "Not responding",
  };

  function label(body) {
    var state = (body && body.state) || "unavailable";
    if (state === "degraded" || state === "attention") {
      // Count the findings that are actually at the worst level, so "1 issue"
      // is true and "3 issues" is not invented from the total number of checks.
      var n = ((body && body.findings) || []).filter(function (f) {
        return f.state === state;
      }).length;
      return n === 1 ? "1 issue" : n + " issues";
    }
    return LABEL[state] || state;
  }

  var lastOk = 0;

  async function tick() {
    var chip = el("runtimeChip");
    if (!chip) return;
    var body = null;
    try {
      var r = await fetch("/api/runtime", { headers: { "X-Wavr-Local": "1" } });
      if (r.ok) { body = await r.json(); lastOk = Date.now(); }
    } catch (e) {
      body = null;
    }

    // A response that is merely OLD is also not an answer. Without this a
    // suspended laptop waking up would show a stale-but-green chip for as long
    // as the first request took to fail.
    if (body && Date.now() - lastOk > STALE_MS) body = null;

    if (!body) {
      chip.hidden = false;
      chip.dataset.state = "unavailable";
      el("runtimeText").textContent = LABEL.unavailable;
      chip.setAttribute(
        "data-tip",
        "Wavr is not answering on this machine. It may have stopped.");
      // The accessible name carries the state as words. A screen reader gets
      // the same information the dot carries visually — colour is never the
      // only thing saying what is happening.
      chip.setAttribute("aria-label", "Wavr is not responding");
      return;
    }

    chip.hidden = false;
    chip.dataset.state = body.state || "starting";
    var text = label(body);
    el("runtimeText").textContent = (body.space ? body.space + " · " : "") + text;
    chip.setAttribute("data-tip", body.headline || text);
    chip.setAttribute("aria-label",
                      (body.headline || text) + ". Click for detail.");
  }

  // -- The action inbox --------------------------------------------------------
  //
  // Same poll, same rule: no answer means the chip goes away rather than
  // showing a stale count. A badge that says "2 need you" over a Core that
  // stopped answering is worse than no badge, because it is specific.

  async function tickAttention() {
    var chip = el("attnChip");
    if (!chip) return;
    var body = null;
    try {
      var r = await fetch("/api/attention", { headers: { "X-Wavr-Local": "1" } });
      if (r.ok) body = await r.json();
    } catch (e) {
      body = null;
    }
    renderAttention(body);
    if (!body || !body.total) {
      // Hidden, not zero. A permanent "0" is furniture, and furniture is
      // exactly what people stop seeing.
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    chip.dataset.band = body.blocking ? "blocking" : "degraded";
    el("attnCount").textContent = String(body.total);
    chip.setAttribute("data-tip", body.headline);
    chip.setAttribute("aria-label", body.headline + ". Click to see them.");
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
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " minutes";
    if (s < 86400) return Math.round(s / 3600) + " hours";
    return Math.round(s / 86400) + " days";
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
      el("attnHint").textContent = (body && body.headline) || "";
      list.innerHTML = "";
    }
    tile.hidden = false;
    el("attnHint").textContent = body.headline || "";

    list.innerHTML = items.map(function (it) {
      var waited = since(it.since);
      return '<div class="attn-row" data-band="' + escape(it.band) + '">'
        + '<span class="attn-band" aria-hidden="true"></span>'
        + '<div class="attn-body">'
        + '<div class="attn-title">' + escape(it.title)
        + (it.count > 1 ? " <span class=\"attn-since\">×"
                          + it.count + "</span>" : "")
        + "</div>"
        + (it.detail ? '<div class="attn-detail">' + escape(it.detail) + "</div>" : "")
        + (waited ? '<div class="attn-since">Waiting ' + escape(waited) + "</div>" : "")
        + "</div>"
        + (it.action
            ? '<button type="button" class="ctl small attn-go" data-where="'
              + escape(it.where) + '">' + escape(it.action) + "</button>"
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
        if (target.gear) {
          var gear = el("gearNavBtn");
          if (gear) gear.click();
          var rail = document.querySelector(
            '[data-section="' + target.gear + '"]');
          if (rail) rail.click();
          return;
        }
        var tab = el(target.tab);
        if (tab) tab.click();
      });
    });

    var note = el("attnNote");
    if (note) {
      // Said out loud when a source could not be read. "Nothing needs your
      // attention" over a source that failed is the exact lie this surface
      // exists to prevent, so a partial answer never renders as a complete one.
      if (body.could_not_check && body.could_not_check.length) {
        note.hidden = false;
        note.textContent = "Wavr could not check: "
          + body.could_not_check.join(", ")
          + ". There may be more waiting than this list shows.";
      } else {
        note.hidden = true;
      }
    }
  }

  function wire() {
    var chip = el("runtimeChip");
    if (!chip) return;
    chip.addEventListener("click", function () {
      // Straight to the surface that explains it, rather than a second
      // rendering of the same summary in a popover.
      var gear = el("gearNavBtn");
      if (gear) gear.click();
      // `data-section`, which is what the gear rail actually uses. This was
      // `[data-sec="trust"]` — an attribute that appears nowhere in the
      // dashboard — so the selector matched nothing, only the gear opened, and
      // the overlay landed on whatever section was last active. The chip's
      // whole justification is that a summary must be openable.
      var rail = document.querySelector('[data-section="gearSecTrust"]');
      if (rail) rail.click();
    });
    var attn = el("attnChip");
    if (attn) {
      attn.addEventListener("click", function () {
        var tab = el("tab-novos");
        if (tab) tab.click();
      });
    }
    tick();
    tickAttention();
    setInterval(function () { tick(); tickAttention(); }, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
