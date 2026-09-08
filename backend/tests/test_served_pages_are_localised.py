"""A page the Core serves is a page the product ships — in every language.

`test_localisation.py` proves the shell is translated, and proves it very
thoroughly. It reads `frontend/index.html` and `frontend/js/*.js`, and that was
the whole product right up until it was not: the Core also serves
`frontend/measure.html` and three reference experiences under `experiences/`,
and NOTHING looked at them.

What that cost, concretely. `measure.html` — the WebXR room-capture page — was
written end to end in Portuguese, and the button that opens it says "Measure
with phone" in English. So it was wrong in both directions at once: a Brazilian
reached it through an English interface, and an English reader reached a page
they could not read. The three experiences were the mirror image: English only,
with no way to be anything else.

## What this file asserts, and why in this shape

Three properties, in the order they depend on each other:

  1. the page LOADS the runtime — `i18n.js`, then `locale-pt.js`, then
     `format.js`, before anything that calls `WavrT`. A `data-i18n` attribute on
     a page with no runtime is decoration.
  2. nothing on it is a hardcoded non-English sentence. The source language is
     English; a Portuguese sentence in the source is a string no catalogue can
     ever reach, in either direction.
  3. its visible strings are MARKED — `data-i18n` on markup, `data-i18n-attr`
     on the attributes a person reads, `WavrT(...)` around the sentences
     JavaScript composes.

Every helper that reads JavaScript comes from `test_localisation`, deliberately.
A second scanner for "what does `WavrT` receive" would disagree with the first
one the day somebody writes a ternary or a `+` across two lines, and the two
would then certify different products.

## What this file does NOT assert

That every string here has a Portuguese translation. That check exists once, in
`test_localisation.test_every_declared_string_has_a_translation`, and the right
fix is to widen the set of files `declared_keys()` reads rather than to grow a
second copy of it here — a completeness rule in two places is a completeness
rule that will disagree with itself.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from tests.test_localisation import (
    _first_argument,
    _js_string_literals,
    _keys_in,
)

ROOT = Path(__file__).resolve().parents[2]

# Every HTML page the Core hands to a browser that is NOT `index.html`.
#
# `/measure.html` is routed in `app.py` beside `/` and is token-exempt, so an
# unpaired phone on the LAN can read it; the three experiences are served from
# `/experiences/{name}/` behind the developer-mode switch. All four are static
# HTML a human reads, which is the only property that matters here.
PAGES = {
    "measure.html": ROOT / "frontend" / "measure.html",
    "spatial-web": ROOT / "experiences" / "spatial-web" / "index.html",
    "capability-aware": ROOT / "experiences" / "capability-aware" / "index.html",
    "anchor-demo": ROOT / "experiences" / "anchor-demo" / "index.html",
}

# In load order, and the order is the point: several call sites run `WavrT` while
# their own script is still executing, so a runtime that arrives second is a
# runtime that was undefined when it was first called.
RUNTIME_SCRIPTS = ("i18n.js", "locale-pt.js", "format.js")


def source(page: str) -> str:
    return PAGES[page].read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


def _strip_code(src: str) -> str:
    """Markup only: no `<script>`, no `<style>`, no comments.

    A `<style>` block is full of words nobody reads and a `<script>` block is
    checked separately, by a scanner that understands JavaScript strings.
    """
    src = _strip_comments(src)
    src = re.sub(r"<script\b[^>]*>.*?</script\s*>", " ", src, flags=re.S | re.I)
    src = re.sub(r"<style\b[^>]*>.*?</style\s*>", " ", src, flags=re.S | re.I)
    return src


def scripts(page: str) -> str:
    """Every inline and module `<script>` body on the page, concatenated."""
    bodies = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", _strip_comments(source(page)),
                        flags=re.S | re.I)
    return "\n".join(bodies)


# -- 1. the runtime is actually there ------------------------------------------

@pytest.mark.parametrize("page", sorted(PAGES))
def test_each_served_page_loads_the_translation_runtime(page):
    """`data-i18n` with no runtime behind it is an attribute, not a translation."""
    src = source(page)
    found = re.findall(r'<script src="[^"]*?/?js/([a-z0-9.-]+)"', src)
    loaded = [name for name in found if name in RUNTIME_SCRIPTS]
    missing = [name for name in RUNTIME_SCRIPTS if name not in loaded]
    assert not missing, (
        f"{page} does not load {missing}. Every string on it renders in the "
        f"source language whatever the reader chose.")
    assert loaded == list(RUNTIME_SCRIPTS), (
        f"{page} loads the runtime in the order {loaded}. `i18n.js` installs "
        f"`WavrT`, `locale-pt.js` registers the catalogue into it, and "
        f"`format.js` shares the same resolved locale — in that order, or an "
        f"early caller gets an undefined global or an empty catalogue.")


@pytest.mark.parametrize("page", sorted(PAGES))
def test_the_runtime_is_loaded_before_anything_calls_it(page):
    """Classic scripts run as the parser reaches them. A `WavrT(` above the tag
    that defines it is a `ReferenceError`, and everything after it in that
    block never runs."""
    src = source(page)
    tag = src.find('js/i18n.js')
    call = _strip_comments(src).find("WavrT(")
    assert tag != -1
    if call != -1:
        assert tag < call, (
            f"{page} calls WavrT before it loads js/i18n.js")


# -- 2. the source language is English ------------------------------------------

# Letters that do not occur in English words. `ç` and the nasal vowels are the
# cheapest possible proof that a sentence was authored in Portuguese, and they
# cannot fire on an English string — a page may still say "m²" or "−1".
NON_ENGLISH_LETTERS = "ãõçáâêéíóôú"

# Words that settle the cases the letters miss: `Iniciar captura`, `Salvar`,
# `Andar`, `Medir de novo`. Each is a whole word with a boundary, so `and` does
# not fire `andar` and a URL cannot fire any of them.
PORTUGUESE_WORDS = (
    "não", "você", "cômodo", "cômodos", "aparelho", "celular", "câmera",
    "chão", "canto", "cantos", "perímetro", "área", "andar", "térreo",
    "subsolo", "salvar", "medir", "iniciar", "captura", "pareado",
    "pareamento", "conexão", "endereço", "tentar", "novamente", "aponte",
    "toque", "informe", "enviando", "concluir", "desfazer", "sair",
)


def _portuguese_in(text: str) -> list[str]:
    hits = []
    low = text.lower()
    for ch in NON_ENGLISH_LETTERS:
        if ch in low:
            hits.append(ch)
    for word in PORTUGUESE_WORDS:
        if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", low):
            hits.append(word)
    return hits


def visible_text(page: str) -> list[str]:
    """Every run of text a browser would paint, one entry per run."""
    out = []
    for chunk in re.split(r"<[^>]*>", _strip_code(source(page))):
        chunk = " ".join(html.unescape(chunk).split())
        if chunk and re.search(r"[^\W\d_]", chunk):
            out.append(chunk)
    return out


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_served_page_has_a_hardcoded_non_english_sentence_in_its_markup(page):
    """The catalogue key IS the English string.

    A Portuguese sentence written into the markup is therefore not "already
    translated" — it is a key that no catalogue contains, so it renders as
    itself for a Brazilian AND for everybody else. `measure.html` was an entire
    page of them, reached from an English button.
    """
    bad = [(t, _portuguese_in(t)) for t in visible_text(page) if _portuguese_in(t)]
    assert not bad, (
        f"{page} shows text that is not English:\n  "
        + "\n  ".join(f"{t!r} — {h}" for t, h in bad[:10]))


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_served_page_has_a_hardcoded_non_english_string_in_its_javascript(page):
    """Half of a page's words are built in JavaScript, so half of the check has
    to read JavaScript. Comments are skipped by the scanner, template literals
    are skipped whole."""
    bad = []
    for lit in _js_string_literals(scripts(page)):
        hits = _portuguese_in(lit)
        if hits:
            bad.append((lit, hits))
    assert not bad, (
        f"{page} composes text that is not English:\n  "
        + "\n  ".join(f"{t!r} — {h}" for t, h in bad[:10]))


# -- 3. the visible strings are marked -----------------------------------------

# Text that is the same in every language. The product name, and nothing else:
# an exemption list is how a marking rule quietly stops applying.
NEVER_TRANSLATED = {"Wavr"}

# Tags whose text is not read off the page as copy. `<title>` is deliberately
# not marked anywhere in this product — `test_localisation` has its own rule
# about that, because the shell overwrites it with the household's Space name.
UNREAD_TAGS = {"title", "script", "style"}

# The attributes a person actually reads, which is the same short list
# `test_copy_audit` audits. `aria-hidden`, `class` and `id` are not copy.
READ_ATTRS = ("placeholder", "aria-label", "title", "data-tip")

_ELEMENT = re.compile(
    r"<(?P<tag>[a-zA-Z0-9]+)(?P<attrs>[^>]*)>(?P<body>[^<>]*)</(?P=tag)\s*>", re.S)


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_visible_string_in_the_markup_is_marked_translatable(page):
    """`data-i18n`, read the way `WavrI18n.apply` reads it.

    Only elements whose whole body is text are considered, which is not a
    weakening: `apply` sets `textContent`, so an element that WRAPS other
    elements can never be marked without destroying them. That constraint is
    why this page's copy is flat — one sentence, one element — rather than a
    paragraph with `<b>` inside it.
    """
    unmarked = []
    for m in _ELEMENT.finditer(_strip_code(source(page))):
        if m.group("tag").lower() in UNREAD_TAGS:
            continue
        body = " ".join(html.unescape(m.group("body")).split())
        if not body or not re.search(r"[^\W\d_]", body):
            continue                                   # "·", "●", "…", ""
        if body in NEVER_TRANSLATED:
            continue
        if re.search(r"\bdata-i18n\b(?!-)", m.group("attrs")):
            continue
        unmarked.append(body)
    assert not unmarked, (
        f"{page} paints text nothing can translate:\n  "
        + "\n  ".join(repr(u) for u in unmarked[:15]))


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_attribute_a_person_reads_is_marked_translatable(page):
    """A placeholder is copy. `data-i18n` does not reach it; `data-i18n-attr`
    does, and the two are easy to confuse because the element looks marked."""
    unmarked = []
    for tag in re.finditer(r"<[a-zA-Z0-9]+[^>]*>", _strip_code(source(page))):
        attrs = tag.group(0)
        spec = re.search(r'data-i18n-attr="([^"]*)"', attrs)
        declared = {p.split(":", 1)[0].strip()
                    for p in (spec.group(1).split("|") if spec else [])}
        for name in READ_ATTRS:
            value = re.search(rf'\b{name}="([^"]*)"', attrs)
            if not value or not re.search(r"[^\W\d_]", value.group(1)):
                continue
            if name not in declared:
                unmarked.append(f"{name}={value.group(1)!r}")
    assert not unmarked, (
        f"{page} has copy in attributes that nothing can translate:\n  "
        + "\n  ".join(unmarked[:15]))


def _translated_literals(text: str) -> set[str]:
    """Every string literal that sits inside a `WavrT(...)` argument.

    Compared by VALUE rather than by position, so a sentence split across two
    source lines with `+` counts as translated whichever half is examined —
    `_keys_in` already knows how to join those, and this only needs to know
    which pieces it saw.
    """
    out: set[str] = set()
    for m in re.finditer(r"\bWavrT\(", text):
        arg = _first_argument(text, m.end() - 1)
        out |= set(_js_string_literals(arg))
        out |= _keys_in(arg)
    return out


# Fragments of source code that the shared literal scanner hands back as if
# they were strings. `_js_string_literals` skips comments and template literals
# but does not model REGEX literals, so the `"` inside `/[&<>"]/g` — the escaper
# every one of these pages defines, and that `test_reference_experiences`
# requires them to keep — opens a "string" that runs to the next quote and
# swallows the code in between. None of those blobs can be copy: a sentence a
# person reads has never contained `=>`, a semicolon, a `${` or a raw newline.
_IS_CODE = re.compile(r"=>|;|\$\{|\n")


def _reads_like_copy(lit: str) -> bool:
    """A literal that is prose rather than an identifier, a class or a header.

    Three words and twelve characters. Deliberately blunt: `"immersive-ar"`,
    `"Content-Type"`, `"local-floor"` and `"room.presence"` are single tokens,
    and a real sentence has never been two words long in this product.

    `{n}`-style slots are NOT excluded — an untranslated
    `"Could not save (code {code})."` is exactly what this has to catch.
    """
    s = lit.strip()
    if len(s) < 12 or not re.search(r"[A-Za-z]", s):
        return False
    if _IS_CODE.search(s):
        return False
    if re.search(r"https?://|^[\w.-]+/[\w.-]+$", s):
        return False
    return len(s.split()) >= 3


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_sentence_javascript_composes_goes_through_the_runtime(page):
    """The other half of the page's words.

    On `measure.html` this is most of them: every feedback line, every HUD
    status, every failure the Core can return. A page whose markup is marked and
    whose JavaScript is not is a page that changes language halfway through the
    first thing that goes wrong.
    """
    text = scripts(page)
    translated = _translated_literals(text)
    loose = sorted({lit for lit in _js_string_literals(text)
                    if _reads_like_copy(lit) and lit not in translated})
    assert not loose, (
        f"{page} builds sentences that never reach WavrT:\n  "
        + "\n  ".join(repr(x) for x in loose[:15]))


# -- the guard on the guard ------------------------------------------------------

def test_this_file_is_looking_at_pages_that_exist():
    """Every assertion above passes vacuously against a missing file, and a
    renamed page would take the whole check with it silently."""
    for name, path in PAGES.items():
        assert path.is_file(), f"{name} is not at {path}"
    assert len(PAGES) >= 4


def test_the_marking_check_can_actually_fail():
    """Proof that the scanner sees copy at all.

    Written because the two rules above are shaped so that a mistake in the
    regex — one that matches nothing — reads exactly like a page with no
    problems. This feeds it a page that IS wrong and requires a complaint.
    """
    page = _ELEMENT.finditer('<p class="lead">Aponte o celular para o chão.</p>')
    bodies = [m.group("body") for m in page]
    assert bodies == ["Aponte o celular para o chão."]
    assert _portuguese_in(bodies[0]), "the Portuguese detector matched nothing"
    assert not re.search(r"\bdata-i18n\b(?!-)", '<p class="lead">')
