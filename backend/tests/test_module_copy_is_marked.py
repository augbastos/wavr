"""Copy that never reaches the translator, in the three shapes it hides in.

`test_localisation.py` proves that everything the product DECLARES translatable
has a translation. It cannot prove the other direction, and that is the gap this
closes: a sentence that never reaches `WavrT` is not declared, so it is not
missing, so every completeness check there passes while a household reads
English.

## What was actually found, and why one rule would not have caught it

Four separate defects, one per shape, all in modules that were otherwise fully
translated:

  1. **A data table of literals.** The precision ladder — a whole named feature —
     rendered out of `PRECISION_WORD` and `PRECISION_NEXT_COPY`, two
     module-level objects in `radar.js`. Nothing that scans for `WavrT(` can see
     inside a table, and a table is built ONCE, at parse time, so even marked-up
     literals would have frozen whichever language was active on load.
  2. **A template literal in a sink.** The map's hover text was
     `` `person ${id} · confidence ${pct}%` ``. The runtime receives a different
     string every time, so it can never be a catalogue key.
  3. **A bare literal in a ternary inside a template.** A history row read
     `` `${occupied ? "occupied" : "empty"} (…)` ``. Both words are literals; both
     are invisible where they sit.
  4. **A bare literal beside a translated sibling.** `identity.js` had
     `actionFeedback(fb, false, WavrT("couldn't add — check the name"))` next to
     `actionFeedback(fb, true, null, "✓ added")`, four times. The same button
     answered in Portuguese when it failed and in English when it worked.

So the rules below are three, by mechanism: prose sitting anywhere outside
`WavrT`, a bare literal landing in a user-visible sink, and a template literal
landing in one.

## Every extractor here is `test_localisation`'s

`_js_string_literals`, `_first_argument` and `_keys_in` are imported, not
reimplemented. A second, subtly different extractor would disagree with the real
completeness check somewhere, and the first anybody would hear of it is a test
that fails on correct code or passes on broken code.

`_keys_in` in particular does exactly the job a sink needs: given an expression,
it returns the string literals that expression can EVALUATE TO, discarding any
alternative that is not purely literals (`s.health === "dead" ? WavrT("no
signal") : …` yields nothing, because the condition holds an identifier and both
branches are calls). A non-empty answer with a letter in it is, by construction,
copy the sink can put on screen without the translator ever seeing it.

## Why a list of modules and not the whole frontend

Forty-four modules were written before the translation layer existed and several
are being reworked right now. A rule that fails on all of them at once is a rule
somebody switches off. `GUARDED` is a ratchet: it holds what is clean, and a
module joins it when it is cleaned. It is not a claim that the rest is fine —
`test_localisation.module_strings()` still ADMITS a bare module literal as
justification for a catalogue entry, which is exactly the leniency this rule
removes, one file at a time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_localisation import (
    FRONTEND,
    _first_argument,
    _js_string_literals,
    _keys_in,
)

# The modules this rule holds. Adding one is how the rule spreads; removing one
# needs a reason written next to it.
GUARDED = ("radar.js", "render.js", "housemap.js", "identity.js", "format.js")

SHELL = (FRONTEND / "index.html").read_text(encoding="utf-8")


def _blank_comments(text: str) -> str:
    """The same source with every comment replaced by spaces of equal length.

    Comments have to go, and they have to go without moving anything: the rules
    below report `file:line`, and two of them describe the exact defect they
    look for IN a comment — the note above `renderIdentity()` quotes
    `actionFeedback(fb, true, null, "✓ added")` as the thing that was wrong, and
    a scanner that cannot tell code from prose flags the explanation of why it
    exists. Blanking in place keeps every offset and every line number.

    Not `re.sub`: a `/*` inside a STRING (a URL, a glob) starts a match that
    eats everything up to the next `*/` anywhere in the file. This walks, with
    the same string and template rules `_js_string_literals` uses.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i) if "\n" not in text[i:j]
                       else re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c in "\"'`":
            q, start, i = c, i, i + 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
            out.append(text[start:i])
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _src(name: str) -> str:
    """The module as the browser runs it: code only, comments blanked out."""
    return _blank_comments((FRONTEND / "js" / name).read_text(encoding="utf-8"))


