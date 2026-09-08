"""The public site and the product are one visual language, and stay one.

Somebody who reads the site and then installs Wavr should not feel they have
arrived somewhere else. Today they do not: every colour, radius and surface the
two have in common is byte-identical.

## So what is this test for

The palette is duplicated **by hand**. The dashboard is one self-contained HTML
file with its CSS inline; the site is a separate static bundle published to
Cloudflare with no build step, so there is no import to share and no bundler to
enforce anything. Change `--accent` in one and the other keeps the old green
silently, and nobody finds out until the screenshots in a README stop matching
the app.

That is the same shape as every other bug worth pinning here: two producers of
one value, no mechanism keeping them honest. This file is the mechanism.

## What it does NOT require

The site is allowed tokens the app has no use for (`--content-w`, `--mono`) and
the app is allowed tokens the site has no use for — the runtime STATE colours
being the clear case, because the site renders no live state at all and giving
it a `--state-degraded` would be inviting a marketing page to imply one.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "frontend" / "index.html"
SITE = ROOT / "site" / "public" / "assets" / "wavr.css"

# The tokens that carry the identity. A token outside this list may legitimately
# live in one and not the other; these must agree wherever both define them.
IDENTITY = {
    "--bg", "--surface", "--elevated", "--elevated-2", "--line",
    "--line-strong", "--text", "--dim",
    "--accent", "--accent-soft", "--warn", "--warn-soft",
    "--danger", "--danger-soft", "--radius",
}


def _root_tokens(path: Path) -> dict[str, str]:
    src = path.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\}", src, re.S)
    assert block, f"{path.name} has no :root block — this test measures nothing"
    return {k: v.strip().lower()
            for k, v in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+);",
                                   block.group(1))}


def test_every_identity_token_the_site_defines_matches_the_product():
    app, site = _root_tokens(APP), _root_tokens(SITE)
    drift = []
    for name in sorted(IDENTITY):
        if name in app and name in site and app[name] != site[name]:
            drift.append(f"{name}: app {app[name]!r} vs site {site[name]!r}")
    assert not drift, (
        "the site and the product have drifted apart:\n  " + "\n  ".join(drift)
        + "\n\nThere is no build step joining them — the palette is duplicated "
          "by hand, so this is the only thing that notices.")


def test_the_site_actually_defines_the_identity():
    """Deleting a token would make the comparison above vacuously pass while
    the site quietly fell back to browser defaults."""
    site = _root_tokens(SITE)
    missing = sorted(IDENTITY - set(site))
    assert not missing, (
        f"the site no longer defines: {missing}. If one was dropped on purpose, "
        f"take it out of IDENTITY here and say why.")


def test_the_product_actually_defines_the_identity():
    app = _root_tokens(APP)
    missing = sorted(IDENTITY - set(app))
    assert not missing, f"the dashboard no longer defines: {missing}"


def test_the_site_does_not_borrow_the_runtime_state_colours():
    """The state palette means "this is what your Core is doing right now". A
    marketing page has no live Core, and a page that paints itself in
    `--state-healthy` is claiming something it cannot know."""
    css = SITE.read_text(encoding="utf-8")
    borrowed = sorted(set(re.findall(r"--state-[a-z]+", css)))
    assert not borrowed, (
        f"the site uses runtime state colours: {borrowed}. Those say something "
        f"about a running Core, and the site is not looking at one.")


def test_both_set_text_in_the_same_typeface():
    """Colour is half of it. A site in Segoe and an app in Inter reads as two
    products however well the greens match."""
    stack = re.compile(r'font-family\s*:\s*(system-ui[^;}]*)')
    app = stack.search(APP.read_text(encoding="utf-8"))
    site = stack.search(SITE.read_text(encoding="utf-8"))
    assert app and site, "one of them stopped declaring a system-ui stack"
    norm = lambda s: re.sub(r"\s+", "", s.group(1)).lower()
    assert norm(app) == norm(site), (
        f"the body typeface drifted:\n  app  {app.group(1)}\n  site "
        f"{site.group(1)}")


def test_the_comparison_can_actually_fail():
    """A parser that silently stops matching turns all of the above green."""
    app, site = _root_tokens(APP), _root_tokens(SITE)
    assert len(app) >= 10, f"only parsed {len(app)} app tokens"
    assert len(site) >= 10, f"only parsed {len(site)} site tokens"
    overlap = IDENTITY & set(app) & set(site)
    assert len(overlap) >= 12, (
        f"only {len(overlap)} identity tokens are defined in both, so the "
        f"drift check covers almost nothing: {sorted(overlap)}")
