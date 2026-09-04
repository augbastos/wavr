/* Developer Mode — the tools for building ON Wavr, kept away from everybody else.
 *
 * A classic script, not a module: it reads `MODE` and reuses `head()`-style
 * builders defined earlier in document order, exactly as js/trust.js does. A
 * module would be deferred past those and find them undefined.
 *
 * ## Why this whole section is hidden by default
 *
 * A household setting up a camera should never encounter the word "manifest".
 * Every control here answers a question only somebody writing software has, and
 * putting them beside the ordinary settings makes the ordinary user feel they
 * are operating a tool built for somebody else.
 *
 * ## The one thing this screen must never do
 *
 * Let a simulated house look real. Every scenario writes evidence tagged
 * `sim:`, and the Core reports which rooms currently hold any — so this screen
 * shows that live, in the place somebody would be looking when they were most
 * likely to be fooled by it.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
(function () {
  "use strict";

  var el = function (id) { return document.getElementById(id); };

  function head(title, hint) {
    var h = document.createElement("div");
    h.className = "tile-head";
    var t = document.createElement("h2");
    t.textContent = title;
    h.appendChild(t);
    if (hint) {
      var s = document.createElement("span");
      s.className = "hint";
      s.textContent = hint;
      h.appendChild(s);
    }
    return h;
  }

  /* The SAME row shape js/trust.js builds — `pair-dev-row` / `pair-dev-meta`
   * are the panel's existing vocabulary. An earlier draft of this file invented
   * `kv-row` / `kv-k` / `kv-v`, which have no styles anywhere: the section would
   * have rendered as unstyled text, and it would have looked like a bug in the
   * data rather than a missing rule. */
  function row(label, value) {
    var d = document.createElement("div");
    d.className = "pair-dev-row";
    var left = document.createElement("span");
    var b = document.createElement("b");
    b.textContent = label;
    left.appendChild(b);
    if (value) {
      var m = document.createElement("span");
      m.className = "pair-dev-meta";
      m.textContent = " · " + value;
      left.appendChild(m);
    }
    d.appendChild(left);
    return d;
  }

  function note(text) {
    var p = document.createElement("p");
    p.className = "panel-note";
    p.textContent = text;
    return p;
  }

  async function get(path) {
    try {
      var r = await fetch(path, { headers: { "X-Wavr-Local": "1" } });
      if (!r.ok) return { __status: r.status };
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function post(path, body) {
    try {
      var r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Wavr-Local": "1" },
        body: JSON.stringify(body || {}),
      });
      return r.ok ? await r.json() : { __status: r.status };
    } catch (e) {
      return null;
    }
  }

  function renderOff(host, status) {
    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head("Developer mode is off", "nothing here is running"));
    t.appendChild(note(
      status === 403
        ? "Turn it on in Core settings. These tools are hidden by default "
          + "because nobody who is not writing software needs them — the routes "
          + "behind them stay protected either way."
        : "The Core did not answer. It may be starting."));
    host.appendChild(t);
  }

  async function renderStatus(host) {
    var body = await get("/api/dev/status");
    if (!body || body.__status) {
      renderOff(host, body && body.__status);
      return null;
    }

    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head("This Core", "what an application can talk to"));
    Object.keys(body.protocol || {}).forEach(function (k) {
      t.appendChild(row(k.replace(/_/g, " "), "v" + body.protocol[k]));
    });
    t.appendChild(row("rooms", String((body.rooms || []).length)));
    t.appendChild(row("rooms with a reading",
                      String((body.rooms_with_state || []).length)));

    // The load-bearing line on this screen. Placed with the other facts rather
    // than in a corner, because somebody looking at a convincing dashboard needs
    // to be told here that none of it came from a sensor.
    var sim = body.simulated_rooms || [];
    t.appendChild(row("simulated rooms", sim.length ? sim.join(", ") : "none"));
    if (sim.length) t.appendChild(note(body.note));
    host.appendChild(t);

    if ((body.experiences || []).length) {
      var e = document.createElement("div");
      e.className = "tile";
      e.appendChild(head("Reference experiences", "served by this Core"));
      body.experiences.forEach(function (x) {
        var a = document.createElement("a");
        a.href = x.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.className = "pair-dev-row";
        a.textContent = x.name;
        e.appendChild(a);
      });
      e.appendChild(note("Each one uses the real SDK against this Core. "
                       + "Read their source — that is what they are for."));
      host.appendChild(e);
    }
    return body;
  }

  async function renderProviders(host) {
    var body = await get("/api/dev/providers");
    if (!body || body.__status) return;

    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head("Providers", "what this Core can take evidence from"));
    (body.providers || []).forEach(function (p) {
      t.appendChild(row(p.label, p.reach
                        + (p.leaves_the_home ? " — leaves the home" : "")));
    });

    var faults = (body.health || []).filter(function (h) { return h.fault; });
    if (faults.length) {
      t.appendChild(note("Not working right now: " + faults.map(function (h) {
        return h.name + " (" + h.state + ")";
      }).join(", ")));
    }
    host.appendChild(t);
  }

  async function renderScenarios(host) {
    var body = await get("/api/dev/scenarios");
    if (!body || body.__status) return;

    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head("Simulated house",
                       "develop without owning the sensors"));

    (body.scenarios || []).forEach(function (s) {
      var wrap = document.createElement("div");
      wrap.className = "pair-dev-row";

      var text = document.createElement("span");
      var title = document.createElement("strong");
      title.textContent = s.title;
      var why = document.createElement("span");
      // `pair-dev-meta`, not `hint`: `.hint` is dimmed inside a `.tile-head` and
      // inherits full contrast anywhere else, so these paragraphs rendered at
      // the same weight as their own titles — a wall of text with no shape.
      why.className = "pair-dev-meta";
      why.textContent = s.teaches;
      text.appendChild(title);
      text.appendChild(document.createElement("br"));
      text.appendChild(why);

      var run = document.createElement("button");
      run.type = "button";
      run.className = "ctl small";
      run.textContent = "Run";
      run.setAttribute(
        "data-tip",
        "Writes simulated evidence into " + (s.rooms || []).join(", ")
          + " — labelled sim:, and it decays out on its own");
      run.onclick = async function () {
        run.disabled = true;
        run.textContent = "Running…";
        // Realtime, because a person clicking this wants to WATCH it. The
        // instant mode exists for tests, where the point is the end state.
        var out = await post("/api/dev/scenarios/" + encodeURIComponent(s.key)
                             + "/run", { realtime: true });
        run.textContent = out && !out.__status ? "Running" : "Failed";
        setTimeout(function () {
          run.disabled = false;
          run.textContent = "Run";
          renderDeveloper();
        }, Math.max(2000, (s.duration_s || 0) * 1000));
      };

      wrap.appendChild(text);
      wrap.appendChild(run);
      t.appendChild(wrap);
    });

    t.appendChild(note(body.note));
    host.appendChild(t);
  }

  function renderManifestChecker(host) {
    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head("Manifest checker", "is my document correct?"));

    var ta = document.createElement("textarea");
    ta.className = "dev-manifest";
    ta.spellcheck = false;
    ta.rows = 8;
    ta.value = JSON.stringify({
      id: "my-experience",
      name: "My Experience",
      requires: ["presence"],
      optional: ["count"],
      scopes: ["room.presence", "room.count"],
    }, null, 2);

    var out = document.createElement("p");
    out.className = "panel-note";
    out.setAttribute("aria-live", "polite");

    var timer;
    async function check() {
      var parsed;
      try {
        parsed = JSON.parse(ta.value);
      } catch (e) {
        out.textContent = "Not valid JSON: " + e.message;
        return;
      }
      var res = await post("/api/dev/manifest/validate", { manifest: parsed });
      if (!res || res.__status) { out.textContent = "The Core did not answer."; return; }
      if (!res.valid) { out.textContent = res.error; return; }
      out.textContent = res.warnings && res.warnings.length
        ? res.warnings.join("  ")
        : "Valid. Whether a Space can support it is a different question — "
          + "the compatibility route answers that.";
    }
    ta.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(check, 400);
    });

    t.appendChild(ta);
    t.appendChild(out);
    host.appendChild(t);
    check();
  }

  async function renderDeveloper() {
    if (typeof MODE !== "undefined" && MODE !== "live") return;
    var host = el("developerBody");
    if (!host) return;
    host.textContent = "";

    var status = await renderStatus(host);
    if (!status) return;          // off, or unreachable: nothing else is useful
    await renderProviders(host);
    await renderScenarios(host);
    renderManifestChecker(host);
  }

  // Rendered when the section is opened rather than on load — it makes three
  // requests, and on a normal install this section is switched off entirely.
  window.__wavrRenderDeveloper = renderDeveloper;

  /* Backup and diagnostics.
   *
   * Lives in this file rather than the main script only because this is where
   * the download plumbing already is; the BUTTONS are in Core settings, where a
   * normal person looks for a backup. Nothing here is developer-mode gated.
   *
   * The download is built client-side from the JSON the Core already served,
   * which keeps the Core free of a second content-type path and means the file
   * a person saves is byte-identical to what the API returned. */
  function download(name, body) {
    var blob = new Blob([JSON.stringify(body, null, 2)],
                        { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoked on the next tick: revoking synchronously races the click on some
    // browsers and produces a silently empty file.
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  function stamp() {
    return new Date().toISOString().slice(0, 10);
  }

  function wireDownload(buttonId, path, filename, describe) {
    var btn = el(buttonId);
    if (!btn) return;
    btn.onclick = async function () {
      var fb = el("backupFb");
      btn.disabled = true;
      var body = await get(path);
      btn.disabled = false;
      if (!body || body.__status) {
        // The Core refuses a file that fails its own leak audit, and a 500 here
        // means exactly that. Say so, rather than "download failed" — this is a
        // bug worth reporting, not a network blip to retry.
        if (fb) {
          fb.className = "action-fb err";
          fb.textContent = body && body.__status === 500
            ? "Wavr refused to build this file — it failed its own check for "
              + "leaked credentials. Please report it."
            : "Could not build the file. This control needs local admin access.";
        }
        return;
      }
      download(filename.replace("{date}", stamp()), body);
      if (fb) {
        fb.className = "action-fb";
        fb.textContent = describe;
      }
    };
  }

  wireDownload("exportConfigBtn", "/api/config/export",
               "wavr-setup-{date}.json",
               "Saved. Camera URLs and tokens are NOT in it — re-enter those "
               + "after importing.");
  wireDownload("exportBundleBtn", "/api/diagnostics/bundle",
               "wavr-diagnostics-{date}.json",
               "Saved. Sensor health and the event stream — no credentials, no "
               + "coordinates, and no record of who was where.");
})();
