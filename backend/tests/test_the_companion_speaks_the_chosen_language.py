"""A new English sentence must not be able to enter the companion unnoticed.

The phone app carried about two hundred and thirty sentences and called the
dashboard's translator zero times. Every one of them rendered in English on a
handset whose owner had set the product to Portuguese — including the screens
that come FIRST: find a hub, verify its certificate, ask to be let in, choose
what this device does. A person's first ten minutes with Wavr were in the wrong
language, and no check in this repository could see it, because every
localisation check read `frontend/` and the companion lives in `mobile/`.

They are all routed now. This is the part that keeps them routed.

## What is measured

Not "does the catalogue have entries" — `test_localisation.py` already asks
that, and it asks it of the keys the shim DECLARES. This asks the other half:
is there a literal in the shim sitting in a position where it becomes text on
a screen, without passing the translator? That is the shape of the regression,
and it is invisible to a catalogue check: an unrouted literal declares no key,
so nothing is missing, so everything looks complete.

The positions are the doors the companion actually renders through:

    el(tag, cls, TEXT)                       -- el's third argument IS textContent
    x.textContent = TEXT
    x.title = TEXT
    x.placeholder = TEXT
    x.setAttribute("aria-label"|"title"|"placeholder"|"data-tip", TEXT)
    document.createTextNode(TEXT)

A literal that never passes one of those is not on anybody's screen. Machinery
— CSS classes, wire values, `Error` messages, WebSocket close reasons — never
reaches a door, so this never asks about it, which is why the rule can be
mechanical instead of a judgement about what "looks like prose".
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "backend"))
from tests.mobile_tree import mobile_dir   # noqa: E402 -- shared lookup

SHIM = mobile_dir() / "src" / "wavr-mobile-shim.js"

LIT = r'(?P<q>["\'])(?P<t>(?:(?!(?P=q))[^\\]|\\.)*)(?P=q)'

PORTAS = [
    ("el(…, …, text)", re.compile(
        r'\bel\(\s*["\'][a-z0-9]+["\']\s*,\s*'
        r'(?:["\'][^"\']*["\']|null)\s*,\s*' + LIT)),
    ("textContent", re.compile(r'\.textContent\s*=\s*' + LIT)),
    ("title", re.compile(r'\.title\s*=\s*' + LIT)),
    ("placeholder", re.compile(r'\.placeholder\s*=\s*' + LIT)),
    ("setAttribute", re.compile(
        r'\.setAttribute\(\s*'
        r'["\'](?:aria-label|title|placeholder|data-tip)["\']\s*,\s*' + LIT)),
    ("createTextNode", re.compile(r'\bcreateTextNode\(\s*' + LIT)),
]

# A literal with no letters is punctuation or a number, not a sentence.
TEM_PALAVRA = re.compile(r"[A-Za-z]{2,}")


def _fonte() -> str:
    return io.open(SHIM, encoding="utf-8", newline="").read().replace(
        "\r\n", "\n")


def _atravessa_o_tradutor(src: str, ini: int) -> bool:
    """Is this literal inside a `T(...)` call?

    Walks back over any `literal +` chain first, because a sentence the source
    broke across lines arrives at the door as `T("half " + "half")` and only
    the FIRST half is preceded by `T(`.
    """
    antes = src[:ini].rstrip()
    while antes.endswith("+"):
        antes = antes[:-1].rstrip()
        if not antes or antes[-1] not in "\"'":
            break
        aspas = antes[-1]
        k = antes.rfind(aspas, 0, len(antes) - 1)
        while k > 0 and antes[k - 1] == "\\":
            k = antes.rfind(aspas, 0, k)
        if k < 0:
            break
        antes = antes[:k].rstrip()
    return antes.endswith("T(") or antes.endswith("WavrT(")


def _crus():
    src = _fonte()
    fora = []
    for nome, rx in PORTAS:
        for m in rx.finditer(src):
            s = m.group("t")
            if not TEM_PALAVRA.search(s):
                continue
            if _atravessa_o_tradutor(src, m.start("q")):
                continue
            fora.append((src.count("\n", 0, m.start()) + 1, nome, s))
    return sorted(fora)


def test_every_sentence_the_companion_shows_goes_through_the_translator():
    fora = _crus()
    assert not fora, (
        f"{len(fora)} literals in {SHIM.name} become text on a screen without "
        f"passing T(), so they render in English whatever language the reader "
        f"chose:\n  "
        + "\n  ".join(f"line {l} ({porta}): {s!r}" for l, porta, s in fora[:20])
        + ("\n  …" if len(fora) > 20 else "")
        + "\n\nWrap the literal: T(\"…\"), or T(\"… {slot} …\", { slot: value }) "
          "when a value goes inside the sentence.")


def test_the_translator_the_companion_calls_is_the_dashboard_s_own():
    """`T()` must be a route to `WavrT`, not a second translator.

    A local helper that returned its argument would satisfy the test above
    perfectly and translate nothing — the checking half present, the producing
    half gone. So the helper is read: it has to define `T`, it has to reach
    `window.WavrT`, and it has to fall back to the English source rather than
    throw on the overlays it draws before the dashboard exists.
    """
    src = _fonte()
    assert "function T(s, args)" in src, (
        f"{SHIM.name} no longer defines T(s, args); the wrapping above is "
        f"calling something else")
    corpo = src[src.index("function T(s, args)"):]
    corpo = corpo[:corpo.index("\n  }") + 4]
    assert "window.WavrT" in corpo, (
        "T() no longer reaches the dashboard's translator, so every sentence "
        "in the companion renders in English on a translated screen")
    assert "return s;" in corpo, (
        "T() no longer falls back to its argument. The companion draws "
        "overlays before the dashboard has loaded, where window.WavrT does "
        "not exist yet; without the fallback those screens raise instead of "
        "rendering English")


def test_the_language_tables_are_rebuilt_rather_than_frozen():
    """A table of sentences at module level is written in whatever language
    was in force while the file was PARSED — which, for this file, is before
    `js/i18n.js` has run and before any catalogue exists.

    Both tables that used to be object literals are functions now, for that
    reason, and this is what stops the next one from being written flat. The
    frontend reached the same conclusion about two of its own tables.
    """
    src = _fonte()
    assert "function whatsNew()" in src, (
        "WHATS_NEW went back to being a module-level object literal; its "
        "sentences would be translated once, at parse time, into whatever "
        "language was active before the catalogue loaded — which is none")
    assert "function consentWords(" in src, (
        "the consent level's label and tip went back into the CONSENT table; "
        "at module level they translate before the catalogue exists and stay "
        "English for the life of the page")
    # And the wire values did NOT move into the words function: `next` drives
    # the tap cycle and `color` drives the CSS variable. Translating either
    # would break the control rather than the copy.
    tabela = src[src.index("var CONSENT = {"):]
    tabela = tabela[:tabela.index("};")]
    assert "T(" not in tabela, (
        "a translator call appeared inside the CONSENT table, which holds "
        f"wire values and colours, not words:\n{tabela}")
