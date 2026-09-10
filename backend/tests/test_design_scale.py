"""The design system exists in code, and the sprawl it inherited only shrinks.

## What the count actually was

Before the scale was written down, this stylesheet carried **325 font-size
declarations across 64 distinct values**. Six of them — .72, .74, .76, .78, .8,
.82rem — accounted for 156 uses, and no person alive can tell those apart. Also
21 border-radii and six font-weights, two of which were 560 and 650.

That is what "no design system in code" looks like when you count it instead of
asserting it.

## Why this is a ratchet and not a rewrite

Converting all 325 in an 18,000-line stylesheet is 325 chances to shift a
layout for no functional gain, and it would land as one unreviewable diff. So
the scale is defined in `:root`, new CSS uses it, and the ceilings below hold
the distinct-value counts at what they were the day this was written. They may
go DOWN freely. Going up means somebody invented a seventh caption size, and
has to raise a number here to do it — at which point the question "is this
actually a new size, or one of the six you already have?" gets asked.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# Today's counts. Lower them when the sprawl shrinks; raising one is a decision.
#
# Raised on 2026-09-10 by the pass that made the Space the page. This file's
# whole purpose is to make somebody say why, out loud, so:
#
#   font-size 64 -> 66   Three sizes arrived and one left. The Core ambient face
#                        is read from three metres and had no rank of its own:
#                        its state, its qualifier and its clock now sit at
#                        68 / 27 / 24px, and the 36px that used to make the
#                        clock the headline retired with that composition.
#                        Net +2. The dashboard's display tier is a token
#                        (`--fs-display`) rather than a fourth literal.
#   border-radius 20 -> 21   The new value is `0`. The stage's side column is one
#                        surface with hairline divisions instead of three
#                        stacked cards, so the sections inside it give their
#                        corner back. A reset, not a new radius.
#   gap 22 -> 23         One: the Core hero stack's own rhythm, set in vh
#                        because that face is composed against the height of the
#                        display it lives on rather than against a text scale.
#
# Everything else in that pass used the tokens below, which is why three numbers
# moved instead of thirty.
CEILINGS = {
    "font-size": 66,
    "border-radius": 21,
    "font-weight": 6,
    "gap": 23,
}

SCALE_TOKENS = {
    "--fs-xs", "--fs-sm", "--fs-md", "--fs-base", "--fs-lg", "--fs-xl",
    "--fs-2xl", "--fs-display",
    "--fw-normal", "--fw-medium", "--fw-semibold", "--fw-bold",
    "--radius-sm", "--radius-pill",
    "--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6", "--sp-7", "--sp-8",
}


def _css() -> str:
    src = SHELL.read_text(encoding="utf-8")
    return "\n".join(m.group(1) for m in
                     re.finditer(r"<style[^>]*>(.*?)</style>", src, re.S))


def _distinct(prop: str, css: str) -> set[str]:
    """Distinct LITERAL values. A `var(--fs-sm)` is not sprawl, it is the cure
    — counting it as another distinct value would make adopting the scale
    *raise* the number, which is exactly backwards as an incentive."""
    vals = re.findall(re.escape(prop) + r"\s*:\s*([^;}]+)", css)
    return {v.strip() for v in vals if not v.strip().startswith("var(")}


def test_the_scale_is_declared():
    css = _css()
    missing = sorted(t for t in SCALE_TOKENS
                     if not re.search(re.escape(t) + r"\s*:", css))
    assert not missing, f"the design scale lost these tokens: {missing}"


def test_no_property_invents_more_distinct_values_than_it_already_had():
    css = _css()
    over = []
    for prop, ceiling in CEILINGS.items():
        n = len(_distinct(prop, css))
        if n > ceiling:
            over.append(f"{prop}: {n} distinct values > {ceiling}")
    assert not over, (
        "the design sprawl grew:\n  " + "\n  ".join(over)
        + "\n\nBefore raising a ceiling: is this genuinely a new size, or one "
          "of the ones already there? The scale tokens are in :root.")


def test_the_scale_is_actually_used_somewhere():
    """A token nothing references is a comment with extra steps."""
    css = _css()
    used = {t for t in SCALE_TOKENS if f"var({t})" in css}
    assert len(used) >= 6, (
        f"only {len(used)} scale tokens are referenced anywhere: {sorted(used)}. "
        f"A scale nothing uses is decoration.")


def test_the_newest_css_does_not_invent_sizes():
    """The runtime/attention/coverage block is the CSS written most recently,
    and it is held to the scale — it was itself carrying seven distinct sizes
    for ten uses before this."""
    css = _css()
    # Anchor on the RULE, at the start of its own line -- not on the string.
    # `.runtime-chip{` also appears inside a selector list in a touch-target
    # media query 540 lines above, and inside a `@media (max-width:820px)`
    # one-liner below; `.index()` found the first and sliced in most of the
    # stylesheet, then reported 47 literal sizes from CSS this test is not
    # about. If a third standalone rule ever appears, say so instead of
    # quietly reading somewhere else.
    inicios = [m.start() for m in re.finditer(r"^\.runtime-chip\{", css, re.M)]
    assert len(inicios) == 1, (
        f"expected one standalone `.runtime-chip` rule, found {len(inicios)} -- "
        f"this test can no longer tell which block it is reading")
    start = inicios[0]
    end = css.index("\n", css.index('.core-runtime[data-state="unavailable"]'))
    assert end > start, "the block anchors are out of order"
    block = css[start:end]
    literal = [v.strip() for v in re.findall(r"font-size\s*:\s*([^;}]+)", block)
               if not v.strip().startswith("var(")]
    assert not literal, (
        f"literal font sizes are back in the newest block: {literal}")


def test_the_counter_is_looking_at_real_css():
    css = _css()
    assert len(css) > 50_000
    assert len(_distinct("font-size", css)) > 20, "the scan found almost nothing"
