/* Developer Mode — the tools for building ON Wavr, kept away from everybody else.
 *
 * A classic script, not a module: it reads `MODE` and reuses `head()`-style
 * builders defined earlier in document order, exactly as js/trust.js does. A
 * module would be deferred past those and find them undefined.
 *
 * ## Why this whole section is hidden by default
 *
 * Somebody setting up a camera should never encounter the word "manifest".
 * Every control here answers a question only somebody writing software has, and
 * putting them beside the ordinary settings makes the ordinary user feel they
 * are operating a tool built for somebody else.
 *
 * ## The one thing this screen must never do
 *
 * Let a simulated Space look real. Every scenario writes evidence tagged
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
    t.appendChild(head(WavrT("Developer mode is off"), WavrT("nothing here is running")));
    t.appendChild(note(
      status === 403
        ? WavrT("Turn it on in Core settings. These tools are hidden by default "
          + "because nobody who is not writing software needs them — the routes "
          + "behind them stay protected either way.")
        : WavrT("The Core did not answer. It may be starting.")));
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
    t.appendChild(head(WavrT("This Core"), WavrT("what an application can talk to")));
    Object.keys(body.protocol || {}).forEach(function (k) {
      t.appendChild(row(k.replace(/_/g, " "), "v" + body.protocol[k]));
    });
    t.appendChild(row(WavrT("rooms"), String((body.rooms || []).length)));
    t.appendChild(row(WavrT("rooms with a reading"),
                      String((body.rooms_with_state || []).length)));

    // The load-bearing line on this screen. Placed with the other facts rather
    // than in a corner, because somebody looking at a convincing dashboard needs
    // to be told here that none of it came from a sensor.
    var sim = body.simulated_rooms || [];
    t.appendChild(row(WavrT("simulated rooms"), sim.length ? sim.join(", ") : WavrT("none")));
    if (sim.length && body.note) t.appendChild(note(WavrT(body.note)));
    host.appendChild(t);

    if ((body.experiences || []).length) {
      var e = document.createElement("div");
      e.className = "tile";
      e.appendChild(head(WavrT("Reference experiences"), WavrT("served by this Core")));
      body.experiences.forEach(function (x) {
        var a = document.createElement("a");
        a.href = x.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.className = "pair-dev-row";
        a.textContent = x.name;
        e.appendChild(a);
      });
      e.appendChild(note(WavrT("Each one uses the real SDK against this Core. "
                       + "Read their source — that is what they are for.")));
      host.appendChild(e);
    }
    return body;
  }

  async function renderProviders(host) {
    var body = await get("/api/dev/providers");
    if (!body || body.__status) return;

    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head(WavrT("Providers"), WavrT("what this Core can take evidence from")));
    (body.providers || []).forEach(function (p) {
      t.appendChild(row(WavrT(p.label), WavrT(p.reach)
                        + (p.leaves_the_home ? WavrT(" — leaves your network") : "")));
    });

    var faults = (body.health || []).filter(function (h) { return h.fault; });
    if (faults.length) {
      t.appendChild(note(WavrT("Not working right now: {list}", {list: faults.map(function (h) {
        return h.name + " (" + h.state + ")";
      }).join(", ")})));
    }
    host.appendChild(t);
  }

  async function renderScenarios(host) {
    var body = await get("/api/dev/scenarios");
    if (!body || body.__status) return;

    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head(WavrT("Simulated Space"),
                       WavrT("develop without owning the sensors")));

    (body.scenarios || []).forEach(function (s) {
      var wrap = document.createElement("div");
      wrap.className = "pair-dev-row";

      var text = document.createElement("span");
      var title = document.createElement("strong");
      title.textContent = WavrT(s.title);
      var why = document.createElement("span");
      // `pair-dev-meta`, not `hint`: `.hint` is dimmed inside a `.tile-head` and
      // inherits full contrast anywhere else, so these paragraphs rendered at
      // the same weight as their own titles — a wall of text with no shape.
      why.className = "pair-dev-meta";
      why.textContent = WavrT(s.teaches);
      text.appendChild(title);
      text.appendChild(document.createElement("br"));
      text.appendChild(why);

      var run = document.createElement("button");
      run.type = "button";
      run.className = "ctl small";
      run.textContent = WavrT("Run");
      run.setAttribute(
        "data-tip",
        WavrT("Writes simulated evidence into {rooms} — labelled sim:, and it decays out on its own",
              {rooms: (s.rooms || []).join(", ")}));
      run.onclick = async function () {
        run.disabled = true;
        run.textContent = WavrT("Running…");
        // Realtime, because a person clicking this wants to WATCH it. The
        // instant mode exists for tests, where the point is the end state.
        var out = await post("/api/dev/scenarios/" + encodeURIComponent(s.key)
                             + "/run", { realtime: true });
        run.textContent = out && !out.__status ? WavrT("Running") : WavrT("Failed");
        setTimeout(function () {
          run.disabled = false;
          run.textContent = WavrT("Run");
          renderDeveloper();
        }, Math.max(2000, (s.duration_s || 0) * 1000));
      };

      wrap.appendChild(text);
      wrap.appendChild(run);
      t.appendChild(wrap);
    });

    if (body.note) t.appendChild(note(WavrT(body.note)));
    host.appendChild(t);
  }

  function renderManifestChecker(host) {
    var t = document.createElement("div");
    t.className = "tile";
    t.appendChild(head(WavrT("Manifest checker"), WavrT("is my document correct?")));

    var ta = document.createElement("textarea");
    ta.className = "dev-manifest";
    ta.spellcheck = false;
    ta.rows = 8;
    // The tile heading names this tool for somebody LOOKING at it. A screen
    // reader lands on the field itself and, with nothing here, announced bare
    // "edit text" — the one control on this screen you are meant to type into,
    // and the only one that did not say what it was. No visible label: the
    // heading and its hint sit directly above, and a third line would repeat
    // them to everybody who can see. The name carries the tool's own title, so
    // hearing the field is as good as seeing which tile it sits in.
    ta.setAttribute("aria-label",
                    WavrT("Manifest checker — the document to check"));
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
        out.textContent = WavrT("Not valid JSON: {error}", {error: e.message});
        return;
      }
      var res = await post("/api/dev/manifest/validate", { manifest: parsed });
      if (!res || res.__status) { out.textContent = WavrT("The Core did not answer."); return; }
      if (!res.valid) { out.textContent = res.error; return; }
      out.textContent = res.warnings && res.warnings.length
        ? res.warnings.join("  ")
        : WavrT("Valid. Whether a Space can support it is a different question — "
          + "the compatibility route answers that.");
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
            ? WavrT("Wavr refused to build this file — it failed its own check for "
              + "leaked credentials. Please report it.")
            : WavrT("Could not build the file. This control needs local admin access.");
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

  /* The diagnostic bundle, enriched with what Wavr already reports elsewhere.
   *
   * `/api/diagnostics/bundle` carries sensor coverage and the config shape —
   * everything the export/import machinery already redacts. Three more
   * things somebody diagnosing a dead-on-arrival install needs are already
   * served, separately, by routes this same dashboard already calls:
   *
   *   - `/api/runtime`      — is the Core alive, for how long, doing what.
   *   - `/api/health/doctor` — network/discovery reachability and sensor
   *     faults, as a `report` STRING that the server has already redacted
   *     (see net_doctor.redact_macs). Only that string is used here, never
   *     the route's raw `checks` array — that array is not MAC-scrubbed the
   *     way the text report is, and this file has no reason to be the first
   *     place that regresses.
   *   - `/api/attention`    — anything waiting for a person, as COUNTS only.
   *     Its `items` carry a phone's or a camera's own name — exactly the
   *     kind of word this file promises never to carry.
   *
   * Merged here, client-side, rather than by teaching the server route about
   * three more subsystems: each of these already has its own shape and its
   * own audience, and a second assembly point in the backend is a second
   * place secrecy could regress. A section that fails to fetch is named in
   * `sections_unavailable` rather than silently missing — the same "say what
   * you could not check" rule `/api/attention` itself follows.
   */
  function _safeRuntime(body) {
    if (!body || body.__status) return null;
    // `space` and `headline` dropped deliberately: both can carry the
    // household's own name for their home, which this file promises never to
    // carry — the exact leak this codebase already found once, in an
    // anchor's name (see config_export.py's module docstring).
    return {
      state: body.state, role: body.role, uptime_s: body.uptime_s,
      last_state_age_s: body.last_state_age_s,
      findings: (body.findings || []).map(function (f) {
        return { key: f.key, state: f.state, text: f.text, detail: f.detail };
      }),
    };
  }

  function _safeAttention(body) {
    if (!body || body.__status) return null;
    return { total: body.total, blocking: body.blocking,
             degraded: body.degraded, info: body.info };
  }

  async function buildDiagnosticBundle() {
    var bundle = await get("/api/diagnostics/bundle");
    if (!bundle || bundle.__status) return { __failed: bundle };

    var missing = [];

    var runtime = _safeRuntime(await get("/api/runtime"));
    if (!runtime) missing.push("core_state");
    bundle.core_state = runtime;

    var doctor = await get("/api/health/doctor");
    var report = doctor && !doctor.__status ? (doctor.report || null) : null;
    if (!report) missing.push("network_and_sensor_report");
    bundle.network_and_sensor_report = report;

    var attention = _safeAttention(await get("/api/attention"));
    if (!attention) missing.push("pending_items");
    bundle.pending_items = attention;

    if (missing.length) bundle.sections_unavailable = missing;
    return bundle;
  }

  function wireBundleDownload() {
    var btn = el("exportBundleBtn");
    if (!btn) return;
    btn.onclick = async function () {
      var fb = el("backupFb");
      btn.disabled = true;
      // Reuses the catalogue's existing generic wait state rather than
      // declaring a new sentence: three more requests now happen behind this
      // click (see buildDiagnosticBundle), and "Loading…" is already
      // translated everywhere else this dashboard waits on one.
      if (fb) {
        fb.className = "action-fb";
        fb.textContent = WavrT("Loading…");
      }
      var body = await buildDiagnosticBundle();
      btn.disabled = false;
      if (body.__failed !== undefined) {
        if (fb) {
          fb.className = "action-fb err";
          fb.textContent = body.__failed && body.__failed.__status === 500
            ? WavrT("Wavr refused to build this file — it failed its own check for "
              + "leaked credentials. Please report it.")
            : WavrT("Could not build the file. This control needs local admin access.");
        }
        return;
      }
      download("wavr-diagnostics-" + stamp() + ".json", body);
      if (fb) {
        // The SAME sentence the old, simpler download used — still true of
        // this file (nothing about who was where, no credentials), and it
        // means this change adds no new catalogue entry a translator has not
        // yet seen. `sections_unavailable`, when present, still travels
        // inside the saved FILE itself for whoever reads it.
        fb.className = "action-fb";
        fb.textContent = WavrT("Saved. Sensor health and the event stream — no "
          + "credentials, no coordinates, and no record of who was where.");
      }
    };
  }

  // -- Restore: the other half of an export ------------------------------------
  //
  // Two steps on purpose. An import replaces a floor plan that took somebody an
  // evening to draw, so it is shown before it is applied, and the apply carries
  // the digest the preview returned — a person cannot be shown the consequences
  // of one file and apply another.

  async function postDetailed(path, body) {
    // `post` above collapses every failure to a status code. Here the DETAIL is
    // the message: "this is not the file you previewed" and "preview it first"
    // are different problems with different next actions.
    try {
      var r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Wavr-Local": "1" },
        body: JSON.stringify(body || {}),
      });
      var data = null;
      try { data = await r.json(); } catch (e) { data = null; }
      return { ok: r.ok, status: r.status, data: data };
    } catch (e) {
      return { ok: false, status: 0, data: null };
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function wireImport() {
    var input = el("importConfigFile");
    var host = el("importPreview");
    var fb = el("backupFb");
    if (!input || !host) return;

    function say(kind, text) {
      if (!fb) return;
      fb.className = kind === "err" ? "action-fb err" : "action-fb";
      fb.textContent = text;
    }

    input.onchange = async function () {
      var file = input.files && input.files[0];
      input.value = "";                      // so the same file can be re-picked
      if (!file) return;
      host.hidden = true;
      say("", WavrT("Reading {file}…", {file: file.name}));

      var config;
      try {
        config = JSON.parse(await file.text());
      } catch (e) {
        say("err", WavrT("That file is not readable JSON. Pick the file Wavr "
                 + "downloaded, not a screenshot or a zip of it."));
        return;
      }

      var seen = await postDetailed("/api/config/preview", { config: config });
      if (!seen.ok) {
        say("err", (seen.data && seen.data.detail)
                   || WavrT("Wavr could not read that file."));
        return;
      }
      var p = seen.data;
      var rows = [];
      rows.push(WavrT("{name} — {rooms}, {anchors}, {cameras}.", {
        name: "<b>" + esc((p.space && p.space.name) || WavrT("This file")) + "</b>",
        rooms: WavrT("{n} room|{n} rooms", {n: p.rooms_incoming.length}),
        anchors: WavrT("{n} anchor|{n} anchors", {n: p.anchors_incoming}),
        cameras: WavrT("{n} camera|{n} cameras", {n: p.cameras_incoming}),
      }));
      // The destructive part, first and in a person's words.
      if (p.rooms_lost.length) {
        rows.push('<span class="sw-warn">'
                  + WavrT("These rooms exist here and NOT in the file, and importing "
                          + "removes them: {rooms}.",
                          {rooms: "<b>" + esc(p.rooms_lost.join(", ")) + "</b>"})
                  + "</span>");
      }
      if (p.rooms_replaced.length) {
        rows.push(WavrT("Replaced: {rooms}.", {rooms: esc(p.rooms_replaced.join(", "))}));
      }
      if (p.secrets_needed && p.secrets_needed.length) {
        rows.push(WavrT("You will have to enter again: {items}.",
                        {items: esc(p.secrets_needed.join(", "))}));
      }
      rows.push('<div class="controls-row" style="margin-top:10px">'
                + '<button type="button" class="ctl small primary" id="importGoBtn">'
                + esc(WavrT("Replace my setup with this file")) + "</button>"
                + '<button type="button" class="ctl small" id="importCancelBtn">'
                + esc(WavrT("Cancel")) + "</button></div>");
      host.innerHTML = rows.map(function (r) { return "<p>" + r + "</p>"; }).join("");
      host.hidden = false;
      say("", WavrT("Nothing has been written yet."));

      el("importCancelBtn").onclick = function () {
        host.hidden = true;
        say("", WavrT("Cancelled. Nothing was changed."));
      };
      el("importGoBtn").onclick = async function () {
        el("importGoBtn").disabled = true;
        var done = await postDetailed("/api/config/import",
                                      { config: config, confirm_digest: p.digest });
        if (!done.ok) {
          say("err", (done.data && done.data.detail) || WavrT("The import failed."));
          el("importGoBtn").disabled = false;
          return;
        }
        var r = done.data;
        var lines = [WavrT("Restored {rooms} and {anchors}.", {
          rooms: WavrT("{n} room|{n} rooms", {n: r.written.rooms}),
          anchors: WavrT("{n} anchor|{n} anchors", {n: r.written.anchors}),
        })];
        // Everything that did NOT happen is said here rather than discovered
        // later. A silent skip is how somebody finds out in a month.
        if (r.anchors_skipped && r.anchors_skipped.length) {
          lines.push(WavrT("Skipped, because this file has no such room: {anchors}.",
                     {anchors: esc(r.anchors_skipped.map(function (a) {
                         return a.name + " (" + a.room + ")"; }).join(", "))}));
        }
        if (r.cameras_to_re_add && r.cameras_to_re_add.length) {
          lines.push(WavrT("Add these cameras again with their stream address: {cameras}.",
                     {cameras: esc(r.cameras_to_re_add.map(function (c) {
                         return c.name; }).join(", "))}));
        }
        if (r.problems && r.problems.length) {
          lines.push('<span class="sw-warn">' + esc(r.problems.join("; "))
                     + "</span>");
        }
        host.innerHTML = lines.map(function (l) { return "<p>" + l + "</p>"; }).join("");
        say("", WavrT("Done. Reload to see your floor plan."));
      };
    };
  }

  wireImport();

  // -- Updates -----------------------------------------------------------------
  //
  // The tile renders `GET /api/updates`, which makes no network request of its
  // own. The one thing that DOES reach outward is behind the button, and the
  // button only appears when the operator has already switched that connector
  // on — an outward action must not be one click away from somebody who never
  // agreed to it.
  //
  // And the tile only asks the Core anything when there IS a Core: see the
  // mode gate at the bottom of this file.

  async function renderUpdates() {
    var how = el("updateHow");
    if (!how) return;
    var body = await get("/api/updates");
    if (!body || body.__status) {
      how.textContent = WavrT("Wavr could not read its own update settings.");
      return;
    }
    el("updateVersion").textContent =
      WavrT("running {version} · {channel}",
            {version: body.running || "?", channel: body.channel || WavrT("unknown")});

    // The instruction for THIS install, which is the actually useful half.
    how.textContent = body.how_to_update ? WavrT(body.how_to_update) : "";

    var why = el("updateWhy");
    if (body.why_no_check) {
      why.hidden = false;
      why.textContent = WavrT(body.why_no_check);
    } else {
      why.hidden = true;
    }
    // `up_to_date` is a TRISTATE. `null` means nobody has looked, which is not
    // the same as "you are up to date" — rendering the two the same way is the
    // exact class of lie this codebase keeps having to remove.
    var fb = el("updateFb");
    if (fb) {
      fb.className = "action-fb";
      fb.textContent = body.up_to_date === null
        ? (body.check_enabled ? WavrT("Not checked yet.") : "")
        : body.up_to_date
          ? WavrT("You are on the latest release.")
          : WavrT("A newer release exists. Follow the instruction above.");
    }
    el("updateActions").hidden = !body.check_enabled;
  }

  var checkBtn = el("updateCheckBtn");
  if (checkBtn) {
    checkBtn.onclick = async function () {
      checkBtn.disabled = true;
      el("updateFb").textContent = WavrT("Reading the release list…");
      var out = await post("/api/updates/check", {});
      checkBtn.disabled = false;
      if (!out || out.__status) {
        el("updateFb").className = "action-fb err";
        el("updateFb").textContent =
          WavrT("Could not reach the release list. Nothing was sent.");
        return;
      }
      renderUpdates();
    };
  }

  /* The SAME gate `renderDeveloper` applies, and for a harder reason.
   *
   * This ran unconditionally, so opening the off-localhost demo fetched
   * `/api/updates` from a host with no Core behind it. The demo states on
   * screen that "this page makes no backend calls" — a sentence the network
   * tab contradicted. A promise about egress is not a promise until the only
   * thing that can break it is gone.
   *
   * `MODE` undefined means the shell has not been loaded around this module at
   * all (a unit harness), and the rest of the file treats that as live rather
   * than as a reason to render nothing. */
  function wireUpdates() {
    if (typeof MODE === "undefined" || MODE === "live") {
      renderUpdates();
      return;
    }
    // Not left on "Working out how this copy of Wavr updates…" — a placeholder
    // that can never resolve is its own small lie, and on a demo it would sit
    // there for as long as the page was open.
    var how = el("updateHow");
    if (how) {
      how.textContent = WavrT("Updates are handled on the Core itself, not from here.");
    }
  }

  /* Deferred to DOMContentLoaded, which is not tidiness.
   *
   * `#updateHow` is a `data-i18n` paragraph, and `js/i18n.js` snapshots the
   * original English of every one of those as the PARSER inserts it, then
   * restores it over the whole document on DOMContentLoaded. Anything this
   * module wrote there while the page was still parsing was therefore rubbed
   * out a moment later. `renderUpdates` escaped it only by accident — it
   * awaits a request, so its write usually lands after that pass — and the
   * sentence above, being synchronous, did not escape it at all.
   *
   * `js/i18n.js` is tag one, so its listener is registered before this one and
   * runs first: the restore happens, and then this writes over it for good. */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireUpdates);
  } else {
    wireUpdates();
  }

  /* Backup and diagnostics need a Core, and the demo has none.
   *
   * These fire only on a click, so they were not part of the "this page makes
   * no backend calls" promise being broken on LOAD — but the promise is about
   * the page, not about the load, and somebody who presses Export in the demo
   * both makes a request and gets a failure that reads like a bug in Wavr. The
   * tile says what it needs instead, in the same words the rest of this module
   * uses for the same situation. */
  if (typeof MODE !== "undefined" && MODE !== "live") {
    ["exportConfigBtn", "exportBundleBtn"].forEach(function (id) {
      var b = el(id);
      if (b) b.disabled = true;
    });
    var why = el("backupFb");
    if (why) {
      why.className = "action-fb";
      // The guard above is `MODE !== "live"`, which is the demo AND a paired
      // companion. Saying "in this demo" to a phone somebody paired is false,
      // and it is the phrasing `new-devices.js` and `whoshome.js` already use
      // for the same guard: one sentence covering both, rather than a branch
      // the next reader has to remember to add.
      why.textContent = WavrT("Backup and diagnostics need the hub itself — not "
        + "available in this demo or on a view-only companion device.");
    }
    return;
  }

  wireDownload("exportConfigBtn", "/api/config/export",
               "wavr-setup-{date}.json",
               WavrT("Saved. Camera URLs and tokens are NOT in it — re-enter those "
               + "after importing."));
  wireBundleDownload();
})();
