/* ============================================================================
   Wavr public site — shared script. ONE file, no build step, no CDN, no
   analytics, nothing sent anywhere. Every other page on this site loads this
   same file; it only does anything on install.html (it looks for
   #install-recommended and no-ops everywhere else).

   What this file reads, in full (documented on privacy.html too):
     - navigator.userAgentData (Client Hints), if the browser exposes it, via
       getHighEntropyValues(["platform","architecture","bitness","mobile"]).
     - navigator.userAgent as a fallback string parse when Client Hints aren't
       available (Firefox, Safari, older browsers).
     - a same-origin fetch() of assets/releases.json (a file this site ships).
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

  var recommendedEl = document.getElementById("install-recommended");
  if (!recommendedEl) return; // not install.html — nothing else to do here

  var detectLineEl = document.getElementById("detect-line");
  var otherPlatformsEl = document.getElementById("other-platforms");

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

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

  // ---- releases.json lookup ---------------------------------------------

  var DESKTOP_ARCHES = {
    windows: ["x64", "arm64"],
    macos: ["arm64", "x64"],
    linux: ["x64", "arm64", "arm"],
  };

  function humanSize(bytes) {
    if (!bytes) return null;
    var mb = bytes / (1024 * 1024);
    return mb >= 1000 ? (mb / 1024).toFixed(2) + " GB" : mb.toFixed(1) + " MB";
  }

  function renderAssetRow(key, entry) {
    var size = humanSize(entry.size);
    var meta = [];
    if (size) meta.push(size);
    if (entry.sha256) meta.push("SHA-256: " + escapeHTML(entry.sha256));
    if (entry.publishedAt) meta.push("published " + escapeHTML(entry.publishedAt));
    return (
      '<div class="tile tile-accent" style="margin-top:12px">' +
      '<span class="tag tag-ready">Published</span> ' +
      "<p><strong>" + escapeHTML(entry.label || entry.filename || key) + "</strong></p>" +
      '<a class="btn btn-primary" href="' + encodeURI(entry.url) + '">Download</a>' +
      (meta.length ? '<p class="fine-print asset-meta">' + meta.join(" · ") + "</p>" : "") +
      "</div>"
    );
  }

  function fillReleaseSlots(releases) {
    var assets = (releases && releases.assets) || {};
    document.querySelectorAll(".release-slot").forEach(function (slot) {
      var os = slot.getAttribute("data-os");
      var arches = DESKTOP_ARCHES[os] || [];
      var html = "";
      arches.forEach(function (arch) {
        var entry = assets[os + "-" + arch];
        if (entry && entry.url) html += renderAssetRow(os + "-" + arch, entry);
      });
      if (html) {
        slot.innerHTML = html;
        slot.hidden = false;
      }
    });
  }

  // ---- Recommended-platform rendering ------------------------------------

  var OS_LABELS = { windows: "Windows", macos: "macOS", linux: "Linux", android: "Android", ios: "iOS" };

  function buildRecommended(detected) {
    var article = document.getElementById("platform-" + detected.os);
    if (!article) {
      detectLineEl.textContent =
        "We couldn't confidently tell what you're browsing from (" + escapeHTML(detected.method) + "). " +
        "Nothing was guessed, so pick your platform in the full list below.";
      return;
    }

    var archNote = detected.arch !== "unknown" ? " (" + detected.arch + ")" : "";
    detectLineEl.textContent =
      "Detected from your browser: " + OS_LABELS[detected.os] + archNote +
      ". This is a guess, Phase A: see below for the honest version of that sentence.";

    // Include the release-slot too (it may already hold a real download button, filled
    // by fillReleaseSlots() above, in which case it must not go missing from the
    // prominent recommended card just because it lives in a sibling <div>).
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
  }

  // ---- Run ---------------------------------------------------------------

  fetch("assets/releases.json")
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(function (releases) {
      fillReleaseSlots(releases);
      return detectPlatform();
    })
    .then(buildRecommended)
    .catch(function () {
      detectLineEl.textContent = "Platform detection didn't run. Pick your platform in the full list below.";
    });
})();
