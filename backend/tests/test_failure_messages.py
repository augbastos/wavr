"""A failure must say what to do, not only that something did not happen.

## What was measured

75 call sites render a failure through `actionFeedback(el, false, msg)`. Sorted
by whether the message tells a person anything actionable — a reason, a next
step, a place to look — a large minority say only the noun: "couldn't save",
"update failed", "connection failed".

Two of those are not the same event, and the wording hid it:

  * The fetch **threw**. The Core was never reached, nothing was attempted, and
    pressing the button again right now does exactly the same thing.
  * The Core **answered and refused**. Something was attempted and declined,
    and it may have said why.

`failureText(r, verb)` makes that distinction once, and prefers the Core's own
`detail` over anything written in the client. The four call sites that already
shared the "server detail, else one noun" shape now use it.

## Why this is a ratchet

Rewriting all 75 is a broad copy change with a per-site judgement call, landing
as one unreviewable diff over user-visible text. So this freezes the count of
bare failure messages at today's number. It may fall freely. Raising it means
somebody added another dead end, and has to say so here.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# Today's count of failure strings that report the failure and nothing else.
BARE_CEILING = 29

FAIL_WORDS = re.compile(
    r"\b(fail|failed|error|couldn'?t|could not|cannot|can'?t|unable|denied|"
    r"refused|rejected|invalid|not available|unavailable|timed out|"
    r"went wrong|no response)\b", re.I)

# A message earns its keep if it says what to do, where to look, or why.
# "is it running?" counts: it is a question a person can act on. Leaving it out
# was the first version of this classifier, and it overcounted.
ACTIONABLE = re.compile(
    r"\b(try|turn on|turn it|check|open|add|enable|run|running|use|go to|tap|"
    r"click|press|set |settings|because|needs|requires|first|instead|then |"
    r"nothing changed|nothing was)\b|\?", re.I)


def _js() -> str:
    """Every line of JavaScript the page runs.

    This used to pull the inline `<script>` bodies out of index.html, which at
    the time was all of it. After the shell was modularised the same code lives
    in `frontend/js/*.js`, where there are no tags to strip — so the old
    extraction came back with the import map and nothing else, and every
    assertion below started failing on code that still ships unchanged.

    HTML comments are removed first: one of them mentions the wizard's "own
    <script>", and a `<script>` regex reads that as an opening tag and swallows
    a thousand lines of markup into what this function calls JavaScript.
    """
    from tests.frontend_source import MODULES, SHELL
    markup = re.sub(r"<!--.*?-->", "", SHELL, flags=re.S)
    inline = "\n".join(
        m.group(1) for m in
        re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                    markup, re.S))
    return "\n".join([inline, *MODULES.values()])


def _messages() -> tuple[list[str], list[str]]:
    js = _js()
    # The localisation wrapper must not blind this audit. Every user-facing
    # literal now reads `x.textContent = WavrT("…")`, and a scanner that only
    # knew the bare form reports the count collapsing to near zero while
    # nothing about the messages has changed. A translation layer is not
    # allowed to switch off a check by moving a quote mark.
    #
    # It did, though, and for six weeks. The first attempt at this line wrote
    # `(?:t\()?` — the global's original name — and the global had already been
    # renamed to `WavrT` because thirty-five local `t` declarations shadowed
    # it. So the scanner matched nothing wrapped, `fine` fell to 1, and the
    # ceiling below passed for the only reason a ceiling must never pass: there
    # was nothing left to count. `test_plenty_of_messages_already_do_the_right
    # _thing` is the guard against exactly that, and it is the test that
    # eventually said so.
    pat = re.compile(
        r'(?:textContent|innerText|actionFeedback\([^,]+,\s*false,)\s*=?\s*'
        r'(?:WavrT\()?'
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')')
    seen, bare, fine = set(), [], []
    for m in pat.finditer(js):
        body = m.group(1)[1:-1]
        if len(body) < 8 or body in seen or not FAIL_WORDS.search(body):
            continue
        seen.add(body)
        (fine if ACTIONABLE.search(body) else bare).append(body)
    return bare, fine


def test_the_bare_failure_count_does_not_grow():
    bare, fine = _messages()
    assert len(bare) <= BARE_CEILING, (
        f"{len(bare)} failure messages now say only that something failed "
        f"(ceiling {BARE_CEILING}). The new ones are likely among:\n  "
        + "\n  ".join(repr(b) for b in sorted(bare, key=len)[-8:])
        + "\n\nSay what to do, or what it cost. `failureText(r, verb)` already "
          "distinguishes an unreachable Core from a refused request.")


def test_plenty_of_messages_already_do_the_right_thing():
    """If this drops toward zero the classifier has broken, and the ceiling
    above would then pass for the wrong reason."""
    bare, fine = _messages()
    assert len(fine) >= 15, (
        f"only {len(fine)} actionable failure messages found — the scan is "
        f"probably not matching any more.")


def test_the_shared_helper_exists_and_separates_the_two_cases():
    js = _js()
    assert "async function failureText(r, verb)" in js, (
        "failureText is gone; the four sites using it now say nothing useful.")
    body = js[js.index("async function failureText(r, verb)"):][:900]
    assert "could not reach the Core" in body, "the unreachable case lost its words"
    assert "refused it" in body, "the refused case lost its words"
    assert "j.detail" in body, (
        "it no longer prefers the Core's own reason, which is always better "
        "than anything written in the client.")


def test_the_canonical_sites_use_it():
    js = _js()
    n = len(re.findall(r"await failureText\(r,", js))
    assert n >= 4, (
        f"only {n} call sites use the shared helper; there were four.")
    leftover = re.findall(
        r'var msg = "[^"]*";\s*try\{ if\(r\)\{ var j = await r\.json\(\);', js)
    assert not leftover, (
        f"{len(leftover)} sites went back to the hand-rolled shape: {leftover}")
