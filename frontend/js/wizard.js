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

  // The Core, through the shared client.
  //
  // This function used to be fourteen lines, and `wizard.js` carried a
  // byte-identical copy of them. Two implementations of one decision is the
  // shape that eventually disagrees: the next person to improve the error
  // message improves one of them, and the product starts saying two different
  // things about the same failure depending on which screen you were looking
  // at. `js/api.js` owns it now; the signature here is unchanged.
  function api(path, opts) { return WavrAPI.json(path, opts); }

  function fail(err) {
    var box = el("swErr");
    box.hidden = false;
    // Never render a raw stack or a bare status code at a first-time user.
    box.textContent = (err && err.message) ? err.message
      : WavrT("Something went wrong. Wavr is still running — you can try again.");
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
    el("swTitle").textContent = legacy ? WavrT("Let's finish setting up Wavr") : WavrT("Welcome to Wavr");
    el("swLede").textContent = legacy
      ? WavrT("Wavr is already running on this machine with {n} paired device. Give it a name and nothing you've set up changes."
              + "|Wavr is already running on this machine with {n} paired devices. Give it a name and nothing you've set up changes.",
              {n: state.status.existing_devices})
      : WavrT("Wavr turns this machine and your network into a space it can understand. "
        + "This takes about a minute.");

    var opts = [];
    if (legacy) {
      opts.push({ id: "adopt", t: WavrT("Name what's already here"),
                  d: WavrT("Keeps every paired device, every camera and every setting exactly as it is.") });
    }
    opts.push({ id: "create", t: legacy ? WavrT("Start fresh instead") : WavrT("Create a new Space"),
                d: WavrT("This machine becomes the Core — the part of Wavr that does the thinking.") });
    opts.push({ id: "join", t: WavrT("Join a Space that already exists"),
                d: WavrT("Another device on this network is already running Wavr.") });

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

    actions([{ label: WavrT("Continue"), primary: true, disabled: !state.path, go: next }]);
  }

  // -- Step 1: name it -------------------------------------------------------
  // A FUNCTION, not a constant.
  //
  // As a module-level table its labels were plain literals that no extractor
  // could see: they reach the screen through `WavrT(table[key])`, so they were
  // looked up at runtime and declared nowhere, which means they could never be
  // translated and nothing could report that. Built per call, the literals sit
  // inside `WavrT(...)` where they are visible — and the table is rebuilt in
  // whatever language is current, instead of frozen in the one that was active
  // when the file parsed.
  function kinds() {
    return [["home", WavrT("Home")],
            ["apartment", WavrT("Apartment")],
            ["office", WavrT("Office")],
            ["shop", WavrT("Shop")],
            ["workshop", WavrT("Workshop")],
            ["other", WavrT("Somewhere else")]];
  }

  function stepName() {
    steps(1);
    el("swTitle").textContent = WavrT("What is this place?");
    el("swLede").textContent = WavrT("Wavr uses this to pick sensible room names and defaults. "
      + "You can change it at any time.");
    el("swBody").innerHTML =
      '<div class="sw-grid">' + kinds().map(function (k) {
        return '<button class="sw-opt" data-k="' + k[0] + '" aria-pressed="' +
          (state.kind === k[0]) + '"><span><b>' + esc(WavrT(k[1])) + "</b></span></button>";
      }).join("") + "</div>" +
      '<div class="sw-card" style="margin-top:14px">' +
        '<label for="swName">' + esc(WavrT("What should Wavr call it?")) + '</label>' +
        '<input type="text" id="swName" maxlength="64" autocomplete="off" spellcheck="false">' +
        '<p class="sw-note" style="margin-top:10px">' + esc(WavrT("This name stays on this machine. " +
        "Wavr never broadcasts it to your network.")) + '</p>' +
      "</div>" +
      '<div class="sw-card" style="margin-top:10px">' +
        '<label for="swOwner">' + esc(WavrT("And who are you?")) + '</label>' +
        '<input type="text" id="swOwner" maxlength="64" autocomplete="off">' +
        '<p class="sw-note" style="margin-top:10px">' + esc(WavrT("You become the Owner — the only " +
        "person who can hand this Space to someone else.")) + '</p>' +
      "</div>" +
      /* The room this machine is in.
       *
       * Optional, and an empty map is the honest default — a floor plan Wavr
       * invented is a map of rooms that do not exist. But the release notes
       * said "setup keeps the first room you name", the backend's `seed_room`
       * was built and tested for exactly this, and the wizard never asked. One
       * of the two had to change, and asking is the better product: one room
       * turns an empty Space screen into a screen with something on it.
       */
      '<div class="sw-card" style="margin-top:10px">' +
        '<label for="swRoom">' + esc(WavrT("Which room is this machine in?")) + '</label>' +
        '<input type="text" id="swRoom" maxlength="64" autocomplete="off" ' +
          'placeholder="' + esc(WavrT("e.g. living room")) + '">' +
        '<p class="sw-note" style="margin-top:10px">' + esc(WavrT("Optional. It becomes the "
        + "first room on your floor plan — you can draw the rest later, or skip this and "
        + "start with an empty map.")) + '</p>' +
      "</div>";

    var nameIn = el("swName"), ownerIn = el("swOwner"), roomIn = el("swRoom");
    nameIn.value = state.name || (state.kind === "home" ? "My Home" : "");
    ownerIn.value = state.owner || "";
    roomIn.value = state.room || "";
    function sync() {
      state.name = nameIn.value;
      state.owner = ownerIn.value;
      state.room = roomIn.value;
      el("swActions").querySelector(".primary").disabled = !nameIn.value.trim();
    }
    nameIn.addEventListener("input", sync);
    ownerIn.addEventListener("input", sync);
    roomIn.addEventListener("input", sync);

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
      { label: WavrT("Back"), go: back },
      { label: WavrT("Continue"), primary: true, disabled: !nameIn.value.trim(), go: next }
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
    el("swTitle").textContent = WavrT("Looking at this device");
    el("swLede").textContent = WavrT("Wavr is checking what this machine can do. "
      + "Nothing is switched on and nothing leaves this device.");
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      esc(WavrT("Checking…")) + "</div>";
    actions([{ label: WavrT("Back"), go: back }]);

    api("/api/setup/scan", { method: "POST" }).then(function (res) {
      state.scan = res;
      var m = res.manifest, rec = res.recommendation;
      state.functions = rec.functions.slice();

      var caps = [["camera", "Camera"], ["ble", "Bluetooth"], ["wifi", "Wi-Fi"],
                  ["ethernet", "Ethernet"], ["gpu", "Graphics"], ["battery", "Battery"]];
      var factsHtml = fact(WavrT("System"), m.os_version || WavrT("Unknown"), !m.os_version) +
        fact(WavrT("Memory"), m.ram_mb ? (Math.round(m.ram_mb / 1024 * 10) / 10) + " GB" : WavrT("Unknown"),
             !m.ram_mb) +
        fact(WavrT("Processors"), m.cpu_count != null ? String(m.cpu_count) : WavrT("Unknown"),
             m.cpu_count == null) +
        caps.map(function (c) {
          var v = m.capabilities[c[0]];
          // Tristate, honestly: null is "we couldn't tell", never "no".
          return fact(WavrT(c[1]), v === true ? WavrT("Yes") : v === false ? WavrT("No") : WavrT("Couldn't tell"),
                      v == null);
        }).join("");

      el("swBody").innerHTML =
        '<div class="sw-rec"><b>' + esc(WavrT("Recommended: {label}", {label: rec.label})) + "</b><ul>" +
          rec.reasons.map(function (r) { return "<li>" + esc(WavrT(r)) + "</li>"; }).join("") +
        "</ul></div>" +
        '<dl class="sw-facts">' + factsHtml + "</dl>" +
        '<p class="sw-note" style="margin-top:12px">' +
        WavrT("Anything Wavr couldn’t work out is marked {marked} rather than guessed at. "
              + "You can change what this device does later.",
              {marked: '<span class="sw-unknown">' + esc(WavrT("Couldn’t tell")) + '</span>'}) +
        '</p>';

      actions([{ label: WavrT("Back"), go: back },
               { label: WavrT("Use this setup"), primary: true, go: next }]);
    }).catch(function (e) {
      fail(e);
      el("swBody").innerHTML = '<div class="sw-card">' + esc(WavrT("Wavr couldn’t check this device. " +
        "You can still continue — it will start as a Core and you can adjust it later.")) + "</div>";
      state.functions = ["core", "client"];
      actions([{ label: WavrT("Back"), go: back },
               { label: WavrT("Continue anyway"), primary: true, go: next }]);
    });
  }

  // -- Step 3: commit --------------------------------------------------------
  function stepFinish() {
    steps(3);
    el("swTitle").textContent = WavrT("Setting up {name}", {name: state.name || WavrT("your Space")});
    el("swLede").textContent = "";
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      esc(WavrT("Creating…")) + "</div>";
    el("swActions").innerHTML = "";

    // `room` travels with it. The backend's `seed_room` puts it on the empty
    // floor plan; without this line the wizard was asking nobody, and the
    // release notes' "setup keeps the first room you name" was a promise about
    // a question nothing ever put.
    var body = { name: state.name.trim(), kind: state.kind,
                 owner_name: (state.owner || "").trim() || "Owner",
                 room: (state.room || "").trim() };
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
      el("swTitle").textContent = WavrT("{name} is ready", {name: res.space.name});
      el("swLede").textContent = "";
      var core = res.core || {};
      // `multi` decides only the closing note's wording (see renderReady below);
      // everything else on this screen is unaffected by it either way.
      function renderReady(multi) {
        el("swBody").innerHTML =
          '<div class="sw-rec"><b>' +
            esc(WavrT(core.status === "primary"
                      ? "This device is now your Core"
                      : "This device is now your standby Core")) + "</b>" +
            '<ul><li>' + esc(WavrT("Wavr will start watching for what is around it.")) + '</li>' +
            "<li>" + esc(WavrT("You can add phones, tablets and sensors from the Devices tab.")) + "</li></ul></div>" +
          (res.adopted_devices
            ? '<p class="sw-note" style="margin-top:12px">' +
              esc(WavrT("{n} existing device was kept exactly as they were."
                        + "|{n} existing devices were kept exactly as they were.",
                        {n: res.adopted_devices})) + "</p>"
            : "") +
          '<p class="sw-note' + (multi ? "" : " sw-warn") + '" style="margin-top:12px">' +
          (multi
            ? esc(WavrT("Other devices on your network can already reach Wavr — pair one from the Devices tab in Settings."))
            : WavrT("Right now only this machine can reach Wavr. To use it from your phone, "
                    + "turn on {setting} in Settings — Wavr will explain what that changes before you do.",
                    {setting: "<b>" + esc(WavrT("Let other devices connect")) + "</b>"})) +
          "</p>";
        actions([{ label: WavrT("Open Wavr"), primary: true, go: done }]);
      }
      // The closing note used to say "only this machine can reach Wavr" and tell
      // the operator to turn on "Let other devices connect" UNCONDITIONALLY --
      // including on an install where that setting was already on (adopting an
      // existing multi-device install, or a Core re-run through setup after the
      // switch was flipped). `/api/status` is the one place `cfg.multidevice`
      // is already exposed to the browser (no new route); read it once, here,
      // rather than assume the off-by-default case is the only case. A failed
      // probe (older Core) falls back to the honest single-machine default
      // rather than blocking the "Space is ready" screen on it.
      api("/api/status").then(function (st) {
        renderReady(!!(st && st.features && st.features.multidevice));
      }).catch(function () { renderReady(false); });
    }).catch(function (e) {
      fail(e);
      // Deliberately does NOT claim "nothing was changed" -- the client cannot
      // know that. The Core rolls back a half-created Space itself; this says
      // what happened and offers the retry.
      el("swBody").innerHTML = '<div class="sw-card">' + esc(WavrT("Setup didn\u2019t finish. " +
        "Wavr is still running and you can try again.")) + "</div>";
      actions([{ label: WavrT("Back"), go: function () { state.step = 2; render(); } }]);
    });
  }

  // -- Join path -------------------------------------------------------------
  function stepJoin() {
    steps(1);
    el("swTitle").textContent = WavrT("Wavr Spaces on this network");
    el("swLede").textContent = WavrT("Looking for other devices already running Wavr.");
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      esc(WavrT("Searching…")) + "</div>";
    actions([{ label: WavrT("Back"), go: back }]);

    api("/api/setup/nearby").then(function (res) {
      if (!res.available) {
        el("swBody").innerHTML = '<div class="sw-card">' +
          esc(WavrT("Wavr can’t search this network from here. That usually means the "
                    + "optional discovery component isn’t installed.")) +
          '<p class="sw-note" style="margin-top:10px">' +
          esc(WavrT("You can still set this machine up as its own Space and connect the two later.")) +
          "</p></div>";
      } else if (!res.cores.length) {
        el("swBody").innerHTML = '<div class="sw-card">' + esc(WavrT("No other Wavr Cores answered.")) +
          '<p class="sw-note" style="margin-top:10px">' +
          esc(WavrT("Make sure the other device is switched on, running Wavr, and on this same Wi-Fi.")) +
          "</p></div>";
      } else {
        el("swBody").innerHTML = '<div class="sw-choice">' + res.cores.map(function (c, i) {
          return '<button class="sw-opt" data-core="' + i + '"><span><b>' + esc(c.name) +
            "</b><span>" + esc(c.host) + ":" + esc(c.port) + "</span></span></button>";
        }).join("") + "</div>" +
        '<p class="sw-note" style="margin-top:12px">' +
        esc(WavrT("Pick the device that is already running your Space.")) + "</p>";
        el("swBody").querySelectorAll("[data-core]").forEach(function (b) {
          b.addEventListener("click", function () {
            stepJoinConfirm(res.cores[Number(b.dataset.core)]);
          });
        });
      }
      actions([{ label: WavrT("Back"), go: back },
               { label: WavrT("Search again"), go: stepJoin }]);
    }).catch(function (e) {
      fail(e);
      // Replace the spinner. Without this the operator gets an error message
      // above something that spins forever -- the two success branches above
      // both replace it, and this one used not to.
      el("swBody").innerHTML = '<div class="sw-card">' +
        esc(WavrT("Wavr couldn\u2019t search the network just now.")) + "</div>";
      actions([{ label: WavrT("Back"), go: back }, { label: WavrT("Try again"), go: stepJoin }]);
    });
  }

  // Joining needs one thing this Core cannot discover: what the household is
  // called. The name is deliberately absent from the mDNS advertisement (see
  // /api/setup/nearby), so it comes from the person, who already chose it on the
  // other device. Nothing else is asked -- no address, no port, no id.
  function stepJoinConfirm(core) {
    steps(1);
    el("swTitle").textContent = WavrT("Join this Space");
    el("swLede").textContent = WavrT("This machine becomes a second Core for it.");
    el("swBody").innerHTML =
      '<div class="sw-card"><b>' + esc(core.name) + "</b>" +
        '<span class="sw-note" style="display:block;margin-top:4px">' +
        esc(core.host) + ":" + esc(core.port) + "</span></div>" +
      '<div class="sw-card" style="margin-top:12px">' +
        '<label for="swJoinName">' + esc(WavrT("What did you call this place on that device?")) + '</label>' +
        '<input type="text" id="swJoinName" maxlength="64" autocomplete="off" ' +
        'spellcheck="false" placeholder="My Home">' +
        '<p class="sw-note" style="margin-top:10px">' +
        esc(WavrT("Wavr can’t read the name off the network — it is never broadcast — so it needs yours.")) +
        "</p>" +
      "</div>" +
      '<div class="sw-card" style="margin-top:10px">' +
        '<label for="swJoinOwner">' + esc(WavrT("And who are you?")) + '</label>' +
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
      { label: WavrT("Back"), go: stepJoin },
      { label: WavrT("Join"), primary: true, disabled: !nameIn.value.trim(),
        go: function () { doJoin(core); } }
    ]);
  }

  function doJoin(core) {
    el("swBody").innerHTML = '<div class="sw-card"><span class="sw-spin"></span>' +
      esc(WavrT("Joining…")) + "</div>";
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
      el("swTitle").textContent = WavrT("Joined — one step left");
      el("swLede").textContent = "";
      el("swBody").innerHTML =
        '<div class="sw-card">' +
        WavrT("This machine is now a {role} for {name}.",
              {role: "<b>" + esc(WavrT("standby Core")) + "</b>",
               name: esc(res.space && res.space.name ? res.space.name : WavrT("your Space"))}) +
        '<p class="sw-note" style="margin-top:10px">' +
        esc(WavrT("It does not mirror the other Core yet. Connect the two and they will agree "
                  + "on who is in charge, and notice if they ever disagree.")) +
        "</p></div>" +
        '<div class="sw-card" style="margin-top:10px">' +
        WavrT("Connect them from {path} on either machine. It is a one-time code compare, "
              + "so neither trusts the other on the network’s word.",
              {path: "<b>" + esc(WavrT("Settings → Devices → Cores")) + "</b>"}) +
        "</div>";
      actions([{ label: WavrT("Open Wavr"), primary: true, go: done }]);
    }).catch(function (e) {
      fail(e);
      el("swBody").innerHTML = '<div class="sw-card">' +
        esc(WavrT("Wavr couldn’t join that Space. It is still running and nothing "
                  + "on the other device was touched.")) + "</div>";
      actions([{ label: WavrT("Back"), go: function () { stepJoinConfirm(core); } }]);
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
    // The writer lives in runtime.js, not here: this IIFE returns early for
    // every mode but "live", so a copy defined below it would be unreachable
    // on exactly the surfaces that need the Space name most -- a paired
    // phone and the Core kiosk face.
    else if (window.__wavrShowSpace) window.__wavrShowSpace(s.space);
  }).catch(function (err) {
    // Degrading to "no wizard" is right for an older Core without these routes.
    // Being UNDIAGNOSABLE is not: a fresh Core whose probe 403s or 500s would
    // otherwise leave the operator on a dashboard for a Space that never got
    // created, with nothing anywhere saying why.
    console.warn("Wavr: setup probe failed, skipping the first-run wizard.", err);
  });

})();