def _has_letter(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


# --------------------------------------------------------------------------- #
# Shape 1: prose, anywhere.
# --------------------------------------------------------------------------- #

# A word is alphabetic. Everything a stylesheet, a selector, a wire value or a
# fragment of markup is made of — `width="13"`, `max-height:820px`, `2-digit`,
# `application/json` — is not, which is what keeps this off code.
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*$")
_EDGE = "<>()[]{}\"'.,;:!?#/\\"


def _is_prose(text: str) -> bool:
    """Three or more plain English words in a row is a sentence, not a token.

    Two is not enough: `card card-unwatched`, `use strict`, `Ground floor` and
    `living room` are all two words, and the last two are DELIBERATELY untranslated
    (a room name is a join key between the floor plan and RoomState — translating
    it would silently unlink the two).
    """
    words = [w for w in (t.strip(_EDGE) for t in text.split()) if _WORD.match(w)]
    return len(words) >= 3


def _wavrt_keys(text: str) -> set[str]:
    """Every literal this file already hands to the translator.

    Every literal INSIDE the first argument, not `_keys_in`'s answer for it.
    The two differ, and the difference matters here: `_keys_in` is
    all-or-nothing by design, so a nested ternary — `WavrT(a ? (b ? "x" : "y")
    : …)`, which render.js uses for the four singular/plural forms of the
    position rung — yields nothing at all. Those strings DO reach the runtime;
    they are simply not derivable statically, and reporting them as unmarked
    copy would be a false alarm about correct code.
    """
    keys: set[str] = set()
    for m in re.finditer(r"\bWavrT\(", text):
        arg = _first_argument(text, m.end() - 1)
        keys |= {" ".join(s.split()) for s in _js_string_literals(arg)}
    return keys


@pytest.mark.parametrize("name", GUARDED)
def test_prose_in_a_module_reaches_the_translator(name):
    text = _src(name)
    translated = _wavrt_keys(text)
    bare = sorted({
        norm for norm in (" ".join(s.split()) for s in _js_string_literals(text))
        if _is_prose(norm) and norm not in translated
    })
    assert not bare, (
        f"{name} builds {len(bare)} English sentence(s) that never reach "
        f"WavrT, so they render English in every language:\n  "
        + "\n  ".join(repr(b) for b in bare)
        + "\n\nIf the literal lives in a module-level table, make the table a "
          "FUNCTION that returns WavrT(...) — see precisionWord() in radar.js, "
          "kinds() in wizard.js, connDesc() in connectors.js. A table is also "
          "built once, at parse time, so it freezes the launch language.")


# --------------------------------------------------------------------------- #
# Shapes 2 and 3: whatever lands in something a person reads.
# --------------------------------------------------------------------------- #

# The places a string becomes visible text. `innerHTML` is deliberately absent:
# the static skeletons built with it here are overwritten field by field with
# textContent immediately afterwards, and reading markup is `test_copy_audit`'s
# job, not this one's.
_SINKS = (
    # el.textContent = <expression>
    re.compile(r"\.(?:textContent|innerText|placeholder|title)\s*=\s*"),
    # el.setAttribute("aria-label", <expression>)
    re.compile(r"\.setAttribute\(\s*[\"'](?:aria-label|title|placeholder|"
               r"data-tip)[\"']\s*,\s*"),
    # document.createTextNode(<expression>)
    re.compile(r"\bcreateTextNode\(\s*"),
    # actionFeedback(el, ok, msg, <okMsg>) — the success half, which is the one
    # that was left in English while its failure sibling was translated.
    re.compile(r"\bactionFeedback\([^()]*?,\s*(?:true|false)\s*,\s*[^,()]*,\s*"),
)


def _sink_expressions(text: str):
    """(line, source) of every expression that ends up in front of a person.

    Balanced the way `_first_argument` balances: strings are skipped whole, and
    the expression ends at the `;` that closes the statement or at the `)` that
    closes the call it was an argument to.
    """
    for pattern in _SINKS:
        for m in pattern.finditer(text):
            i, n, depth = m.end(), len(text), 0
            while i < n:
                c = text[i]
                if c in "\"'`":
                    q, i = c, i + 1
                    while i < n:
                        if text[i] == "\\":
                            i += 2
                            continue
                        if text[i] == q:
                            break
                        i += 1
                elif c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif c == ";" and depth == 0:
                    break
                i += 1
            yield text[:m.end()].count("\n") + 1, text[m.end():i]


@pytest.mark.parametrize("name", GUARDED)
def test_a_bare_literal_never_lands_in_front_of_a_person(name):
    text = _src(name)
    offenders = []
    for line, expr in _sink_expressions(text):
        for key in sorted(_keys_in(expr)):
            if _has_letter(key):
                offenders.append(f"{name}:{line}  {key!r}")
    assert not offenders, (
        "these strings are written straight into something a person reads, "
        "without passing through WavrT:\n  " + "\n  ".join(offenders)
        + "\n\nWrap the literal in WavrT(...). Where a sentence has two forms, "
          "write both out whole — Portuguese agrees gender and number around "
          "the word you would be substituting in.")


_SUBST = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", re.S)


def _templates_at_top_level(expr: str):
    """Template literals in `expr` that are not nested inside a call."""
    i, n, depth = 0, len(expr), 0
    while i < n:
        c = expr[i]
        if c in "\"'":
            i += 1
            while i < n:
                if expr[i] == "\\":
                    i += 2
                    continue
                if expr[i] == c:
                    break
                i += 1
        elif c == "`":
            start, i = i + 1, i + 1
            while i < n:
                if expr[i] == "\\":
                    i += 2
                    continue
                if expr[i] == "`":
                    break
                i += 1
            if depth == 0:
                yield expr[start:i]
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1


@pytest.mark.parametrize("name", GUARDED)
def test_a_template_literal_never_lands_in_front_of_a_person(name):
    """A template is a different string on every call, so it can never be a key.

    Checked in both halves, because the words hide in either: the literal text
    between the substitutions (`person …  · confidence …`), and bare literals
    inside a substitution (`${occupied ? "occupied" : "empty"}`).
    """
    text = _src(name)
    offenders = []
    for line, expr in _sink_expressions(text):
        for tpl in _templates_at_top_level(expr):
            words = " ".join(_SUBST.sub(" ", tpl).split())
            inner = sorted(k for sub in _SUBST.findall(tpl)
                           for k in _keys_in(sub) if _has_letter(k))
            if _has_letter(words):
                offenders.append(f"{name}:{line}  literal text {words!r}")
            if inner:
                offenders.append(f"{name}:{line}  inside a slot: "
                                 + ", ".join(repr(k) for k in inner))
    assert not offenders, (
        "a template literal is being put in front of a person, so the sentence "
        "it produces can never be looked up:\n  " + "\n  ".join(offenders)
        + "\n\nWrite the whole sentence as a WavrT key with {slot} names, and "
          "pass the values in. Where the sentence has two forms, write two "
          "keys rather than one with a word substituted into it.")


# --------------------------------------------------------------------------- #
# The numbers inside those sentences.
# --------------------------------------------------------------------------- #

# Every way of turning a number or an instant into text that is NOT WavrFmt.
# Each one bakes in one locale's conventions and no other: `toFixed` writes an
# English decimal point, an ISO slice writes UTC on a 24-hour clock, and
# `toLocaleString` with no argument writes whatever the browser guessed, which
# is not the locale the reader chose in this product's own language control.
_BY_HAND = (
    (re.compile(r"\.toFixed\("), "toFixed — WavrFmt.number(v, {maximumFractionDigits: n})"),
    (re.compile(r"\.toLocaleString\("), "toLocaleString — WavrFmt.number / WavrFmt.dateTime"),
    (re.compile(r"\.toLocaleTimeString\("), "toLocaleTimeString — WavrFmt.time"),
    (re.compile(r"\.toLocaleDateString\("), "toLocaleDateString — WavrFmt.date"),
    (re.compile(r"\.slice\(\s*11\s*,"), "an ISO slice — WavrFmt.time; characters "
                                        "11..19 of an ISO string are UTC on a "
                                        "24-hour clock, in every timezone"),
)


@pytest.mark.parametrize("name", GUARDED)
def test_a_module_never_formats_a_number_or_a_time_by_hand(name):
    """`format.js` is exempt: it IS the implementation."""
    if name == "format.js":
        pytest.skip("format.js is where the locale-aware formatting lives")
    text = _src(name)
    offenders = []
    for pattern, what in _BY_HAND:
        for m in pattern.finditer(text):
            offenders.append(f"{name}:{text[:m.start()].count(chr(10)) + 1}  {what}")
    assert not offenders, (
        "a number or an instant is being formatted by hand, so it renders in "
        "one fixed locale inside sentences translated into another:\n  "
        + "\n  ".join(offenders))


def test_the_number_formatter_is_actually_used():
    """`WavrFmt.number` and `WavrFmt.metres` shipped with ZERO call sites.

    That is the failure mode this whole family of helpers has: forgetting them
    looks right to whoever is reading the screen in English, so a module that
    interpolates `String(0.82)` into a Portuguese sentence passes review. A
    formatter nothing calls is not a formatter, it is a plan.

    `metres` is deliberately not asserted here yet: the frontend's only distance
    on screen is in `cameras.js`, which is another lane's file — see the
    handoff. Widen this the moment that call site lands.
    """
    callers = [f.name for f in sorted(FRONTEND.glob("js/*.js"))
               if f.name != "format.js" and "WavrFmt.number(" in
               f.read_text(encoding="utf-8")]
    assert callers, (
        "nothing in the frontend calls WavrFmt.number, so every fraction on "
        "screen is being written with an English decimal point")


# --------------------------------------------------------------------------- #
# The extractors themselves, because a broken one passes every rule above.
# --------------------------------------------------------------------------- #

def test_the_extractors_still_see_the_modules():
    """Three of these rules assert that a set is EMPTY. An import that silently
    stopped matching would satisfy all of them forever."""
    # render.js today: 212 literals, 34 WavrT arguments, 36 sinks. The floors are
    # set well under those so ordinary editing does not trip them, and well over
    # zero so an import that stopped matching does.
    text = _src("render.js")
    assert len(_js_string_literals(text)) > 100, (
        "the literal scanner has stopped seeing render.js")
    assert len(_wavrt_keys(text)) > 20, (
        "the WavrT scanner has stopped seeing render.js")
    sinks = list(_sink_expressions(text))
    assert len(sinks) > 20, (
        f"only {len(sinks)} user-visible sinks found in render.js; the sink "
        f"patterns have stopped matching the code")

    # And it must still be able to SEE a defect, not merely find nothing.
    assert _is_prose("Add a room sensor (Bluetooth/radar) to locate the room")
    assert not _is_prose("card card-unwatched")
    assert _keys_in('"✓ added"') == {"✓ added"}
    # `_keys_in` is all-or-nothing: an expression that is not purely literals
    # yields nothing at all. That is why the `actionFeedback` pattern above
    # consumes the `msg` argument as well — pointed at `null, "✓ added"` this
    # would see an identifier first and report the file clean.
    assert _keys_in('null, "✓ added"') == set()
    assert _keys_in('s.health === "dead" ? WavrT("no signal") : x') == set()


# --------------------------------------------------------------------------- #
# The unwatched-room card: legible, not merely quiet.
# --------------------------------------------------------------------------- #

def _root_tokens() -> dict[str, str]:
    at = SHELL.index(":root{")
    i, depth = at + len(":root{"), 1
    while depth:
        if SHELL[i] == "{":
            depth += 1
        elif SHELL[i] == "}":
            depth -= 1
        i += 1
    block = SHELL[at:i]
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})", block))


