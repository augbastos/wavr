# Wavr site

The public "install Wavr" website: what Wavr is, how to install it on every platform, and
plain-language privacy. Static HTML/CSS/vanilla JS, no build step, no framework, no npm,
zero external requests (no CDN, no web fonts, no analytics). Same visual language as the
product dashboard (`frontend/index.html`).

**Status: built, never deployed.** No Cloudflare Pages project named `wavr-site` exists yet.
Nothing here has ever been pushed live. Publishing needs Augusto's explicit go-ahead — see
`CLAUDE.md`'s Canon PEAC rules on public/irreversible actions.

## Layout

```
site/
  wrangler.toml       # Cloudflare Pages config (pattern copied from infra/wavr-diag-worker)
  README.md           # this file
  public/             # the static output — this is what gets deployed
    index.html
    install.html       # the centrepiece: universal install flow
    compatibility.html
    nodes.html
    privacy.html
    docs.html
    404.html
    assets/
      wavr.css         # one shared stylesheet
      wavr.js          # one shared script (platform detection + release lookup)
      releases.json    # download-link data (see shape below)
      icon.svg          # self-hosted brand mark (copy of frontend/icon.svg)
```

## Preview locally

```bash
cd site/public
python -m http.server 8080
# open http://127.0.0.1:8080
```

No build step. Editing any file and reloading is the whole workflow.

## Deploy

**Do not run this without checking with Augusto first** — a Cloudflare Pages deploy is a
public, semi-irreversible action (Canon PEAC: public/irreversible needs explicit OK from
that conversation).

```bash
cd site
npx wrangler pages deploy public --project-name wavr-site
```

This creates the `wavr-site` Pages project on first run (project-scoped URL,
`https://wavr-site.pages.dev`, no account subdomain — same pattern as the existing
`wavr-diag` project). No custom domain is configured.

## `assets/releases.json` shape

Fetched same-origin by `install.html` at load, and by nothing else. Hand-editable; also safe
to regenerate from a release CI job later without changing the page code, as long as the
shape below is preserved.

```jsonc
{
  "schemaVersion": 1,
  "version": "0.4.0",          // or null if nothing has shipped yet
  "publishedAt": "2026-09-03", // ISO date, or null
  "note": "free-text, shown nowhere on the page today — for maintainers",
  "assets": {
    // key = "<os>-<arch>", os in {windows, macos, linux}, arch in {x64, arm64, arm}
    "windows-x64": {
      "label": "Windows x64 installer",   // shown as the download's title
      "filename": "wavr-setup-x64.exe",
      "url": "https://github.com/augbastos/wavr/releases/download/v0.4.0/wavr-setup-x64.exe",
      "sha256": "…",                      // shown next to the download for the visitor to verify
      "size": 48213000,                   // bytes; the page renders this as MB/GB
      "publishedAt": "2026-09-03"
    }
    // any key with no entry (or missing "url") falls back to the honest
    // "not published yet — here's the source route" state. There is no dead-link state.
  }
}
```

Today `assets` is `{}` — nothing has been released. Android/iOS never look up this file: the
mobile companion route (pair a browser to a Core, no app to install) is the same regardless
of whether a native app ever ships, so `install.html` never gates it on a release.

## Design notes

- Design tokens copied verbatim from `frontend/index.html:29-60` (dark-only,
  `--accent:#3db54a` reserved for confident/shipped state and primary actions, `--warn`
  for "needs attention / not published yet").
- `install.html`'s platform detection is feature-detected
  (`navigator.userAgentData.getHighEntropyValues`, falling back to `navigator.userAgent`
  parsing) and documented in full on `privacy.html`. It degrades to a fully usable, static
  "pick your platform" list with JavaScript disabled (progressive enhancement, not a
  requirement).
- Every honesty gap that exists in the product today (no packaged installers, the Node
  dashboard panel not built, the Android kiosk launcher unsigned, macOS/Linux desktop-shell
  LAN HTTPS trust not implemented) is stated on the site rather than smoothed over.
