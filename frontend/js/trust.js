/* Trust Check — "how well does Wavr work here, and why should I believe it?"
 *
 * A COMPOSITION of systems that already exist: coverage, reliability, guided
 * validation, topology. It owns no judgement of its own — every verdict and
 * every sentence on this screen comes from the backend that measured it. If this
 * file ever starts deciding what "good" means, the judgement has moved to the
 * wrong place and two surfaces will disagree about the same sensor.
 *
 * A classic script, not a module: it uses `MODE` and `actionFeedback` from the
 * main document's shared global scope and must execute in document order.
 */
(function () {
  "use strict";

  var HDR = { "X-Wavr-Local": "1" };

  function el(id) { return document.getElementById(id); }

  function api(path, opts) {
    opts = opts || {};
    var headers = { "X-Wavr-Local": "1" };
    if (opts.body) headers["Content-Type"] = "application/json";
    return fetch(location.origin + path, {
      method: opts.method || "GET", headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.detail || ("Wavr answered " + r.status));
        return j;
      });
    });
  }

  // No innerHTML anywhere in this file. The values it renders — sensor ids,
  // room names — are operator input, and a file that never reaches for
  // innerHTML cannot grow an unsafe one in a later edit.
  function head(title, hint) {
    var d = document.createElement("div");
    d.className = "tile-head";
    var h = document.createElement("h2");
    h.textContent = title;
    d.appendChild(h);
    if (hint) {
      var s = document.createElement("span");
      s.className = "hint";
      s.textContent = hint;
      d.appendChild(s);
    }
    return d;
  }

  function row(parent, label, value, note) {
    var d = document.createElement("div");
    d.className = "pair-dev-row";
    var left = document.createElement("span");
    var b = document.createElement("b"); b.textContent = label;
    left.appendChild(b);
    if (value) {
      var m = document.createElement("span");
      m.className = "pair-dev-meta";
      m.textContent = " · " + value;
      left.appendChild(m);
    }
    d.appendChild(left);
    if (note) {
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = note;
      d.appendChild(n);
    }
    parent.appendChild(d);
    return d;
  }

  // ---- Coverage: what Wavr can see ----------------------------------------
  async function renderCoverage(into) {
    var body;
    try { body = await api("/api/coverage"); } catch (e) { return; }
    if (!body || body.available === false) {
      row(into, "Coverage", "cannot be read right now",
          "Wavr could not enumerate its sensors. This is NOT the same as having none.");
      return;
    }
    var watched = 0, present = 0;
    (body.rooms || []).forEach(function (r) {
      present++;
      if (r.observing) watched++;
    });
    var blind = (body.uncovered || []).length;
    row(into, "Rooms being watched", watched + " of " + (present + blind));
    (body.rooms || []).filter(function (r) { return !r.observing; })
      .forEach(function (r) {
        row(into, r.room, "has a sensor, but it is not answering",
            "This is a repair, not a purchase — something you own should be reporting here.");
      });
    (body.uncovered || []).forEach(function (name) {
      row(into, name, "no sensor at all",
          "Wavr cannot see this room. That is not the same as it being empty.");
    });
  }

  // ---- Reliability: what each sensor has earned ---------------------------
  async function renderReliability(into) {
    var body;
    try { body = await api("/api/reliability"); } catch (e) { return; }
    if (!body || body.available === false) {
      row(into, "Sensor reliability", "not tracked on this Core");
      return;
    }
    var profiles = body.profiles || [];
    if (!profiles.length) {
      row(into, "Sensor reliability", "nothing measured yet",
          "Every sensor is treated at full trust until a guided walk has "
          + "measured it. A new sensor is not a suspect sensor.");
      return;
    }
    profiles.forEach(function (p) {
      // The backend's own sentence, verbatim. Rewriting it here would create a
      // second opinion about the same measurement.
      // NOT `head` — that name is the element builder above, and shadowing it
      // here would break the first heading anyone adds to this function.
      var label = p.sensor_id + " · " + p.capability
        + (p.room ? " · " + p.room : "");
      row(into, label, p.measured ? "measured" : "not enough checks yet", p.reason);
    });
  }

  // ---- Topology: is the floor plan coherent -------------------------------
  async function renderTopology(into) {
    var body;
    try { body = await api("/api/topology"); } catch (e) { return; }
    var isolated = (body && body.isolated) || [];
    if (!isolated.length) return;      // nothing to say is better than a green tick
    isolated.forEach(function (name) {
      row(into, name, "connects to no other room",
          "Almost always a gap in the floor plan. Wavr will treat any movement "
          + "in or out of this room as implausible until it is linked.");
    });
  }

  // ---- The guided walk ------------------------------------------------------
  function buildWalk(into) {
    var state = { session: null, room: "", timer: null, samples: 0 };

    var card = document.createElement("div");
    card.className = "tile";

    var lede = document.createElement("p");
    lede.className = "hint";
    lede.textContent = "Walk through a room and tell Wavr what is true. It "
      + "grades every sensor against you and remembers which ones to trust here.";
    card.appendChild(lede);

    var roomInp = document.createElement("input");
    roomInp.type = "text"; roomInp.maxLength = 64;
    roomInp.placeholder = "which room are you testing?";
    roomInp.style.marginTop = "8px";
    card.appendChild(roomInp);

    var fb = document.createElement("span");
    fb.className = "action-fb";
    fb.setAttribute("aria-live", "polite");

    var steps = document.createElement("div");
    steps.style.marginTop = "10px";

    var startBtn = document.createElement("button");
    startBtn.type = "button"; startBtn.className = "ctl small primary";
    startBtn.textContent = "Start a walk";

    function stopPolling() {
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }

    // Sampling on a loop is what makes RESPONSE TIME measurable rather than
    // just accuracy — the first moment a sensor agrees is the number a
    // household actually feels.
    function startPolling() {
      stopPolling();
      state.samples = 0;
      state.timer = setInterval(function () {
        api("/api/validation/" + encodeURIComponent(state.session) + "/sample",
            { method: "POST" })
          .then(function (r) {
            if (r.sampled) {
              state.samples++;
              counter.textContent = state.samples + " checks so far";
            } else if (r.reason === "window_expired") {
              stopPolling();
              counter.textContent = "Paused — tell Wavr what is true again.";
            }
          })
          .catch(function () { /* a dropped sample is not worth interrupting a walk */ });
      }, 1500);
    }

    var counter = document.createElement("span");
    counter.className = "pair-dev-meta";

    function declare(occupied, label) {
      stopPolling();
      api("/api/validation/" + encodeURIComponent(state.session) + "/declare",
          { method: "POST", body: { occupied: occupied } })
        .then(function () {
          counter.textContent = label + " — hold still for a few seconds";
          startPolling();
        })
        .catch(function (e) { actionFeedback(fb, false, String(e.message || e)); });
    }

    function renderRunning() {
      steps.textContent = "";
      var here = document.createElement("button");
      here.type = "button"; here.className = "ctl small primary";
      here.textContent = "I am in this room";
      here.onclick = function () { declare(true, "Recording: you are here"); };

      var gone = document.createElement("button");
      gone.type = "button"; gone.className = "ctl small";
      gone.textContent = "I have left";
      gone.onclick = function () { declare(false, "Recording: the room is empty"); };

      var done = document.createElement("button");
      done.type = "button"; done.className = "ctl small";
      done.textContent = "Finish";
      done.onclick = function () {
        stopPolling();
        api("/api/validation/" + encodeURIComponent(state.session) + "/finish",
            { method: "POST" })
          .then(renderSummary)
          .catch(function (e) { actionFeedback(fb, false, String(e.message || e)); });
      };

      var give = document.createElement("button");
      give.type = "button"; give.className = "ctl small off";
      give.textContent = "Discard";
      give.onclick = function () {
        stopPolling();
        api("/api/validation/" + encodeURIComponent(state.session) + "/abandon",
            { method: "POST" })
          .then(function () {
            state.session = null;
            steps.textContent = "";
            counter.textContent = "";
            actionFeedback(fb, true, null, "✓ discarded — nothing recorded");
            startBtn.disabled = false;
          });
      };

      [here, gone, done, give].forEach(function (b) { steps.appendChild(b); });
      steps.appendChild(counter);
    }

    function renderSummary(summary) {
      state.session = null;
      startBtn.disabled = false;
      steps.textContent = "";

      var head = document.createElement("p");
      head.className = "hint";
      // The backend's conclusion, verbatim.
      head.textContent = summary.conclusion || "";
      steps.appendChild(head);

      (summary.sensors || []).forEach(function (s) {
        row(steps, s.sensor_id + " · " + s.modality, s.verdict, s.note);
      });
      if (!(summary.sensors || []).length) {
        row(steps, "Nothing was graded", "",
            "No sensor was reporting on that room during the walk. A sensor "
            + "that could not have known is never marked wrong.");
      }
    }

    startBtn.onclick = function () {
      var room = roomInp.value.trim();
      if (!room) { actionFeedback(fb, false, "which room?"); return; }
      startBtn.disabled = true;
      api("/api/validation/start", { method: "POST", body: { room: room } })
        .then(function (r) {
          state.session = r.session_id;
          state.room = room;
          if (r.resumed) actionFeedback(fb, true, null, "✓ resumed");
          renderRunning();
        })
        .catch(function (e) {
          startBtn.disabled = false;
          actionFeedback(fb, false, String(e.message || e));
        });
    };

    var controls = document.createElement("div");
    controls.className = "controls-row";
    controls.style.marginTop = "10px";
    controls.appendChild(startBtn);
    controls.appendChild(fb);
    card.appendChild(controls);
    card.appendChild(steps);
    into.appendChild(card);
  }

  // ---- Compose --------------------------------------------------------------
  async function renderTrust() {
    if (MODE !== "live") return;      // live/central only, like the other admin panels
    var host = el("trustBody");
    if (!host) return;
    host.textContent = "";

    var seeing = document.createElement("div");
    seeing.className = "tile";
    seeing.appendChild(head("What Wavr can see"));
    host.appendChild(seeing);
    await renderCoverage(seeing);
    await renderTopology(seeing);

    var earned = document.createElement("div");
    earned.className = "tile";
    earned.appendChild(head("What each sensor has earned",
                            "measured against walks you ran"));
    host.appendChild(earned);
    await renderReliability(earned);

    var walk = document.createElement("div");
    walk.className = "tile";
    walk.appendChild(head("Check a room", "takes a couple of minutes"));
    host.appendChild(walk);
    buildWalk(walk);
  }

  // ---- Privacy: what Wavr is doing, and what it keeps ---------------------
  // Same helpers as Trust, deliberately. The two screens answer neighbouring
  // questions and a second copy of row()/head() would drift from the first edit.

  var POSTURE_ROWS = [
    ["internet_required", "Internet needed", function (v) { return v ? "yes" : "no"; }],
    ["account_required", "Account needed", function (v) { return v ? "yes" : "no"; }],
    ["telemetry", "Telemetry", function (v) { return String(v); }],
    ["external_connections", "External connections",
     function (v) { return v === 0 ? "none enabled" : v + " enabled"; }],
    ["lan_access", "Other devices may connect",
     function (v) { return v ? "yes" : "no — this machine only"; }],
    ["cameras_enabled", "Cameras switched on",
     function (v) { return v === 0 ? "none" : String(v); }],
    ["network_scanning", "Looking at the network",
     function (v) { return v ? "yes" : "no"; }],
    ["identity_labels", "Naming devices after people",
     function (v) { return v ? "yes" : "no"; }]
  ];

  async function renderPosture(into) {
    var body;
    try { body = await api("/api/privacy/posture"); } catch (e) { return; }
    if (!body || body.available === false) {
      row(into, "Current posture", "cannot be read right now",
          (body && body.note) || "This is not a statement that nothing is enabled.");
      return;
    }
    POSTURE_ROWS.forEach(function (spec) {
      if (body[spec[0]] === undefined) return;
      row(into, spec[1], spec[2](body[spec[0]]));
    });
    if (body.note) {
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = body.note;
      into.appendChild(n);
    }
  }

  async function renderStored(into) {
    var body;
    try { body = await api("/api/privacy/data"); } catch (e) { return; }
    var cats = (body && body.categories) || [];
    if (!cats.length) return;

    // Never-stored first, because that is the answer people actually came for.
    var order = { never: 0, session: 1, persistent: 2 };
    var word = {
      never: "never stored",
      session: "in memory only — a restart clears it",
      persistent: "stored on this machine"
    };
    // NOT `order[x] || 9` — `never` is 0, which is falsy, and that single
    // character put the most reassuring facts at the bottom of the screen.
    function rank(r) {
      return Object.prototype.hasOwnProperty.call(order, r) ? order[r] : 9;
    }
    cats.slice().sort(function (a, b) {
      return rank(a.retention) - rank(b.retention);
    }).forEach(function (c) {
      var value = word[c.retention] || c.retention;
      if (c.retention === "persistent") {
        if (c.rows === null || c.rows === undefined) {
          value += " · not counted";
        } else if (c.counts) {
          // The number means something narrower than "rows in a table", and the
          // backend said what. Never render "7 rows" for "7 enabled".
          value += " · " + c.rows + " " + c.counts;
        } else {
          value += " · " + c.rows + " row" + (c.rows === 1 ? "" : "s");
        }
      }
      row(into, c.label, value, c.what);
    });

    [body.note, body.secrets_note].forEach(function (text) {
      if (!text) return;
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = text;
      into.appendChild(n);
    });
  }

  // What Wavr COULD talk to, and where each one reaches. A different question
  // from "what is switched on" — somebody deciding whether to enable something
  // needs to know before they enable it, not after.
  var REACH_WORDS = {
    local: "stays on this machine",
    lan: "stays on your network",
    internet: "contacts a server, tells it nothing about your home",
    cloud: "a third party receives something about your home"
  };

  async function renderProviders(into) {
    var body;
    try { body = await api("/api/privacy/providers"); } catch (e) { return; }
    var groups = (body && body.by_reach) || {};
    ["local", "lan", "internet", "cloud"].forEach(function (reach) {
      (groups[reach] || []).forEach(function (p) {
        var bits = [REACH_WORDS[reach] || reach];
        if (p.requires && p.requires.length) {
          bits.push("needs " + p.requires.join(" and "));
        }
        row(into, p.label, bits.join(" · "), p.notes || "");
      });
    });
    if (body && body.note) {
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = body.note;
      into.appendChild(n);
    }
  }

  async function renderPrivacyData() {
    if (MODE !== "live") return;
    var host = el("privacyDataBody");
    if (!host) return;
    host.textContent = "";

    var now = document.createElement("div");
    now.className = "tile";
    now.appendChild(head("What Wavr is doing right now", "read live, never cached"));
    host.appendChild(now);
    await renderPosture(now);

    var keeps = document.createElement("div");
    keeps.className = "tile";
    keeps.appendChild(head("What Wavr keeps", "including what it never keeps"));
    host.appendChild(keeps);
    await renderStored(keeps);

    var can = document.createElement("div");
    can.className = "tile";
    can.appendChild(head("What Wavr can connect to",
                         "capabilities, not what is switched on"));
    host.appendChild(can);
    await renderProviders(can);
  }

  // Rendered when the section is opened rather than on load: it makes four
  // requests and nobody looks at it every session.
  window.__wavrRenderTrust = renderTrust;
  window.__wavrRenderPrivacyData = renderPrivacyData;
})();
