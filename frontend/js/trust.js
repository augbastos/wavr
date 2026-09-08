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


  function el(id) { return document.getElementById(id); }

  // The Core, through the shared client.
  //
  // This function used to be fourteen lines, and `wizard.js` carried a
  // byte-identical copy of them. Two implementations of one decision is the
  // shape that eventually disagrees: the next person to improve the error
  // message improves one of them, and the product starts saying two different
  // things about the same failure depending on which screen you were looking
  // at. `js/api.js` owns it now; the signature here is unchanged.
  function api(path, opts) { return WavrAPI.json(path, opts); }

  /* Reading a section, with an answer for the case where there is no answer.
   *
   * Every read below was `try { … } catch (e) { return; }` and carried no
   * deadline. One silence, two causes: a Core that REFUSES the connection
   * rejects the promise and the section vanished without a word, and a Core
   * that goes DARK — power cut, suspended laptop, dropped Wi-Fi — settles
   * nothing at all, so even the `return` was unreachable and the section stayed
   * empty for ever.
   *
   * Blank is the worst possible answer on THIS screen. It is the one that
   * answers "what does Wavr know about me", and a section that draws nothing
   * reads as "nothing to declare" rather than "nobody answered". The Network
   * tab and the wall panel were taught this already; the privacy screen is the
   * surface where getting it wrong costs the most.
   *
   * The sentence is the one the house-status tile and the known-presence list
   * already use, word for word, so a person who has seen it once knows it.
   */
  const TRUST_TIMEOUT_MS = 8000;

  async function read(path, into, label) {
    try {
      return await api(path, { timeoutMs: TRUST_TIMEOUT_MS });
    } catch (e) {
      row(into, label, WavrT("cannot be read right now"),
          WavrT("Wavr is not answering, so this cannot be checked. Check that the Core is running."));
      return null;
    }
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
    var body = await read("/api/coverage", into, WavrT("Coverage"));
    if (!body) return;
    if (body.available === false) {
      row(into, WavrT("Coverage"), WavrT("cannot be read right now"),
          WavrT("Wavr could not enumerate its sensors. This is NOT the same as having none."));
      return;
    }
    var watched = 0, present = 0;
    (body.rooms || []).forEach(function (r) {
      present++;
      if (r.observing) watched++;
    });
    var blind = (body.uncovered || []).length;
    row(into, WavrT("Rooms being watched"),
        WavrT("{seen} of {total}", { seen: watched, total: present + blind }));
    (body.rooms || []).filter(function (r) { return !r.observing; })
      .forEach(function (r) {
        row(into, r.room, WavrT("has a sensor, but it is not answering"),
            WavrT("This is a repair, not a purchase — something you own should be reporting here."));
      });
    (body.uncovered || []).forEach(function (name) {
      row(into, name, WavrT("no sensor at all"),
          WavrT("Wavr cannot see this room. That is not the same as it being empty."));
    });
  }

  // ---- Reliability: what each sensor has earned ---------------------------
  async function renderReliability(into) {
    var body = await read("/api/reliability", into, WavrT("Sensor reliability"));
    if (!body) return;
    if (body.available === false) {
      row(into, WavrT("Sensor reliability"), WavrT("not tracked on this Core"));
      return;
    }
    var profiles = body.profiles || [];
    if (!profiles.length) {
      row(into, WavrT("Sensor reliability"), WavrT("nothing measured yet"),
          WavrT("Every sensor is treated at full trust until a guided walk has measured it. A new sensor is not a suspect sensor."));
      return;
    }
    profiles.forEach(function (p) {
      // The backend's own sentence, verbatim. Rewriting it here would create a
      // second opinion about the same measurement.
      // NOT `head` — that name is the element builder above, and shadowing it
      // here would break the first heading anyone adds to this function.
      var label = p.sensor_id + " · " + p.capability
        + (p.room ? " · " + p.room : "");
      row(into, label, p.measured ? WavrT("measured") : WavrT("not enough checks yet"),
          p.reason);
    });
  }

  // ---- Topology: is the floor plan coherent -------------------------------
  //
  // This printed the problem and offered nothing. "Wavr will treat any
  // movement in or out of this room as implausible until it is linked" sat on
  // screen with no way to link it: `POST /api/topology/link` exists, is
  // validated, and was called from nowhere in the product.
  //
  // Stairs are what make that not a nicety. Adjacency is inferred from the
  // drawing and bucketed STRICTLY by level, so geometry can never join two
  // floors — only a person can say which rooms the stairs connect. Until they
  // could, every cross-floor movement in a two-storey Space was judged
  // impossible, permanently, and the interface complained about it forever.
  async function renderTopology(into) {
    var body = await read("/api/topology", into, WavrT("Room connections"));
    if (!body) return;
    var isolated = (body && body.isolated) || [];
    if (!isolated.length) return;      // nothing to say is better than a green tick
    var all = ((body && body.rooms) || []).map(function (r) { return r.room; });
    isolated.forEach(function (name) {
      var r = row(into, name, WavrT("connects to no other room"),
          WavrT("Almost always a gap in the floor plan. Wavr will treat any movement in or out of this room as implausible until it is linked."));
      r.appendChild(linkControl(name, all));
    });
  }

  /* "Which room does this one connect to?" — a picker and a button.
   *
   * One link at a time rather than a graph editor: an isolated room needs ONE
   * neighbour to stop being isolated, and "what is next door" is a question a
   * person can answer without thinking about graphs.
   */
  function linkControl(room, allRooms) {
    var wrap = document.createElement("div");
    wrap.className = "row-actions";

    var pick = document.createElement("select");
    pick.className = "ctl small";
    pick.setAttribute("aria-label",
      WavrT("Room to connect {room} to", { room: room }));
    var none = document.createElement("option");
    none.value = "";
    none.textContent = WavrT("Connects to…");
    pick.appendChild(none);
    allRooms.forEach(function (other) {
      if (other === room) return;
      var o = document.createElement("option");
      o.value = other;
      o.textContent = other;      // a room name is what the household typed
      pick.appendChild(o);
    });

    var go = document.createElement("button");
    go.type = "button";
    go.className = "ctl small";
    go.textContent = WavrT("Link");
    go.disabled = true;
    pick.onchange = function () { go.disabled = !pick.value; };

    var fb = document.createElement("span");
    fb.className = "pair-dev-meta";

    go.onclick = async function () {
      var other = pick.value;
      if (!other) return;
      go.disabled = true;
      try {
        await api("/api/topology/link", {
          method: "POST",
          body: { room_a: room, room_b: other, connected: true,
                  note: "linked in Trust & Privacy" },
        });
      } catch (e) {
        // The reason, not a status code. `api()` already prefers the Core's
        // own `detail` over anything written here.
        fb.textContent = " · " + ((e && e.message)
          || WavrT("couldn't save that link — try again"));
        go.disabled = false;
        return;
      }
      // Repaint from the Core rather than hiding the row locally: this panel
      // renders entirely from the Core's answer, and a row removed by hand
      // would be this file deciding something the Core decides.
      if (window.__wavrRenderTrust) window.__wavrRenderTrust();
      else fb.textContent = " · " + WavrT("linked");
    };

    wrap.appendChild(pick);
    wrap.appendChild(go);
    wrap.appendChild(fb);
    return wrap;
  }

  // ---- The guided walk ------------------------------------------------------
  function buildWalk(into) {
    var state = { session: null, room: "", timer: null, samples: 0 };

    var card = document.createElement("div");
    card.className = "tile";

    var lede = document.createElement("p");
    lede.className = "hint";
    lede.textContent = WavrT("Walk through a room and tell Wavr what is true. It grades every sensor against you and remembers which ones to trust here.");
    card.appendChild(lede);

    var roomInp = document.createElement("input");
    roomInp.type = "text"; roomInp.maxLength = 64;
    // The placeholder is the visual affordance; the aria-label is the NAME.
    // Chromium happens to fall back to the placeholder when a field has
    // nothing else, which is why this read as named in a snapshot and is not
    // a name you can rely on: it is a browser courtesy, it is not what the
    // accessible-name spec asks for, and the text it borrows is the text that
    // disappears the moment somebody types. Every other named field in this
    // product carries `aria-label` beside its placeholder — same string, so
    // the two can never drift apart.
    roomInp.placeholder = WavrT("which room are you testing?");
    roomInp.setAttribute("aria-label", WavrT("which room are you testing?"));
    // It carried no class at all, so it rendered as the browser's bare native
    // text field — a flat grey box beside the dark `pair-dev-role` pickers
    // this same tile uses two rows up (`linkControl`'s room `<select>`).
    // `pair-dev-role` is a plain visual recipe (background/border/radius/
    // font-size), not select-only, so it is safe to reuse on a text input.
    roomInp.className = "pair-dev-role";
    roomInp.style.marginTop = "8px";
    card.appendChild(roomInp);

    var fb = document.createElement("span");
    fb.className = "action-fb";
    fb.setAttribute("aria-live", "polite");

    var steps = document.createElement("div");
    steps.style.marginTop = "10px";

    var startBtn = document.createElement("button");
    startBtn.type = "button"; startBtn.className = "ctl small primary";
    startBtn.textContent = WavrT("Start a walk");

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
              counter.textContent = WavrT("{n} checks so far", { n: state.samples });
            } else if (r.reason === "window_expired") {
              stopPolling();
              counter.textContent = WavrT("Paused — tell Wavr what is true again.");
            }
          })
          .catch(function () { /* a dropped sample is not worth interrupting a walk */ });
      }, 1500);
    }

    var counter = document.createElement("span");
    counter.className = "pair-dev-meta";

    // `message` is the WHOLE sentence, already translated by the caller. It
    // used to be a label this function glued " — hold still…" onto, and a
    // half-sentence is exactly what cannot be translated: Portuguese does not
    // promise to keep English's clause order.
    function declare(occupied, message) {
      stopPolling();
      api("/api/validation/" + encodeURIComponent(state.session) + "/declare",
          { method: "POST", body: { occupied: occupied } })
        .then(function () {
          counter.textContent = message;
          startPolling();
        })
        .catch(function (e) { actionFeedback(fb, false, String(e.message || e)); });
    }

    function renderRunning() {
      steps.textContent = "";
      var here = document.createElement("button");
      here.type = "button"; here.className = "ctl small primary";
      here.textContent = WavrT("I am in this room");
      here.onclick = function () {
        declare(true, WavrT("Recording: you are here — hold still for a few seconds"));
      };

      var gone = document.createElement("button");
      gone.type = "button"; gone.className = "ctl small";
      gone.textContent = WavrT("I have left");
      gone.onclick = function () {
        declare(false, WavrT("Recording: the room is empty — hold still for a few seconds"));
      };

      var done = document.createElement("button");
      done.type = "button"; done.className = "ctl small";
      done.textContent = WavrT("Finish");
      done.onclick = function () {
        stopPolling();
        api("/api/validation/" + encodeURIComponent(state.session) + "/finish",
            { method: "POST" })
          .then(renderSummary)
          .catch(function (e) { actionFeedback(fb, false, String(e.message || e)); });
      };

      var give = document.createElement("button");
      give.type = "button"; give.className = "ctl small off";
      give.textContent = WavrT("Discard");
      give.onclick = function () {
        stopPolling();
        api("/api/validation/" + encodeURIComponent(state.session) + "/abandon",
            { method: "POST" })
          .then(function () {
            state.session = null;
            steps.textContent = "";
            counter.textContent = "";
            actionFeedback(fb, true, null, WavrT("✓ discarded — nothing recorded"));
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
        // The sensor's own name, then what KIND it is in the product's
        // words. This showed the raw modality, so the panel a person opens
        // precisely when they are not sure they trust the answer was the one
        // speaking in wire values.
        var kind = (typeof modalityLabel === "function")
          ? (modalityLabel(s.modality) || s.modality) : s.modality;
        row(steps, s.sensor_id + " · " + kind, s.verdict, s.note);
      });
      if (!(summary.sensors || []).length) {
        row(steps, WavrT("Nothing was graded"), "",
            WavrT("No sensor was reporting on that room during the walk. A sensor that could not have known is never marked wrong."));
      }
    }

    startBtn.onclick = function () {
      var room = roomInp.value.trim();
      if (!room) { actionFeedback(fb, false, WavrT("which room?")); return; }
      startBtn.disabled = true;
      api("/api/validation/start", { method: "POST", body: { room: room } })
        .then(function (r) {
          state.session = r.session_id;
          state.room = room;
          if (r.resumed) actionFeedback(fb, true, null, WavrT("✓ resumed"));
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
    seeing.appendChild(head(WavrT("What Wavr can see")));
    host.appendChild(seeing);
    await renderCoverage(seeing);
    await renderTopology(seeing);

    var earned = document.createElement("div");
    earned.className = "tile";
    earned.appendChild(head(WavrT("What each sensor has earned"),
                            WavrT("measured against walks you ran")));
    host.appendChild(earned);
    await renderReliability(earned);

    var walk = document.createElement("div");
    walk.className = "tile";
    walk.appendChild(head(WavrT("Check a room"), WavrT("takes a couple of minutes")));
    host.appendChild(walk);
    buildWalk(walk);
  }

  // ---- Privacy: what Wavr is doing, and what it keeps ---------------------
  // Same helpers as Trust, deliberately. The two screens answer neighbouring
  // questions and a second copy of row()/head() would drift from the first edit.

  // Built on every render rather than once at load. `WavrT` answers in
  // whatever language is active when it is CALLED, so a table frozen at parse
  // time would still be in the old language after somebody switches. The KEYS
  // are the API's own field names and never translate.
  function postureRows() {
    return [
      ["internet_required", WavrT("Internet needed"),
       function (v) { return v ? WavrT("yes") : WavrT("no"); }],
      ["account_required", WavrT("Account needed"),
       function (v) { return v ? WavrT("yes") : WavrT("no"); }],
      ["telemetry", WavrT("Telemetry"), function (v) { return String(v); }],
      ["external_connections", WavrT("External connections"),
       function (v) { return v === 0 ? WavrT("none enabled") : WavrT("{n} enabled", { n: v }); }],
      ["lan_access", WavrT("Other devices may connect"),
       function (v) { return v ? WavrT("yes") : WavrT("no — this machine only"); }],
      ["cameras_enabled", WavrT("Cameras switched on"),
       function (v) { return v === 0 ? WavrT("none") : String(v); }],
      ["network_scanning", WavrT("Looking at the network"),
       function (v) { return v ? WavrT("yes") : WavrT("no"); }],
      ["identity_labels", WavrT("Naming devices after people"),
       function (v) { return v ? WavrT("yes") : WavrT("no"); }]
    ];
  }

  async function renderPosture(into) {
    var body = await read("/api/privacy/posture", into, WavrT("Current posture"));
    if (!body) return;
    if (body.available === false) {
      row(into, WavrT("Current posture"), WavrT("cannot be read right now"),
          (body && body.note) || WavrT("This is not a statement that nothing is enabled."));
      return;
    }
    postureRows().forEach(function (spec) {
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
    var body = await read("/api/privacy/data", into, WavrT("What Wavr keeps"));
    if (!body) return;
    var cats = (body && body.categories) || [];
    if (!cats.length) return;

    // Never-stored first, because that is the answer people actually came for.
    var order = { never: 0, session: 1, persistent: 2 };
    var word = {
      never: WavrT("never stored"),
      session: WavrT("in memory only — a restart clears it"),
      persistent: WavrT("stored on this machine")
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
          value += " · " + WavrT("not counted");
        } else if (c.counts) {
          // The number means something narrower than "rows in a table", and the
          // backend said what. Never render "7 rows" for "7 enabled". That word
          // is the backend's own, and is left in the language the backend sent.
          value += " · " + c.rows + " " + c.counts;
        } else {
          value += " · " + WavrT("{n} row|{n} rows", { n: c.rows });
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
  // A function for the same reason `postureRows` is one: translated at render,
  // not at parse. The keys are the API's reach values and never translate.
  function reachWords() {
    return {
      local: WavrT("stays on this machine"),
      lan: WavrT("stays on your network"),
      internet: WavrT("contacts a server, tells it nothing about your Space"),
      cloud: WavrT("a third party receives something about your Space")
    };
  }

  async function renderProviders(into) {
    var body = await read("/api/privacy/providers", into,
                          WavrT("What Wavr can connect to"));
    if (!body) return;
    var groups = (body && body.by_reach) || {};
    var reachWord = reachWords();
    ["local", "lan", "internet", "cloud"].forEach(function (reach) {
      (groups[reach] || []).forEach(function (p) {
        var bits = [reachWord[reach] || reach];
        if (p.requires && p.requires.length) {
          // The requirement NAMES come from the provider catalogue and stay in
          // the language the backend speaks; the sentence around them does not.
          bits.push(WavrT("needs {what}", { what: p.requires.join(WavrT(" and ")) }));
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

  /* Which applications can read what about this house.
   *
   * The half of the privacy picture the provider list does not cover. A
   * provider says where evidence COMES FROM; this says where it GOES — and a
   * household that can see the first without the second knows only half of
   * what it agreed to.
   *
   * A device with no grant is shown too, as "presence only". Listing only the
   * granted ones would make the default invisible, and the default is the
   * answer for almost every device. */
  function scopeWords() {
    return {
      "room.presence": WavrT("whether anybody is in a room"),
      "room.count": WavrT("how many people"),
      "room.position": WavrT("whereabouts within a room"),
      "anchors.read": WavrT("the named places you created"),
      "devices.read": WavrT("which screens and speakers are where"),
      "events.subscribe": WavrT("live changes as they happen"),
    };
  }

  async function renderExperienceAccess(into) {
    var body = await read("/api/experience/grants", into,
                          WavrT("What applications can read"));
    if (!body) return;
    var grants = (body && body.grants) || [];
    if (!grants.length) {
      row(into, WavrT("No application has been granted anything"), "",
          WavrT("Every paired device can tell whether a room is occupied, and nothing more. Wavr never tells an application who anybody is."));
      return;
    }
    var scopeWord = scopeWords();
    grants.forEach(function (g) {
      var words = (g.scopes || []).map(function (s) {
        return scopeWord[s] || s;
      });
      row(into, g.experience_id,
          WavrT("on {device}", { device: g.device_id }),
          words.length ? WavrT("Can read: {what}.", { what: words.join(", ") })
                       : WavrT("Presence only."));
    });
    if (body && body.note) {
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = body.note;
      into.appendChild(n);
    }
  }


  // -- Deleting what Wavr learned ---------------------------------------------
  //
  // A screen that lists everything a product keeps about you and offers no way
  // to remove any of it is the wrong end of the sentence, and this one is the
  // privacy screen.
  //
  // Two rules the UI has to carry, both from `data_erasure.py`:
  //
  //   * OBSERVATION and SETUP are different requests. What Wavr learned by
  //     watching rebuilds itself; what a person told Wavr does not. They are
  //     never in the same list and never share a button.
  //   * The counts are shown BEFORE the confirm, so "this deletes 41,208
  //     occupancy rows" is a number somebody saw rather than discovered.
  //
  // Two steps, and no native confirm() — same reveal-then-confirm shape as
  // unpairing and device blocking elsewhere in this product.
  async function renderErase(into) {
    var body;
    try { body = await api("/api/privacy/erasable"); } catch (e) {
      var why = document.createElement("p");
      why.className = "panel-note";
      // Not silence. This route is loopback-root only, and a companion reaching
      // it gets 403 — which is a fact about WHERE they are, not a missing
      // feature, and saying so is the difference between the two.
      why.textContent = WavrT("Deleting stored data can only be done at the Core itself, on this machine.")
        + " " + (e && e.message ? "(" + e.message + ")" : "");
      into.appendChild(why);
      return;
    }

    var cats = (body && body.categories) || [];
    var preset = (body && body.default) || [];
    if (!cats.length) return;

    var chosen = {};
    preset.forEach(function (k) { chosen[k] = true; });

    function group(title, note, kind, defaultOpen) {
      var list = cats.filter(function (c) { return c.kind === kind; });
      if (!list.length) return;
      var wrap = document.createElement(defaultOpen ? "div" : "details");
      if (!defaultOpen) {
        var sum = document.createElement("summary");
        sum.textContent = title;
        wrap.appendChild(sum);
      } else {
        var h = document.createElement("div");
        h.className = "pair-dev-row";
        var hb = document.createElement("b"); hb.textContent = title;
        h.appendChild(hb);
        wrap.appendChild(h);
      }
      var n = document.createElement("p");
      n.className = "panel-note";
      n.textContent = note;
      wrap.appendChild(n);

      list.forEach(function (c) {
        var r = document.createElement("label");
        r.className = "pair-dev-row";
        var box = document.createElement("input");
        box.type = "checkbox";
        box.checked = !!chosen[c.key];
        box.addEventListener("change", function () {
          chosen[c.key] = box.checked;
          update();
        });
        var left = document.createElement("span");
        var b = document.createElement("b"); b.textContent = c.label;
        left.appendChild(b);
        var m = document.createElement("span");
        m.className = "pair-dev-meta";
        // A row count is the honest size of the thing. "not counted" when the
        // table could not be read — never a comfortable zero.
        m.textContent = " \u00b7 " + (c.rows === null || c.rows === undefined
          ? WavrT("not counted")
          : WavrT("{n} row|{n} rows", { n: c.rows }));
        left.appendChild(m);
        var what = document.createElement("div");
        what.className = "pair-dev-note";
        what.textContent = c.what;
        r.appendChild(box); r.appendChild(left); r.appendChild(what);
        wrap.appendChild(r);
      });
      into.appendChild(wrap);
    }

    group(WavrT("What Wavr learned by watching"),
          WavrT("Occupancy history, readings, devices it saw, things it noticed. Deleting this costs accuracy that rebuilds itself, and nothing else."),
          "observation", true);
    group(WavrT("Also delete what I set up (this is starting over)"),
          WavrT("The Space, the people, the cameras, the paired phones, the routines. This is not a privacy action \u2014 it is undoing your installation, and there is no undo for it."),
          "setup", false);

    var summary = document.createElement("p");
    summary.className = "panel-note";
    into.appendChild(summary);

    var go = document.createElement("button");
    go.type = "button";
    go.className = "ctl small danger";
    into.appendChild(go);

    var confirmRow = document.createElement("div");
    confirmRow.className = "pair-dev-row";
    confirmRow.hidden = true;
    var yes = document.createElement("button");
    yes.type = "button"; yes.className = "ctl small danger";
    var no = document.createElement("button");
    no.type = "button"; no.className = "ctl small";
    no.textContent = WavrT("Cancel");
    confirmRow.appendChild(yes); confirmRow.appendChild(no);
    into.appendChild(confirmRow);

    var fb = document.createElement("div");
    fb.className = "action-fb";
    into.appendChild(fb);

    function picked() {
      return cats.filter(function (c) { return chosen[c.key]; });
    }

    function update() {
      var sel = picked();
      var rows = sel.reduce(function (a, c) {
        return a + (typeof c.rows === "number" ? c.rows : 0);
      }, 0);
      var setup = sel.filter(function (c) { return c.kind === "setup"; });
      summary.textContent = !sel.length
        ? WavrT("Nothing selected.")
        : WavrT("{n} category|{n} categories", { n: sel.length })
          + " \u00b7 " + WavrT("about {n} row|about {n} rows", { n: rows })
          + (setup.length
              ? " \u00b7 " + WavrT("including {n} you set up by hand", { n: setup.length })
              : "");
      go.disabled = !sel.length;
      go.textContent = setup.length
        ? WavrT("Delete this, including my setup\u2026")
        : WavrT("Delete what Wavr learned\u2026");
      // The confirm names the consequence, not the action. "Are you sure?"
      // tells somebody nothing they did not already know.
      yes.textContent = setup.length
        ? WavrT("Yes \u2014 delete {n} rows and undo my setup", { n: rows })
        : WavrT("Yes \u2014 delete {n} rows permanently", { n: rows });
      confirmRow.hidden = true;
    }
    update();

    go.addEventListener("click", function () {
      confirmRow.hidden = false;
      yes.focus();
    });
    no.addEventListener("click", function () {
      confirmRow.hidden = true;
      go.focus();
    });
    yes.addEventListener("click", async function () {
      yes.disabled = true; no.disabled = true;
      var keys = picked().map(function (c) { return c.key; });
      try {
        var out = await api("/api/privacy/erase",
                            { method: "POST", body: { categories: keys, confirm: true } });
        var gone = out && out.total ? out.total : 0;
        var said = WavrT("Deleted {n} row.|Deleted {n} rows.", { n: gone });
        if (typeof actionFeedback === "function") {
          actionFeedback(fb, true, null, said);
        } else {
          fb.textContent = said;
        }
        // Re-read rather than decrementing what is on screen: the numbers a
        // person sees next must come from the database, not from arithmetic
        // this file did on a response.
        setTimeout(function () {
          if (window.__wavrRenderPrivacyData) window.__wavrRenderPrivacyData();
        }, 900);
      } catch (e) {
        if (typeof actionFeedback === "function") {
          actionFeedback(fb, false, (e && e.message) || WavrT("Nothing was deleted."));
        } else {
          fb.textContent = (e && e.message) || WavrT("Nothing was deleted.");
        }
        yes.disabled = false; no.disabled = false;
      }
    });
  }

  async function renderPrivacyData() {
    if (MODE !== "live") return;
    var host = el("privacyDataBody");
    if (!host) return;
    host.textContent = "";

    var now = document.createElement("div");
    now.className = "tile";
    now.appendChild(head(WavrT("What Wavr is doing right now"),
                         WavrT("read live, never cached")));
    host.appendChild(now);
    await renderPosture(now);

    var keeps = document.createElement("div");
    keeps.className = "tile";
    keeps.appendChild(head(WavrT("What Wavr keeps"),
                           WavrT("including what it never keeps")));
    host.appendChild(keeps);
    await renderStored(keeps);

    var can = document.createElement("div");
    can.className = "tile";
    can.appendChild(head(WavrT("What Wavr can connect to"),
                         WavrT("capabilities, not what is switched on")));
    host.appendChild(can);
    await renderProviders(can);

    var apps = document.createElement("div");
    apps.className = "tile";
    apps.appendChild(head(WavrT("What applications can read"),
                          WavrT("and what they can never read")));
    host.appendChild(apps);
    await renderExperienceAccess(apps);

    // Last, deliberately: somebody should have read what is kept before being
    // offered the button that removes it.
    var gone = document.createElement("div");
    gone.className = "tile";
    gone.appendChild(head(WavrT("Delete what Wavr learned"),
                          WavrT("permanent, and it says how much first")));
    host.appendChild(gone);
    await renderErase(gone);
  }

  // Rendered when the section is opened rather than on load: it makes four
  // requests and nobody looks at it every session.
  window.__wavrRenderTrust = renderTrust;
  window.__wavrRenderPrivacyData = renderPrivacyData;
})();