def _rgb(value: str) -> tuple[float, float, float]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(value: str) -> float:
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _unwatched_block() -> str:
    text = _src("render.js")
    at = text.index("function syncUnwatchedRooms()")
    end = text.index("\n}", at)
    return text[at:end]


def test_the_unwatched_room_note_is_legible():
    """The one sentence on that card with something to DO in it.

    It was the least legible text on the Space screen: `opacity: 0.68` on the
    whole card faded `--dim` to 3.6:1 against the page behind it — below WCAG AA
    — while the point of the card is to be read and acted on. Opacity is the
    wrong tool for "quiet": it fades the text along with the border it was meant
    to soften. The dashed border carries "not covered"; the colour carries the
    quiet, and it is a palette token chosen to clear AA on this background.
    """
    block = _unwatched_block()
    assert "style.opacity" not in block, (
        "the unwatched-room card is dimmed with opacity again. That fades the "
        "explanatory sentence along with the border and puts it under WCAG AA; "
        "use a colour token for the quiet and leave the text at full strength.")

    m = re.search(r"note\.style\.color\s*=\s*\"var\((--[a-z0-9-]+)\)\"", block)
    assert m, ("the unwatched-room note does not state its own colour, so it "
               "inherits whatever `.panel-note` happens to be and nothing here "
               "can check it reads")
    token = m.group(1)

    tokens = _root_tokens()
    assert token in tokens, (
        f"{token} is not a token in index.html's :root — this card must reuse "
        f"the palette, not invent a colour")

    card_bg = re.search(r"\.card\{[^}]*background:\s*var\((--[a-z0-9-]+)\)", SHELL)
    assert card_bg, "`.card` no longer names its background token"
    bg = tokens[card_bg.group(1)]

    ratio = _contrast(tokens[token], bg)
    assert ratio >= 4.5, (
        f"the unwatched-room note is {token} on {card_bg.group(1)} = "
        f"{ratio:.2f}:1, under the 4.5:1 WCAG AA floor for body text")
