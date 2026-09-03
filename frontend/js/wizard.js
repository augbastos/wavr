/* First-run setup wizard — lifted verbatim out of index.html.
   A classic script, NOT a module: it reads `MODE` from the main script's
   global scope and must keep executing in document order. See the block
   comment above its <script src> tag in index.html for why it exists.
 */
/* First-run wizard. Vanilla, no framework, no build step -- same constraints as
   the rest of this file. */
(function () {
  "use strict";

  // Loopback only. `MODE` is resolved far above (~line 4040) from the host; a
  // LAN companion is in "companion" mode and gets the pairing screen instead,
  // and the offline demo is "simulated" and must make zero network requests --
  // so bailing out here is what keeps that promise intact.
  var mode = (window.WAVR_MOBILE && window.WAVR_MOBILE.mode) ||
    (location.hostname === "localhost" || location.hostname === "127.0.0.1" ? "live" : "other");
  if (mode !== "live") return;
  // The Core kiosk panel (?core) is an ambient face, not an admin surface.
  if (new URLSearchParams(location.search).has("core") || window.WAVR_CORE) return;

  var el = function (id) { return document.getElementById(id); };
  var wiz = el("setupWizard");
  var state = { step: 0, path: null, kind: "home", name: "", owner: "",
                scan: null, status: null, functions: null, busy: false };

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

  function fail(err) {
    var box = el("swErr");
    box.hidden = false;
    // Never render a raw stack or a bare status code at a first-time user.
    box.textContent = (err && err.message) ? err.message
      : "Something went wrong. Wavr is still running — you can try again.";
  }
  function clearErr() { el("swErr").hidden = true; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var STEPS = 4;
  function steps(n) {
    var out = "";
    for (var i = 0; i < STEPS; i++) {
      out += '<i class="' + (i < n ? "done" : i === n ? "now" : "") + '"></i>';
    }
    el("swSteps").innerHTML = out;
  }

  function actions(list) {
    el("swActions").innerHTML = list.map(function (a, i) {
      return '<button class="sw-btn' + (a.primary ? " primary" : "") + '" data-a="' + i +
        '"' + (a.disabled ? " disabled" : "") + '>' + esc(a.label) + "</button>";
    }).join("");
    el("swActions").querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        clearErr();
        list[Number(b.dataset.a)].go();
      });
    });
  }

  // -- Step 0: what are we doing here? ---------------------------------------
  function stepStart() {
    steps(0);
    var legacy = state.status && state.status.existing_devices > 0;
    el("swTitle").textContent = legacy ? "Let's finish setting up Wavr" : "Welcome to Wavr";
    el("swLede").textContent = legacy
      ? "Wavr is already running on this machine with " + state.status.existing_devices +
        " paired device" + (state.status.existing_devices === 1 ? "" : "s") +
        ". Give it a name and nothing you've set up changes."
      : "Wavr turns this machine and your network into a space it can understand. "
        + "This takes about a minute.";

    var opts = [];
    if (legacy) {
      opts.push({ id: "adopt", t: "Name what's already here",
                  d: "Keeps every paired device, every camera and every setting exactly as it is." });
    }
    opts.push({ id: "create", t: legacy ? "Start fresh instead" : "Create a new Space",
                d: "This machine becomes the Core — the part of Wavr that does the thinking." });
    opts.push({ id: "join", t: "Join a Space that already exists",
                d: "Another device on this network is already running Wavr." });

    el("swBody").innerHTML = '<div class="sw-choice">' + opts.map(function (o) {
      return '<button class="sw-opt" data-p="' + o.id + '" aria-pressed="' +
        (state.path === o.id) + '"><span><b>' + esc(o.t) + "</b><span>" +
        esc(o.d) + "</span></span></button>";
    }).join("") + "</div>";

    el("swBody").querySelectorAll("[data-p]").forEach(function (b) {
      b.addEventListener("click", function () {
        state.path = b.dataset.p;
        stepStart();
        render();
      });
    });

    actions([{ label: "Continue", primary: true, disabled: !state.path, go: next }]);
  }

  // -- Step 1: name it -------------------------------------------------------
  var KINDS = [["home", "Home"], ["apartment", "Apartment"], ["office", "Office"],
               ["shop", "Shop"], ["workshop", "Workshop"], ["other", "Somewhere else"]];

  function stepName() {
    steps(1);
    el("swTitle").textContent = "What is this place?";
    el("swLede").textContent = "Wavr uses this to pick sensible room names and defaults. "
      + "You can change it at any time.";
    el("swBody").innerHTML =
      '<div class="sw-grid">' + KINDS.map(function (k) {
        return '<button class="sw-opt" data-k="' + k[0] + '" aria-pressed="' +
          (state.kind === k[0]) + '"><span><b>' + esc(k[1]) + "</b></span></button>";
      }).join("") + "</div>" +
      '<div class="sw-card" style="margin-top:14px">' +
        '<label for="swName">What should Wavr call it?</label>' +
        '<input type="text" id="swName" maxlength="64" autocomplete="off" spellcheck="false">' +
        '<p class="sw-note" style="margin-top:10px">This name stays on this machine. ' +
        'Wavr never broadcasts it to your network.</p>' +
      "</div>" +
      '<div class="sw-card" style="margin-top:10px">' +
        '<label for="swOwner">And who are you?</label>' +
        '<input type="text" id="swOwner" maxlength="64" autocomplete="off">' +
        '<p class="sw-note" style="margin-top:10px">You become the Owner — the only ' +
        'person who can hand this Space to someone else.</p>' +
      "</div>";

    var nameIn = el("swName"), ownerIn = el("swOwner");
    nameIn.value = state.name || (state.kind === "home" ? "My Home" : "");
    ownerIn.value = state.owner || "";
    function sync() {
      state.name = nameIn.value;
      state.owner = ownerIn.value;
      el("swActions").querySelector(".primary").disabled = !nameIn.value.trim();
    }
    nameIn.addEventListener("input", sync);
    ownerIn.addEventListener("input", sync);

    el("swBody").querySelectorAll("[data-k]").forEach(function (b) {
      b.addEventListener("click", function () {
        state.kind = b.dataset.k;
        var wasDefault = !state.name || state.name === "My Home";
        if (wasDefault && state.kind === "home") state.name = "My Home";
        stepName();
        render();
      });
    });

    actions([
      { label: "Back", go: back },
      { label: "Continue", primary: true, disabled: !nameIn.value.trim(), go: next }
    ]);
    setTimeout(function () { nameIn.focus(); }, 0);
  }

  // -- Step 2: what is this machine? ----------------------------------------
  function fact(label, value, unknown) {
    return "<div><dt>" + esc(label) + '</dt><dd' +
      (unknown ? ' class="sw-unknown"' : "") + ">" + esc(value) + "</dd></div>";
  }

  function stepScan() {
    steps(2);
    el("swTitle").textContent = "Looking at this device";
    el("swLede").textContent = "Wavr is checking what this machine can do. "
      + "Nothing is switched on and nothing leaves this device.";
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      "Checking…</div>";
    actions([{ label: "Back", go: back }]);

    api("/api/setup/scan", { method: "POST" }).then(function (res) {
      state.scan = res;
      var m = res.manifest, rec = res.recommendation;
      state.functions = rec.functions.slice();

      var caps = [["camera", "Camera"], ["ble", "Bluetooth"], ["wifi", "Wi-Fi"],
                  ["ethernet", "Ethernet"], ["gpu", "Graphics"], ["battery", "Battery"]];
      var factsHtml = fact("System", m.os_version || "Unknown", !m.os_version) +
        fact("Memory", m.ram_mb ? (Math.round(m.ram_mb / 1024 * 10) / 10) + " GB" : "Unknown",
             !m.ram_mb) +
        fact("Processors", m.cpu_count != null ? String(m.cpu_count) : "Unknown",
             m.cpu_count == null) +
        caps.map(function (c) {
          var v = m.capabilities[c[0]];
          // Tristate, honestly: null is "we couldn't tell", never "no".
          return fact(c[1], v === true ? "Yes" : v === false ? "No" : "Couldn't tell",
                      v == null);
        }).join("");

      el("swBody").innerHTML =
        '<div class="sw-rec"><b>Recommended: ' + esc(rec.label) + "</b><ul>" +
          rec.reasons.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") +
        "</ul></div>" +
        '<dl class="sw-facts">' + factsHtml + "</dl>" +
        '<p class="sw-note" style="margin-top:12px">Anything Wavr couldn’t work out is ' +
        'marked <span class="sw-unknown">Couldn’t tell</span> rather than guessed at. ' +
        'You can change what this device does later.</p>';

      actions([{ label: "Back", go: back },
               { label: "Use this setup", primary: true, go: next }]);
    }).catch(function (e) {
      fail(e);
      el("swBody").innerHTML = '<div class="sw-card">Wavr couldn’t check this device. ' +
        "You can still continue — it will start as a Core and you can adjust it later.</div>";
      state.functions = ["core", "client"];
      actions([{ label: "Back", go: back },
               { label: "Continue anyway", primary: true, go: next }]);
    });
  }

  // -- Step 3: commit --------------------------------------------------------
  function stepFinish() {
    steps(3);
    el("swTitle").textContent = "Setting up " + (state.name || "your Space");
    el("swLede").textContent = "";
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      "Creating…</div>";
    el("swActions").innerHTML = "";

    var body = { name: state.name.trim(), kind: state.kind,
                 owner_name: (state.owner || "").trim() || "Owner" };
    var path = "/api/setup/create-space";
    if (state.path === "adopt") {
      path = "/api/setup/adopt";
    } else {
      body.functions = state.functions;
    }

    api(path, { method: "POST", body: body }).then(function (res) {
      steps(4);
      // textContent, so NOT esc() -- escaping here double-encodes, and a Space
      // called "Mum & Dad's" would greet its owner as "Mum &amp; Dad&#39;s".
      el("swTitle").textContent = res.space.name + " is ready";
      el("swLede").textContent = "";
      var core = res.core || {};
      el("swBody").innerHTML =
        '<div class="sw-rec"><b>This device is now your ' +
          (core.status === "primary" ? "Core" : "standby Core") + "</b>" +
          '<ul><li>Wavr will start watching for what is around it.</li>' +
          "<li>You can add phones, tablets and sensors from the Devices tab.</li></ul></div>" +
        (res.adopted_devices
          ? '<p class="sw-note" style="margin-top:12px">' + res.adopted_devices +
            " existing device" + (res.adopted_devices === 1 ? " was" : "s were") +
            " kept exactly as they were.</p>"
          : "") +
        '<p class="sw-note sw-warn" style="margin-top:12px">Right now only this machine ' +
        "can reach Wavr. To use it from your phone, turn on <b>Let other devices " +
        "connect</b> in Settings — Wavr will explain what that changes before " +
        "you do.</p>";
      actions([{ label: "Open Wavr", primary: true, go: done }]);
    }).catch(function (e) {
      fail(e);
      // Deliberately does NOT claim "nothing was changed" -- the client cannot
      // know that. The Core rolls back a half-created Space itself; this says
      // what happened and offers the retry.
      el("swBody").innerHTML = '<div class="sw-card">Setup didn\u2019t finish. ' +
        "Wavr is still running and you can try again.</div>";
      actions([{ label: "Back", go: function () { state.step = 2; render(); } }]);
    });
  }

  // -- Join path -------------------------------------------------------------
  function stepJoin() {
    steps(1);
    el("swTitle").textContent = "Wavr Spaces on this network";
    el("swLede").textContent = "Looking for other devices already running Wavr.";
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      "Searching…</div>";
    actions([{ label: "Back", go: back }]);

    api("/api/setup/nearby").then(function (res) {
      if (!res.available) {
        el("swBody").innerHTML = '<div class="sw-card">Wavr can’t search this ' +
          "network from here. That usually means the optional discovery component " +
          "isn’t installed.<p class=\"sw-note\" style=\"margin-top:10px\">You can " +
          "still set this machine up as its own Space and connect the two later.</p></div>";
      } else if (!res.cores.length) {
        el("swBody").innerHTML = '<div class="sw-card">No other Wavr Cores answered.' +
          '<p class="sw-note" style="margin-top:10px">Make sure the other device is ' +
          "switched on, running Wavr, and on this same Wi-Fi.</p></div>";
      } else {
        el("swBody").innerHTML = '<div class="sw-choice">' + res.cores.map(function (c, i) {
          return '<button class="sw-opt" data-core="' + i + '"><span><b>' + esc(c.name) +
            "</b><span>" + esc(c.host) + ":" + esc(c.port) + "</span></span></button>";
        }).join("") + "</div>" +
        '<p class="sw-note" style="margin-top:12px">Pick the device that is already ' +
        "running your Space.</p>";
        el("swBody").querySelectorAll("[data-core]").forEach(function (b) {
          b.addEventListener("click", function () {
            stepJoinConfirm(res.cores[Number(b.dataset.core)]);
          });
        });
      }
      actions([{ label: "Back", go: back },
               { label: "Search again", go: stepJoin }]);
    }).catch(function (e) {
      fail(e);
      // Replace the spinner. Without this the operator gets an error message
      // above something that spins forever -- the two success branches above
      // both replace it, and this one used not to.
      el("swBody").innerHTML = '<div class="sw-card">Wavr couldn\u2019t search ' +
        "the network just now.</div>";
      actions([{ label: "Back", go: back }, { label: "Try again", go: stepJoin }]);
    });
  }

  // Joining needs one thing this Core cannot discover: what the household is
  // called. The name is deliberately absent from the mDNS advertisement (see
  // /api/setup/nearby), so it comes from the person, who already chose it on the
  // other device. Nothing else is asked -- no address, no port, no id.
  function stepJoinConfirm(core) {
    steps(1);
    el("swTitle").textContent = "Join this Space";
    el("swLede").textContent = "This machine becomes a second Core for it.";
    el("swBody").innerHTML =
      '<div class="sw-card"><b>' + esc(core.name) + "</b>" +
        '<span class="sw-note" style="display:block;margin-top:4px">' +
        esc(core.host) + ":" + esc(core.port) + "</span></div>" +
      '<div class="sw-card" style="margin-top:12px">' +
        '<label for="swJoinName">What did you call this place on that device?</label>' +
        '<input type="text" id="swJoinName" maxlength="64" autocomplete="off" ' +
        'spellcheck="false" placeholder="My Home">' +
        '<p class="sw-note" style="margin-top:10px">Wavr can’t read the name off ' +
        "the network — it is never broadcast — so it needs yours.</p>" +
      "</div>" +
      '<div class="sw-card" style="margin-top:10px">' +
        '<label for="swJoinOwner">And who are you?</label>' +
        '<input type="text" id="swJoinOwner" maxlength="64" autocomplete="off">' +
      "</div>";

    var nameIn = el("swJoinName"), ownerIn = el("swJoinOwner");
    nameIn.value = state.name || "";
    ownerIn.value = state.owner || "";
    function sync() {
      state.name = nameIn.value;
      state.owner = ownerIn.value;
      var p = el("swActions").querySelector(".primary");
      if (p) p.disabled = !nameIn.value.trim();
    }
    nameIn.addEventListener("input", sync);
    ownerIn.addEventListener("input", sync);

    actions([
      { label: "Back", go: stepJoin },
      { label: "Join", primary: true, disabled: !nameIn.value.trim(),
        go: function () { doJoin(core); } }
    ]);
  }

  function doJoin(core) {
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      "Joining…</div>";
    actions([]);
    api("/api/setup/join-space", { method: "POST", body: {
      space_id: core.space_id,
      name: (state.name || "My Home").trim(),
      owner_name: (state.owner || "Owner").trim()
    } }).then(function (res) {
      // Say plainly what this machine is and is NOT. `join-space` records the
      // shared id and stands by; it does not authenticate against the other Core
      // and copies no state. Calling that a warm spare would be a lie the
      // operator only discovers when they need it.
      el("swTitle").textContent = "Joined — one step left";
      el("swLede").textContent = "";
      el("swBody").innerHTML =
        '<div class="sw-card">This machine is now a <b>standby Core</b> for ' +
        esc(res.space && res.space.name ? res.space.name : "your Space") + "." +
        '<p class="sw-note" style="margin-top:10px">It does not mirror the other ' +
        "Core yet. Connect the two and they will agree on who is in charge, and " +
        "notice if they ever disagree.</p></div>" +
        '<div class="sw-card" style="margin-top:10px">Connect them from ' +
        "<b>Settings → Devices → Cores</b> on either machine. It is a " +
        "one-time code compare, so neither trusts the other on the network’s " +
        "word.</div>";
      actions([{ label: "Open Wavr", primary: true, go: done }]);
    }).catch(function (e) {
      fail(e);
      el("swBody").innerHTML = '<div class="sw-card">Wavr couldn’t join that ' +
        "Space. It is still running and nothing on the other device was touched.</div>";
      actions([{ label: "Back", go: function () { stepJoinConfirm(core); } }]);
    });
  }

  // -- Flow ------------------------------------------------------------------
  function render() {
    if (state.path === "join") return stepJoin();
    if (state.step === 0) return stepStart();
    if (state.step === 1) return stepName();
    // Adopting an existing install skips the capability step: the machine is
    // already doing the job, so proposing a job for it would be theatre.
    if (state.step === 2) return state.path === "adopt" ? stepFinish() : stepScan();
    return stepFinish();
  }
  function next() { state.step++; render(); }
  function back() {
    if (state.step === 0) { state.path = null; return render(); }
    state.step--;
    render();
  }
  function done() { location.reload(); }

  // Keep the dashboard behind it inert while the wizard owns the screen, so a
  // tab underneath can't take focus. `__wavrSetAppInert` is the shell's own
  // helper (defined ~11455) -- reuse it rather than inventing a second one.
  function show() {
    wiz.hidden = false;
    wiz.classList.add("show");
    if (window.__wavrSetAppInert) window.__wavrSetAppInert(true);
    if (window.__wavrTrapFocus) window.__wavrTrapFocus(wiz);
    render();
  }

  // One probe on boot. A failure here must NOT block the dashboard: an older
  // Core without these routes, or a Core with the local token set, simply does
  // not get a wizard -- it gets the app it already had.
  api("/api/setup/status").then(function (s) {
    state.status = s;
    if (s.needs_setup) show();
    else showSpaceName(s.space);
  }).catch(function (err) {
    // Degrading to "no wizard" is right for an older Core without these routes.
    // Being UNDIAGNOSABLE is not: a fresh Core whose probe 403s or 500s would
    // otherwise leave the operator on a dashboard for a Space that never got
    // created, with nothing anywhere saying why.
    console.warn("Wavr: setup probe failed, skipping the first-run wizard.", err);
  });

  // Naming a Space and then never seeing the name is half a feature. Show it
  // beside the wordmark and in the window title, so a household running two
  // Cores can tell one browser tab from the other.
  function showSpaceName(space) {
    if (!space || !space.name) return;
    var slot = el("brandSpace");
    // textContent, never innerHTML: the Space name is operator input and this
    // is the one place it lands in the shell's own chrome.
    if (slot) slot.textContent = space.name;
    document.title = space.name + " \u2014 Wavr";
  }
})();
