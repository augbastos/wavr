"""Wavr has one palette, and every colour on screen comes out of it.

## What went wrong

The runtime-state colours were added as raw hexes lifted from GitHub Primer —
`#3fb950`, `#d29922`, `#f85149`, `#8b949e`. Alongside the product's own
`--accent #3db54a`, `--warn #e8a13a`, `--danger #e8726a`, that shipped **two
greens, two ambers and two reds**. Nothing looked broken. It just looked like a
generic dark admin panel, which is exactly what a borrowed palette makes a
product look like.

Worse, eight rules read `var(--fg-dim, #8b949e)`. `--fg-dim` is not a token this
product has ever defined — it is Primer's name. So every one of those silently
rendered the fallback: the borrowed grey, never the product's `--dim`. A
consumer reading a key the producer never emits, in CSS, where nothing warns
you and nothing looks wrong.

## The two rules

1. **No custom property is consumed that nothing defines.** That is the bug
   above, and CSS will not tell you: it takes the fallback, or renders nothing,
   and either way the page still looks plausible.
2. **No borrowed literal comes back.** Named explicitly, because the way this
   returns is somebody pasting a snippet from a component library.

An exemption exists for `--ax`, which JavaScript sets inline as a position, not
a colour — its fallback is a real default, not a substitute palette.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# Set from JS as an inline style, so a stylesheet default is correct.
SET_AT_RUNTIME = {"--ax"}

# Primer values that were in this file. Not a blocklist of a whole design
# system — just the specific strings that were here, so their return is loud.
BORROWED = {
    "#3fb950": "Primer green (Wavr's is --accent #3db54a)",
    "#d29922": "Primer amber (Wavr's is --warn #e8a13a)",
    "#f85149": "Primer red (Wavr's is --danger #e8726a)",
    "#8b949e": "Primer grey (Wavr's is --dim #9AA4AD)",
    "#6e7681": "Primer dim grey (Wavr's is --dim-deep)",
    "#58a6ff": "Primer blue (Wavr's is --info)",
    "#db6d28": "Primer orange (Wavr's is --attention)",
    "#30363d": "Primer border (Wavr's is --line)",
}

STATE_TOKENS = {
    "--state-healthy", "--state-starting", "--state-updating",
    "--state-paused", "--state-degraded", "--state-attention",
    "--state-unavailable",
}


def _css() -> str:
    src = SHELL.read_text(encoding="utf-8")
    return "\n".join(m.group(1) for m in
                     re.finditer(r"<style[^>]*>(.*?)</style>", src, re.S))


def test_no_custom_property_is_read_that_nothing_defines():
    css = _css()
    used = {m.group(1) for m in re.finditer(r"var\((--[A-Za-z0-9-]+)", css)}
    defined = {m.group(1) for m in re.finditer(r"(--[A-Za-z0-9-]+)\s*:", css)}
    orphans = sorted(used - defined - SET_AT_RUNTIME)
    assert not orphans, (
        f"these are read but never defined: {orphans}\n"
        f"CSS will not tell you: it silently takes the fallback after the "
        f"comma, or renders nothing. If one is set from JS at runtime, add it "
        f"to SET_AT_RUNTIME here and say so.")


def test_no_borrowed_palette_literal_is_back():
    css = _css().lower()
    back = sorted(f"{h} — {why}" for h, why in BORROWED.items() if h in css)
    assert not back, (
        "borrowed palette values are back in the stylesheet:\n  "
        + "\n  ".join(back)
        + "\n\nTwo greens is how a product starts looking like every other "
          "dark dashboard. Use the token.")


def test_every_state_colour_comes_from_the_product_palette():
    """A state is not a different brand. It is this product, saying
    something — so each state maps onto a role in the one palette."""
    css = _css()
    literal = []
    for token in sorted(STATE_TOKENS):
        m = re.search(re.escape(token) + r"\s*:\s*([^;]+);", css)
        assert m, f"{token} is no longer defined"
        value = m.group(1).strip()
        if not value.startswith("var(--"):
            literal.append(f"{token}: {value}")
    assert not literal, (
        "these state colours are raw values rather than palette roles:\n  "
        + "\n  ".join(literal)
        + "\n\nIf the palette genuinely lacks the role, add it to the palette.")


def test_the_roles_the_state_block_needs_exist_in_the_palette():
    css = _css()
    for token in ("--info", "--attention", "--dim-deep"):
        assert re.search(re.escape(token) + r"\s*:\s*#", css), (
            f"{token} is gone from the palette, so the state that maps onto it "
            f"resolves to nothing.")


def test_the_scan_still_finds_the_stylesheet():
    """Every assertion above passes trivially against an empty string."""
    css = _css()
    assert len(css) > 50_000, f"only found {len(css)} bytes of CSS"
    assert css.count("var(--") > 500, "the token scan found almost no uses"
