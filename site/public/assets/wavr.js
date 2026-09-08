/* ============================================================================
   Wavr public site — shared script. ONE file, no build step, no CDN, no
   analytics, nothing sent anywhere. Every page on this site loads this same
   file; each block below no-ops unless the DOM it needs is present, so it's
   safe to load everywhere.

   What this file reads, in full (documented on privacy.html too):
     - navigator.userAgentData (Client Hints), if the browser exposes it, via
       getHighEntropyValues(["platform","architecture","bitness","mobile"]).
     - navigator.userAgent as a fallback string parse when Client Hints aren't
       available (Firefox, Safari, older browsers).
     - a same-origin fetch() of assets/releases.json (a file this site ships).
     - window.location.href, only on download.html, to draw the "take this
       page to your phone" QR code. Never sent anywhere — it's encoded into
       an SVG in your own browser via assets/qrcode.vendor.js (vendored,
       MIT-licensed, no network calls of its own).
   No canvas fingerprinting, no font enumeration, no cookies, no storage
   writes, no request ever leaves this origin.
   ============================================================================ */

(function () {
  "use strict";

  // Placeholder install-script host — the ONE place this domain is written.
  // Swap this string once a real domain exists; every bootstrap command on
  // the page is built from it.
  var BOOTSTRAP_DOMAIN = "wavr.dev";

  document.querySelectorAll(".js-bootstrap-cmd").forEach(function (el) {
    var tpl = el.getAttribute("data-template") || "";
    el.textContent = tpl.replace(/\{domain\}/g, BOOTSTRAP_DOMAIN);
  });

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- Product screenshots: degrade to an honest placeholder ---------------
  // <img class="product-shot" data-shot-label="…"> inside a .shot-frame.
  // If the file doesn't exist yet (another workstream generates these),
  // the frame keeps its reserved size and shows a calm "not captured yet"
  // placeholder — drawn in the product's own dashed "no coverage" style —
  // instead of a broken-image icon.
  document.querySelectorAll(".shot-frame img.product-shot").forEach(function (img) {
    img.addEventListener(
      "error",
      function () {
        var frame = img.closest(".shot-frame");
        if (frame) frame.classList.add("shot-frame--missing");
      },
      { once: true }
    );
  });

  // ---- Phase A: best-effort platform guess from the browser -----------------

  function mapPlatform(raw) {
    var p = (raw || "").toLowerCase();
    if (p.indexOf("win") !== -1) return "windows";
    if (p.indexOf("mac") !== -1) return "macos";
    if (p.indexOf("android") !== -1) return "android";
    if (p.indexOf("ios") !== -1) return "ios";
    if (p.indexOf("linux") !== -1 || p.indexOf("cros") !== -1) return "linux";
    return "unknown";
  }

  function osFromUA(ua) {
    if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
    if (/Android/i.test(ua)) return "android";
    if (/Windows/i.test(ua)) return "windows";
    if (/Macintosh|Mac OS X/i.test(ua)) return "macos";
    if (/Linux|X11/i.test(ua)) return "linux";
    return "unknown";
  }

  function archFromHints(architecture, bitness) {
    var a = (architecture || "").toLowerCase();
    if (a === "arm") return bitness === "32" ? "arm" : "arm64";
    if (a === "x86") return bitness === "64" ? "x64" : "x86";
    return "unknown";
  }

  function archFromUA(ua) {
    if (/arm64|aarch64/i.test(ua)) return "arm64";
    if (/armv?7|arm\b/i.test(ua)) return "arm";
    if (/win64|x86_64|amd64|x64/i.test(ua)) return "x64";
    if (/i686|i386|x86(?!_64)/i.test(ua)) return "x86";
    return "unknown";
  }

  async function detectPlatform() {
    var result = { os: "unknown", mobile: false, arch: "unknown", method: "user-agent (fallback)" };
    var uad = navigator.userAgentData;
    var ua = navigator.userAgent || "";

    if (uad) {
      result.os = mapPlatform(uad.platform);
      result.mobile = !!uad.mobile;
      result.method = "navigator.userAgentData";
      if (typeof uad.getHighEntropyValues === "function") {
        try {
          var hv = await uad.getHighEntropyValues(["architecture", "bitness", "platform", "mobile"]);
          result.os = mapPlatform(hv.platform || uad.platform);
          result.mobile = typeof hv.mobile === "boolean" ? hv.mobile : result.mobile;
          result.arch = archFromHints(hv.architecture, hv.bitness);
          result.method = "navigator.userAgentData.getHighEntropyValues";
        } catch (e) {
          // Some browsers reject high-entropy requests outside a user gesture —
          // fall back silently to the low-entropy uad fields already captured above.
        }
      }
    }

    if (result.os === "unknown") result.os = osFromUA(ua);
    if (!result.mobile) result.mobile = /Mobi|Android|iPhone|iPad|iPod/i.test(ua);
    if (result.arch === "unknown") result.arch = archFromUA(ua);

    return result;
  }

  var OS_LABELS = { windows: "Windows", macos: "macOS", linux: "Linux", android: "Android", ios: "iOS" };

  // ---- releases.json: shared lookup + rendering -----------------------------
  // Schema (schemaVersion 2), generated by scripts/gen_releases_manifest.py —
  // see site/README.md for the full field reference. Shape actually shipped:
  //   { schemaVersion, generatedAt, generatedBy, sourceDir, note,
  //     products: [ { id, product, platform, maturity, purpose,
  //                   downloadName, conflictsWith, available, sizeBytes,
  //                   sha256, modifiedAt, downloadPath,
  //                   outdated, outdatedReason, missingReason } ] }
  // Every download button on this site is built from a product's own
  // downloadPath — never a filename or version typed into the HTML by hand.

  function findProduct(releases, id) {
    var products = (releases && releases.products) || [];
    for (var i = 0; i < products.length; i += 1) {
      if (products[i].id === id) return products[i];
    }
    return null;
  }

  function humanSize(bytes) {
    if (!bytes) return null;
    var mb = bytes / (1024 * 1024);
    return mb >= 1000 ? (mb / 1024).toFixed(2) + " GB" : mb.toFixed(1) + " MB";
  }

  function maturityTagHTML(maturity) {
    if (maturity === "recommended") return '<span class="tag tag-ready">Recommended</span>';
    if (maturity === "specialized") return '<span class="tag tag-note">Specialized</span>';
    return '<span class="tag tag-note">Experimental</span>';
  }

  function conflictNoteHTML(releases, conflictsWith) {
    var ids = conflictsWith || [];
    if (!ids.length) return "";
    var labels = ids.map(function (id) {
      var p = findProduct(releases, id);
      return escapeHTML(p ? p.product : id);
    });
    return (
      '<p class="fine-print" style="margin-top:8px">Do not install this on the same ' +
      "device as: " + labels.join(", ") + ".</p>"
    );
  }

  // `slim`: the surrounding card already has its own heading, maturity badge
  // and description (download.html's Windows/Android cards) — repeating the
  // manifest's own product/purpose/tag there reads as a card nested inside a
  // card. Slim mode drops the identity chrome and the bordered wrapper,
  // leaving just the button, its meta, and the honesty notes. Non-slim (the
  // default) is for slots embedded in plainer contexts — install.html's
  // per-OS cards, and the advanced/experimental items — where the manifest's
  // own wording is the only place that context appears.
  function renderProductRow(releases, entry, slim) {
    if (!entry || !entry.available || entry.missingReason) return "";
    var meta = [];
    var size = humanSize(entry.sizeBytes);
    if (size) meta.push(size);
    if (entry.modifiedAt) meta.push("built " + escapeHTML(String(entry.modifiedAt).slice(0, 10)));
    var details = "";
    if (entry.sha256) {
      details =
        '<details class="asset-verify"><summary>Verify this download</summary>' +
        '<p class="fine-print asset-meta">Filename: <code>' + escapeHTML(entry.downloadName || "") + "</code><br>" +
        "SHA-256: <code>" + escapeHTML(entry.sha256) + "</code></p></details>";
    }
    var outdatedNote = entry.outdated
      ? '<p class="fine-print" style="margin-top:8px">' +
        escapeHTML(entry.outdatedReason || "This build may be out of date.") + "</p>"
      : "";
    var identity = slim
      ? ""
      : maturityTagHTML(entry.maturity) + ' <p><strong>' + escapeHTML(entry.product) + "</strong></p>" +
        (entry.purpose ? '<p class="fine-print">' + escapeHTML(entry.purpose) + "</p>" : "");
    var body =
      identity +
      '<a class="btn btn-primary" href="' + encodeURI(entry.downloadPath) + '" download>Download</a>' +
      (meta.length ? '<p class="fine-print asset-meta" style="margin-top:8px">' + meta.join(" &middot; ") + "</p>" : "") +
      details + outdatedNote + conflictNoteHTML(releases, entry.conflictsWith);
    return slim
      ? '<div style="margin-top:14px">' + body + "</div>"
      : '<div class="tile tile-accent" style="margin-top:12px">' + body + "</div>";
  }

  // A smaller inline variant for a secondary/fallback product (e.g. the MSI
  // next to the main EXE) — a link, not a second full-size accent tile.
  function renderProductRowCompact(entry) {
    if (!entry || !entry.available || entry.missingReason) return "";
    var size = humanSize(entry.sizeBytes);
    return (
      '<p class="fine-print" style="margin-top:10px">' +
      (entry.purpose ? escapeHTML(entry.purpose) + " " : "") +
      '<a href="' + encodeURI(entry.downloadPath) + '" download>' + escapeHTML(entry.product) + "</a>" +
      (size ? " (" + size + ")" : "") + "</p>"
    );
  }

  function fillReleaseSlots(releases) {
    document.querySelectorAll(".release-slot[data-product]").forEach(function (slot) {
      var entry = findProduct(releases, slot.getAttribute("data-product"));
      var slim = slot.getAttribute("data-slim") === "true";
      var html = renderProductRow(releases, entry, slim);
      if (html) {
        slot.innerHTML = html;
        slot.hidden = false;
        markPublished(slot);
      } else {
        // The artefact is genuinely not here. Say so -- this is the one case
        // the note exists for, and it now has to be switched ON rather than
        // switched off, because the page ships in the state where everything
        // IS present.
        markUnavailable(slot);
      }
    });

    document.querySelectorAll(".release-slot[data-product-compact]").forEach(function (slot) {
      var entry = findProduct(releases, slot.getAttribute("data-product-compact"));
      var html = renderProductRowCompact(entry);
      if (html) {
        slot.innerHTML = html;
        slot.hidden = false;
      }
    });

    return releases;
  }

  // Once a real download exists inside a card, its static "nothing published
  // yet" paragraph would otherwise keep saying so right next to a working
  // Download button. Hide that paragraph rather than ship a contradiction.
  // The mirror of `markPublished`, and it exists because the default was
  // inverted: these paragraphs now start hidden, so the honest static page --
  // the one somebody sees with scripting off -- matches the folder, where all
  // five artefacts are present. When one really is missing, this puts its note
  // back on screen.
  function markUnavailable(slot) {
    var node = slot.parentElement;
    while (node && node !== document.body) {
      var notes = node.querySelectorAll(".no-release-note");
      if (notes.length) {
        notes.forEach(function (n) { n.hidden = false; });
        return;
      }
      node = node.parentElement;
    }
  }

  function markPublished(slot) {
    // Walk up to the nearest ancestor that OWNS such a paragraph, and stop.
    //
    // This was `slot.closest(".platform-card, .dl-card")`, which quietly missed
    // the two items under "Advanced downloads": they are a bare <div> and a
    // <details>, and carry neither class. Both therefore rendered a working
    // Download button directly above the words "Not published here" — and one
    // of those downloads is the build that registers itself as the phone's home
    // screen. Found by opening the page in a browser and asking what was
    // visible, which is the only way this kind of thing is ever found.
    //
    // Stopping at the FIRST ancestor that owns a note is the point: the shared
    // `.disclosure-group` wraps both advanced items, so clearing from there
    // would also silence the other one's note.
    var node = slot.parentElement;
    while (node && node !== document.body) {
      var notes = node.querySelectorAll(".no-release-note");
      if (notes.length) {
        notes.forEach(function (n) { n.hidden = true; });
        return;
      }
      node = node.parentElement;
    }
  }

  // ---- download.html: the honest top-of-page release status -----------------

  function renderReleaseStatus(releases) {
    var el = document.getElementById("dl-release-status");
    if (el) {
      var hasProducts = releases && Array.isArray(releases.products) && releases.products.some(function (p) { return p.available; });
      if (hasProducts && releases.note) {
        el.textContent = releases.note;
      } else if (hasProducts) {
        el.textContent = "Pre-release build" + (releases.generatedAt ? ", " + String(releases.generatedAt).slice(0, 10) : "") + ".";
      } else {
        el.textContent = "No release has been published yet, see below for what to do today.";
      }
      // The initial HTML shows a short "checking…" pill; every resolved
      // state above is a full sentence, which reads as shouting in that
      // pill's all-caps styling — switch to a calm inline note instead.
      el.classList.remove("release-badge");
      el.classList.add("release-note");
    }
    return releases;
  }

  // ---- install.html: "recommended for you" ----------------------------------

  function buildRecommended(detected) {
    var recommendedEl = document.getElementById("install-recommended");
    if (!recommendedEl) return detected;

    var detectLineEl = document.getElementById("detect-line");
    var otherPlatformsEl = document.getElementById("other-platforms");
    var article = document.getElementById("platform-" + detected.os);

    if (!article) {
      if (detectLineEl) {
        detectLineEl.textContent =
          "We couldn't confidently tell what you're browsing from (" + escapeHTML(detected.method) + "). " +
          "Nothing was guessed, so pick your platform in the full list below.";
      }
      return detected;
    }

    var archNote = detected.arch !== "unknown" ? " (" + detected.arch + ")" : "";
    if (detectLineEl) {
      detectLineEl.textContent =
        "Detected from your browser: " + OS_LABELS[detected.os] + archNote +
        ". This is a guess, Phase A: see below for the honest version of that sentence.";
    }

    var releaseHTML = "";
    var releaseSlot = article.querySelector(".release-slot");
    if (releaseSlot && !releaseSlot.hidden) releaseHTML = releaseSlot.innerHTML;

    recommendedEl.innerHTML =
      '<div class="tile-eyebrow">Recommended for you</div>' +
      '<div class="tile tile-accent">' +
      "<h3>" + OS_LABELS[detected.os] + "</h3>" +
      releaseHTML +
      article.querySelector(".platform-body").innerHTML +
      "</div>" +
      '<p class="fine-print">Not you? Every platform is listed in <a href="#other-platforms">Other platforms</a> below: nothing is hidden.</p>';
    recommendedEl.hidden = false;

    // Progressive enhancement only: collapse the full list now that a
    // recommendation is shown. No-JS visitors never reach this line, so the
    // list stays open (usable) for them by default.
    if (otherPlatformsEl) otherPlatformsEl.removeAttribute("open");

    return detected;
  }

  // ---- download.html: ONE primary recommendation, "all downloads" as the
  // always-available escape hatch. A first-time user on Windows must see the
  // Windows download as THE option, not pick between two equally-weighted
  // cards or understand any acronym — so this mirrors install.html's
  // recommended/other-platforms split rather than just highlighting a card.

  var DL_CARD_BY_OS = { windows: "dl-card-windows", android: "dl-card-android" };

  function buildDownloadRecommended(detected) {
    var recommendedEl = document.getElementById("dl-recommended");
    if (!recommendedEl) return detected; // not download.html

    var detectLineEl = document.getElementById("dl-detect-line");
    var allPlatformsEl = document.getElementById("dl-all-platforms");

    // iOS has no packaged app path today, same companion-browser story as
    // Android; point at that card rather than inventing an iOS-only one.
    var matchOs = detected.os === "ios" ? "android" : detected.os;
    var cardId = DL_CARD_BY_OS[matchOs];
    var card = cardId && document.getElementById(cardId);

    if (!card) {
      if (detectLineEl) {
        detectLineEl.textContent =
          "We couldn't confidently tell what you're on. Both options below are always shown, pick the one that fits.";
      }
      return detected;
    }

    if (detectLineEl) {
      detectLineEl.textContent =
        "Detected from your browser: " + (OS_LABELS[detected.os] || detected.os) +
        '. This is a guess: see "All downloads" below for the honest version of that sentence.';
    }

    recommendedEl.innerHTML =
      '<div class="tile-eyebrow">Recommended for you</div>' +
      '<div class="tile dl-card tile-accent">' + card.innerHTML + "</div>" +
      '<p class="fine-print">Not on ' + escapeHTML(OS_LABELS[matchOs] || matchOs) +
      '? Every option is in <a href="#dl-all-platforms">All downloads</a> below: nothing is hidden.</p>';
    recommendedEl.hidden = false;

    // Progressive enhancement only: collapse the full list now that a
    // recommendation is shown. No-JS visitors never reach this line, so the
    // list stays open (usable) for them by default.
    if (allPlatformsEl) allPlatformsEl.removeAttribute("open");

    return detected;
  }

  // ---- download.html: "take this page to your phone" QR ---------------------

  function renderPhoneQR() {
    var block = document.getElementById("qr-block");
    if (!block) return; // not download.html

    var cardEl = block.querySelector(".qr-card");
    var urlEl = block.querySelector(".qr-url");
    var noteEl = block.querySelector(".qr-note");
    var loc = window.location;

    var reachableFromAPhone =
      loc.protocol !== "file:" &&
      !/^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)$/i.test(loc.hostname);

    if (urlEl) urlEl.textContent = loc.href;

    if (!reachableFromAPhone) {
      if (cardEl) cardEl.hidden = true;
      if (noteEl) noteEl.hidden = false;
      return;
    }

    if (typeof window.qrcode !== "function") {
      // Vendored library failed to load for some reason — fail to the note
      // rather than an empty white square.
      if (cardEl) cardEl.hidden = true;
      if (noteEl) {
        noteEl.hidden = false;
        noteEl.textContent = "The QR code didn't render. Use the address below instead.";
      }
      return;
    }

    try {
      var qr = window.qrcode(0, "M"); // 0 = auto version, 'M' = 15% error correction
      qr.addData(loc.href);
      qr.make();
      if (cardEl) {
        cardEl.innerHTML = qr.createSvgTag({
          cellSize: 4,
          margin: 8,
          scalable: true,
          alt: "QR code that opens this page on your phone",
          title: "Scan to open this page on your phone",
        });
        cardEl.hidden = false;
      }
      if (noteEl) noteEl.hidden = true;
    } catch (e) {
      if (cardEl) cardEl.hidden = true;
      if (noteEl) {
        noteEl.hidden = false;
        noteEl.textContent = "The QR code didn't render. Use the address below instead.";
      }
    }
  }

  // ---- Run ---------------------------------------------------------------

  fetch("assets/releases.json")
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(fillReleaseSlots)
    .then(renderReleaseStatus)
    .then(detectPlatform)
    .then(buildRecommended)
    .then(buildDownloadRecommended)
    .catch(function () {
      var detectLineEl = document.getElementById("detect-line") || document.getElementById("dl-detect-line");
      if (detectLineEl) detectLineEl.textContent = "Platform detection didn't run. Pick your platform below.";
    });

  renderPhoneQR();
})();
