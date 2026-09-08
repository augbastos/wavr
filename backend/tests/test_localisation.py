"""The product is translated, and stays translated.

`format.js` localised dates, times and numbers, and said plainly what it was
not: "the strings in this product are still English, and pretending otherwise
by shipping a locale switch that only changes date separators would be worse
than honest monolingualism." This is the other half, and these are the checks
that keep the claim true.

## Why the key is the English string

There are no `space.title.heading` identifiers here. A key like that has to be
looked up before anybody can review a change, it drifts from the text it names,
and a missing one renders as a dotted path in front of a household. The source
sentence IS the key, so a missing translation falls back to correct English —
the worst failure this design can produce is a screen partly in the other
language, never a broken one.

That property is only worth having if something enforces it, which is the point
of `test_every_declared_string_has_a_translation` below: "we have Portuguese"
is exactly the kind of claim that stops being true one string at a time.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SHELL = FRONTEND / "index.html"
# The shell is not the only page the Core serves a person. `measure.html` is a
# whole screen reached from a button in Settings, and the three reference
# experiences are served from `/experiences/`. All four were invisible to every
# check in this file, which is how `measure.html` stayed an entirely
# hardcoded-Portuguese page indefinitely — and, once it was marked, how its
# hundred-odd new entries read as "dead" to the no-dead-entries rule.
SERVED_PAGES = [SHELL] + sorted(
    (FRONTEND.parent / "experiences").glob("*/index.html"))
if (FRONTEND / "measure.html").exists():
    SERVED_PAGES.insert(1, FRONTEND / "measure.html")
RUNTIME = FRONTEND / "js" / "i18n.js"
CONTROL = FRONTEND / "js" / "language.js"
CATALOGUES = {"pt": FRONTEND / "js" / "locale-pt.js"}


def _shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def _js_literal(lit: str) -> str:
    """A JS string literal as the ENGINE sees it, not as the file spells it.

    `WavrT(" \\u00b7 the whole house")` looks up a string containing a real
    middle dot; the file contains a backslash and four hex digits. A catalogue
    keyed on the spelling holds a translation nothing can ever find — which is
    exactly what four entries did until this decoded them.
    """
    def sub(m):
        e = m.group(1)
        if e.startswith("u"):
            return chr(int(e[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r",
                '"': '"', "'": "'", "\\": "\\"}.get(e, e)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", sub, lit)


_PIECE = re.compile(r'(?P<q>["\'])(?P<t>(?:(?!(?P=q))[^\\]|\\.)*)(?P=q)', re.S)


def _first_argument(text: str, open_paren: int) -> str:
    """The source of `WavrT(`'s FIRST argument, brackets and strings balanced.

    Stops at the top-level comma, so the `{n: 3}` in `WavrT("{n} rooms", {n: 3})`
    is not mistaken for part of the key.
    """
    depth, i, n, start = 0, open_paren, len(text), open_paren + 1
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
            depth -= 1
            if depth == 0:
                return text[start:i]
        elif c == "," and depth == 1:
            return text[start:i]
        i += 1
    return text[start:i]


def js_keys() -> set[str]:
    """Every string the JavaScript asks the runtime to translate.

    Half this product's text is built in JS, so a completeness check that only
    read the markup would certify a catalogue covering 62% of the words.

    ## Why the whole first argument, and not just a literal after the paren

    This matched `WavrT("literal"` and an optional chain of `+ "literal"`. Two
    shapes slipped past it, and both are ordinary JavaScript:

        WavrT(dir === "inbound" ? "Reads in" : "Sends out")
        WavrT(cond ? "On" : "Off")

    Every one of those strings IS looked up at runtime, none was declared, so
    none could be translated and no completeness check could see the gap. The
    argument is parsed with brackets and strings balanced now, and every
    literal inside it counts.

    Adjacent literals joined by `+` are ONE key, because that is what the
    engine receives; alternatives separated by `?:` are SEPARATE keys, because
    exactly one of them is. Distinguishing them is the only subtlety here, and
    the `+` between two literals is what tells them apart.

    A non-literal argument — `WavrT(d.description)` — declares nothing, by
    design: there is no source string in the frontend to translate. Those are
    admitted through `backend_strings()` instead.
    """
    keys: set[str] = set()
    for f in SERVED_PAGES + sorted(FRONTEND.glob("js/*.js")):
        if f.name in ("i18n.js", "locale-pt.js"):
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'\bWavrT\(', text):
            keys |= _keys_in(_first_argument(text, m.end() - 1))
    return {k for k in keys if k}


def _keys_in(arg: str) -> set[str]:
    """Every key a `WavrT` argument expression can look up.

    A key is a maximal run of `literal (+ literal)*`. Anything else between two
    literals ends the run: `"a " + name + " b"` contributes NOTHING, because
    neither half is a source string — a site like that should be using a
    `{slot}`, and pretending it declares "a  b" would demand a translation for
    a string nothing ever asks for.

    Walked character by character rather than split with a regex. The first
    version split the argument on `?` and `:` to separate the branches of a
    ternary, and split inside the string literals too — so
    `WavrT("… asks each one \\"is there a person in this picture?\\" and
    discards it.")` became two fragments, and the real sentence stopped being
    declared at all.
    """
    out: set[str] = set()
    for alt in _alternatives(arg):
        joined, clean = [], True
        i, n = 0, len(alt)
        want = True                 # a literal, then `+`, then a literal, …
        while i < n:
            c = alt[i]
            if c in " \t\r\n":
                i += 1
                continue
            if c in "\"'" and want:
                piece = _PIECE.match(alt, i)
                if not piece:
                    clean = False
                    break
                joined.append(_js_literal(piece.group("t")))
                want = False
                i = piece.end()
                continue
            if c == "+" and not want:
                want = True
                i += 1
                continue
            clean = False           # an identifier, a call, an operator
            break
        if clean and joined:
            out.add("".join(joined))
    return {k for k in out if k}


def _alternatives(arg: str):
    """An argument expression split into the values it could evaluate to.

    A ternary offers alternatives; a comparison inside its condition does not.
    Splitting at top-level `?` and `:` separates them, and the ALL-OR-NOTHING
    rule in `_keys_in` then discards any alternative that is not purely
    literals joined by `+`:

        dir === "inbound" ? "Reads in" : "Sends out"

    gives three alternatives, of which the first (`dir === "inbound"`) contains
    an identifier and an operator and contributes nothing — so `"inbound"`, a
    wire value, never becomes a translatable key. The same rule discards
    `"a " + name + " b"` entirely: neither half is a source string, and
    declaring `"a  b"` would demand a translation for a string nothing ever
    looks up. A site like that should be using a `{slot}`.
    """
    parts, depth, start, i, n = [], 0, 0, 0, len(arg)
    while i < n:
        c = arg[i]
        if c in "\"'`":
            q, i = c, i + 1
            while i < n:
                if arg[i] == "\\":
                    i += 2
                    continue
                if arg[i] == q:
                    break
                i += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c in "?:" and depth == 0:
            parts.append(arg[start:i])
            start = i + 1
        i += 1
    parts.append(arg[start:])
    return [p for p in parts if p.strip()]


def declared_keys() -> set[str]:
    """Every source string the product declares as translatable — markup AND
    JavaScript.

    Read the way the RUNTIME reads it: `data-i18n` with no value means "my own
    textContent is the key", and textContent is entity-decoded — so a catalogue
    keyed on `Devices &amp; sensors` would never match a lookup for
    `Devices & sensors`.
    """
    keys: set[str] = js_keys()
    for page in SERVED_PAGES:
        src = page.read_text(encoding="utf-8")
        for m in re.finditer(
                r'<(?P<tag>[a-zA-Z0-9]+)(?P<attrs>[^>]*\bdata-i18n\b(?!-)[^>]*)>'
                r'(?P<body>[^<>]{1,400})</(?P=tag)>', src):
            keys.add(" ".join(html.unescape(m.group("body")).split()))
        for m in re.finditer(r'data-i18n-attr="([^"]+)"', src):
            for pair in m.group(1).split("|"):
                at = pair.find(":")
                if at == -1:
                    continue
                text = pair[at + 1:].strip()
                if text:
                    keys.add(" ".join(html.unescape(text).split()))
    return {k for k in keys if k}


def catalogue(tag: str) -> dict[str, str]:
    """Evaluate a catalogue file the way a browser would.

    Parsing the object literal with a regex would quietly disagree with what
    the browser loads the first time somebody writes a string containing a
    brace. Node evaluates the real file against a stub runtime, which is the
    only reading that matters.
    """
    path = CATALOGUES[tag]
    stub = (
        "global.window = global;\n"
        "let captured = {};\n"
        "global.WavrI18n = { register: (t, table) => { captured = table; } };\n"
        + path.read_text(encoding="utf-8") + "\n"
        "process.stdout.write(JSON.stringify(captured));\n"
    )
    # A FILE, not `node -e`. The catalogue is ~59 KB and Windows caps a command
    # line at about 32 KB, so passing it as an argument fails with
    # "The filename or extension is too long" — an error that says nothing
    # about the real cause and would send the next person looking at paths.
    with tempfile.TemporaryDirectory() as d:
        runner = Path(d) / "read-catalogue.js"
        runner.write_text(stub, encoding="utf-8")
        out = subprocess.run([_node(), str(runner)], capture_output=True,
                             text=True, encoding="utf-8")
    assert out.returncode == 0, f"{path.name} did not evaluate:\n{out.stderr}"
    return json.loads(out.stdout or "{}")


def _node() -> str:
    from shutil import which
    node = which("node")
    if not node:
        pytest.skip("node is not on PATH; cannot evaluate the catalogue")
    return node


# -- the mechanism -------------------------------------------------------------

def test_the_runtime_falls_back_to_the_source_string():
    src = RUNTIME.read_text(encoding="utf-8")
    assert "missing[text] = true" in src, (
        "a lookup that misses is no longer recorded, so nothing can audit it")
    assert "return text;" in src, (
        "the fallback is gone — a missing key would render as something other "
        "than correct English")


def test_the_runtime_keeps_the_original_before_translating():
    """Without this, switching language twice translates a translation and
    every switch after the first is a miss."""
    src = RUNTIME.read_text(encoding="utf-8")
    assert "i18nSrc" in src, "the original text is no longer preserved"


def test_the_choice_is_persisted_and_shared_with_the_formatter():
    """One locale, or a person reads Portuguese sentences over British dates."""
    i18n = RUNTIME.read_text(encoding="utf-8")
    fmt = (FRONTEND / "js" / "format.js").read_text(encoding="utf-8")
    assert '"wavr.locale"' in i18n and '"wavr.locale"' in fmt, (
        "the two halves no longer read the same stored key")


def test_the_selector_offers_only_what_this_build_can_render():
    src = CONTROL.read_text(encoding="utf-8")
    assert "WavrI18n.available()" in src, (
        "the language list is hardcoded again, so it can offer a language the "
        "build cannot render")


def test_a_locale_change_tells_everything_to_repaint():
    """Half this product's text is built in JavaScript. Translating the DOM is
    only half the job."""
    assert 'wavr:locale' in RUNTIME.read_text(encoding="utf-8")
    assert 'wavr:locale' in CONTROL.read_text(encoding="utf-8")


# -- the catalogues ------------------------------------------------------------

def test_the_shell_declares_a_substantial_amount_of_text():
    """If the marking were undone, every completeness check below would pass
    against an empty set."""
    keys = declared_keys()
    assert len(keys) >= 600, (
        f"only {len(keys)} strings are marked translatable; the product had "
        f"more than six hundred across markup and JavaScript")
    assert len(js_keys()) >= 200, (
        f"only {len(js_keys())} come from JavaScript, and roughly 240 did — "
        f"the JS half of the check has stopped matching")


@pytest.mark.parametrize("tag", sorted(CATALOGUES))
def test_the_attention_inbox_is_translated(tag):
    """The check that was missing, and the reason a whole surface was English.

    `declared_keys()` reads the frontend: markup marked `data-i18n`, and string
    literals inside `WavrT(...)`. A sentence composed in PYTHON and translated
    in the browser appears in neither, so nothing above requires it to have a
    translation — `backend_strings()` only ALLOWS such an entry to exist, it
    never demands one.

    The Needs Attention inbox is composed entirely in Python. Every row of it
    rendered in English in Portuguese, indefinitely, and the one test that
    could see it walks the landing screen — so it noticed on the runs where
    such a row happened to be on screen, and passed on the rest.

    `attention.TEMPLATES` is that module's whole vocabulary, assembled from the
    constants the code actually uses. Requiring a translation for each asks
    about the product rather than about what was on screen at the time.
    """
    from wavr.attention import TEMPLATES as ATTENTION_TEMPLATES
    from wavr.runtime_status import TEMPLATES as RUNTIME_TEMPLATES

    table = catalogue(tag)
    missing = sorted({t for t in ATTENTION_TEMPLATES if t not in table}
                     | {t for t in RUNTIME_TEMPLATES if t not in table})
    assert not missing, (
        f"{len(missing)} sentences the attention inbox and the runtime chip "
        f"can show have no {tag} translation:\n  "
        + "\n  ".join(repr(m) for m in missing))


@pytest.mark.parametrize("tag", sorted(CATALOGUES))
def test_every_declared_string_has_a_translation(tag):
    keys, table = declared_keys(), catalogue(tag)
    missing = sorted(keys - set(table))
    assert not missing, (
        f"{len(missing)} strings have no {tag} translation, so those screens "
        f"are half in English:\n  "
        + "\n  ".join(repr(m) for m in missing[:25])
        + ("\n  …" if len(missing) > 25 else ""))


_BACKEND_STRINGS = None


def backend_strings():
    """Every string LITERAL the backend actually ships.

    Some human-facing sentences are composed in Python and sent over the API —
    the privacy screen's category descriptions, the provider catalogue's
    requirement notes, a settings field's label. The frontend renders them with
    `WavrT(d.description)`, which works precisely because the catalogue key IS
    the English source string: the backend keeps speaking English and the
    frontend translates what it receives.

    Those keys have no literal anywhere in the frontend, so `declared_keys`
    cannot see them and the rule below would forbid ever translating them.
    That is the wrong trade — it leaves every backend sentence permanently
    English while the product claims to be localised.

    Parsed with `ast`, not grepped, and DOCSTRINGS ARE REMOVED. A sentence that
    exists only in a comment or a docstring is not shipped to anybody's screen,
    and an entry justified by one is exactly the dead entry this guards
    against.
    """
    global _BACKEND_STRINGS
    if _BACKEND_STRINGS is None:
        import ast
        out = set()
        docs = set()
        root = Path(__file__).resolve().parents[1] / "wavr"
        for py in root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8",
                                              errors="replace"))
            except SyntaxError:                     # not ours to fix here
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    s = " ".join(node.value.split())
                    if s:
                        out.add(s)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc:
                        docs.add(" ".join(doc.split()))
        _BACKEND_STRINGS = out - docs
    return _BACKEND_STRINGS


_MODULE_STRINGS = None


def module_strings():
    """Every string literal in the frontend modules.

    The third source a catalogue entry can be justified by, after markup and
    `WavrT("literal")`. Some copy lives in a module-level DATA TABLE and reaches
    the runtime as `WavrT(TABLE[key])` — the egress and sensing item lists, the
    tier metadata, the health vocabulary, the router database's tips. The
    literal is real, shipped and on somebody's screen; it is simply not at the
    call site, so `js_keys` cannot see it and the no-dead-entries rule would
    forbid translating it.

    Two tables were converted to functions instead (`kinds()`, `connDesc()`),
    which is the better shape where it is cheap: the literals move inside
    `WavrT(...)`, and the table is rebuilt in the current language rather than
    frozen in whichever one was active at parse time. Rewriting every table in
    a 118 KB module is not cheap, and doing it badly is worse than admitting
    the literal here.

    Comments are stripped, so a sentence that survives only in a comment cannot
    keep a dead entry alive — the same rule `backend_strings` applies to
    docstrings.
    """
    global _MODULE_STRINGS
    if _MODULE_STRINGS is None:
        out = set()
        for f in sorted(FRONTEND.glob("js/*.js")):
            if f.name == "locale-pt.js":
                continue
            for s in _js_string_literals(f.read_text(encoding="utf-8")):
                s = " ".join(s.split())
                if s:
                    out.add(s)
        _MODULE_STRINGS = out
    return _MODULE_STRINGS


def _js_string_literals(src: str) -> list[str]:
    """Every `'…'`/`"…"` literal, with comments skipped — scanned, not regexed.

    Stripping comments with `re.sub(r"/\\*.*?\\*/", …)` first looked fine and
    was wrong: a `/*` inside a STRING (a URL, a glob, a regex source) starts a
    match that runs to the next `*/` anywhere in the file, deleting whatever is
    between. Half of `features.js` disappeared that way, which is why
    "DHCP fingerprint" — a plain literal on line 400 — was reported as not
    existing in the product.

    Template literals are skipped whole: their substitutions make them
    interpolation, not a source string.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "`":
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "`":
                    i += 1
                    break
                i += 1
        elif c in "\"'":
            m = _PIECE.match(src, i)
            if not m:
                i += 1
                continue
            out.append(_js_literal(m.group("t")))
            i = m.end()
        else:
            i += 1
    return out


def product_strings():
    """Everything the shipped product contains as a string literal."""
    return backend_strings() | module_strings()


@pytest.mark.parametrize("tag", sorted(CATALOGUES))
def test_the_catalogue_has_no_entries_the_product_never_says(tag):
    """Dead entries are how a catalogue grows a second, stale copy of a string
    that was reworded months ago.

    "The product" is the shell AND the backend. A sentence Python composes and
    the frontend renders through `WavrT` is as real as one written in markup,
    and admitting it here is the only way the privacy screen's category
    descriptions or the provider catalogue's notes are ever read in Portuguese.
    """
    keys, table = declared_keys(), catalogue(tag)
    extra = sorted(set(table) - keys - product_strings())
    assert not extra, (
        f"{len(extra)} {tag} entries match nothing in the shell and nothing "
        f"the backend ships:\n  "
        + "\n  ".join(repr(e) for e in extra[:15]))


def test_a_comment_or_a_docstring_cannot_justify_an_entry():
    """The rule above is only safe while `backend_strings` means SHIPPED.

    Parsed with `ast` a comment is not a string literal at all, and docstrings
    are subtracted explicitly. Proved rather than asserted, because replacing
    that parse with a grep would look like a simplification and would quietly
    let a paragraph of module documentation keep a dead entry alive.
    """
    strings = backend_strings()
    src = (Path(__file__).resolve().parents[1] / "wavr" / "app.py").read_text(
        encoding="utf-8")
    comment = next((ln.strip()[2:].strip() for ln in src.splitlines()
                    if ln.strip().startswith("# ") and len(ln.strip()) > 70),
                   None)
    assert comment, "no comment long enough to test with"
    assert " ".join(comment.split()) not in strings, (
        f"a comment reached the shipped-strings set: {comment[:70]!r}")

    # A module that definitely HAS a docstring. `app.py` does not, and the
    # first version of this line fell back to the placeholder `"x"` — which is
    # a one-character string that appears somewhere in a 40-file backend, so
    # the assertion failed while telling you a docstring had leaked. A test
    # whose failure message is about the wrong thing is worse than no test.
    import wavr.space_store as store
    doc = " ".join((store.__doc__ or "").split())
    assert doc, "space_store lost its module docstring; pick another module"
    assert doc not in strings, "a module docstring reached the shipped set"


@pytest.mark.parametrize("tag", sorted(CATALOGUES))
def test_placeholders_survive_translation(tag):
    """`{n}` on the left and nothing on the right renders a sentence with a
    number missing from it."""
    broken = []
    for src, out in catalogue(tag).items():
        want = sorted(re.findall(r"\{(\w+)\}", src))
        got = sorted(re.findall(r"\{(\w+)\}", out))
        if want != got:
            broken.append(f"{src!r} -> {out!r} ({want} vs {got})")
    assert not broken, "placeholders lost in translation:\n  " + "\n  ".join(broken)


@pytest.mark.parametrize("tag", sorted(CATALOGUES))
def test_nothing_is_translated_that_must_stay_canonical(tag):
    """Protocol names, product names and identifiers are the same word in
    every language. A catalogue that "translates" RTSP has invented one."""
    KEEP = ("RTSP", "ONVIF", "Wi-Fi", "MQTT", "DHCP", "mDNS", "SSDP", "Zigbee",
            "BLE", "CSV", "PIN", "QR", "GPU", "VRAM", "Wavr", "Home Assistant",
            "OpenAI")
    lost = []
    for src, out in catalogue(tag).items():
        for word in KEEP:
            if word in src and word not in out:
                lost.append(f"{word}: {src!r} -> {out!r}")
    assert not lost, "canonical terms dropped:\n  " + "\n  ".join(lost[:15])


@pytest.mark.parametrize("tag", ["pt"])
def test_the_portuguese_is_brazilian(tag):
    """The owner's rule, and a real distinction: a Brazilian reading European
    Portuguese notices immediately, and it reads as a product written for
    somebody else."""
    EUROPEAN = {
        "ecrã": "tela", "ficheiro": "arquivo", "telemóvel": "celular",
        "utilizador": "usuário", "rato": "mouse", "autocarro": "ônibus",
        "casa de banho": "banheiro", "telemóveis": "celulares",
        "ficheiros": "arquivos", "utilizadores": "usuários",
        "ecrãs": "telas", "gestor": "gerenciador", "chávena": "xícara",
    }
    hits = []
    for src, out in catalogue(tag).items():
        low = out.lower()
        for bad, good in EUROPEAN.items():
            if re.search(r"\b" + re.escape(bad) + r"\b", low):
                hits.append(f"{bad!r} (use {good!r}): {out!r}")
    assert not hits, ("European Portuguese found:\n  " + "\n  ".join(hits[:15]))


@pytest.mark.parametrize("tag", ["pt"])
def test_the_two_failure_words_are_not_swapped(tag):
    """`docs/VOCABULARY.md`: "Not responding" is about the CORE, "not
    reporting" is about a SENSOR. They are different failures with different
    errands, and a translation that merges them merges the errands."""
    table = catalogue(tag)
    for src, out in table.items():
        low_src, low_out = src.lower(), out.lower()
        if "not responding" in low_src:
            assert "sem reportar" not in low_out, (
                f"a Core failure translated with the SENSOR word: "
                f"{src!r} -> {out!r}")
        if "not reporting" in low_src:
            assert "sem resposta" not in low_out, (
                f"a sensor failure translated with the CORE word: "
                f"{src!r} -> {out!r}")


# -- the name of the global, which was got wrong first -------------------------

def test_the_translation_global_cannot_be_shadowed():
    """`t` was the first name, and it was the wrong one.

    This shell declares a local `t` in thirty-five places — loop variables,
    function parameters, `var t = tag`. Every one of them shadows a global of
    the same name, so `x.textContent = t("…")` inside any of those scopes
    throws `t is not a function` at runtime. The call site reads perfectly, so
    no grep finds it; a browser test did, on the first run after the
    conversion.

    A one-letter global is unusable in a file that shares one scope across
    eighteen thousand lines.
    """
    src = RUNTIME.read_text(encoding="utf-8")
    assert "window.WavrT" in src, "the translation global lost its safe name"
    assert not re.search(r"^\s*window\.t\s*=", src, re.M), (
        "`window.t` is back. Thirty-five local `t` declarations in the shell "
        "shadow it, and every call inside one of those scopes throws.")

    # Counted across every module, not just the document.
    #
    # The shell used to BE the code, so scraping `<script>` bodies out of
    # index.html found all thirty-five shadows. After modularisation it finds
    # none — not because they went away, but because they moved — and this
    # assertion would have reported "no local `t` is left" about a codebase
    # that still has thirty-five of them. Splitting a file does not make an
    # identifier collision safe: the modules are CLASSIC scripts sharing one
    # global scope, so a local `t` in any of them shadows the global exactly as
    # it did when they were one file.
    from tests.frontend_source import ALL
    shadows = len(re.findall(r"\b(?:var|let|const)\s+t\b\s*=", ALL))
    assert shadows > 0, (
        "no local `t` is left anywhere in the frontend. If that is genuinely "
        "true the rule above could be relaxed — but verify it rather than "
        "assuming, because the cost of being wrong is a runtime error no grep "
        "can see. Check that this is counting the modules and not just "
        "index.html before believing it.")


def test_no_call_site_uses_the_short_name():
    """Every generated call reads `WavrT(`. One that says `t(` is a runtime
    error waiting for the scope it happens to land in."""
    offenders = []
    for f in sorted(FRONTEND.glob("js/*.js")) + [SHELL]:
        if f.name in ("i18n.js", "locale-pt.js", "language.js", "format.js"):
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(
                r"(?:\.(?:textContent|innerText|placeholder|title)\s*=\s*|"
                r"setAttribute\([^,]+,\s*|"
                r"actionFeedback\([^,]+,\s*(?:true|false)\s*,\s*)t\(", text):
            offenders.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "these call the short name, which any local `t` shadows:\n  "
        + "\n  ".join(offenders[:20]))


# -- a household's own words never enter the catalogue -------------------------

def test_the_space_name_is_never_sent_through_the_translation_layer():
    """Three doors, all of them closed, and each one was found after the last.

    A name somebody gave their home is DATA. Looking it up in a translation
    catalogue does two wrong things at once: it can return a "translation" of a
    private name, and — because every miss is recorded — it writes that name
    into `WavrI18n.missing()`, which is an audit surface.

      1. `#brandSpace` was marked `data-i18n` while holding the name. Fixed by
         having the writer remove the mark.
      2. `<title>` was marked, and `showSpaceName` overwrites it with
         "<name> — Wavr". Fixed by not marking it.
      3. `WavrT(body.headline)` sent "Wavr — running · <name> · everything
         reporting" through the catalogue. Fixed by the Core emitting
         `headline_template` with a `{space}` slot.

    Each was a different mechanism reaching the same mistake, which is why this
    checks the SHAPE rather than the three instances.
    """
    from tests.frontend_source import MODULES, SHELL

    assert "<title data-i18n" not in SHELL, (
        "the document title is marked again, and runtime.js overwrites it with "
        "the Space's name")

    # Comments stripped first. The fix's own comment quotes the call it
    # replaced — `WavrT(body.headline)` — so searching the raw file found the
    # documentation and failed on it. A guard that cannot tell code from prose
    # fails on the explanation of why it exists.
    rt = MODULES["runtime.js"]
    code = re.sub(r"/\*.*?\*/", " ", rt, flags=re.S)
    code = re.sub(r"^\s*//.*$", " ", code, flags=re.M)
    assert "WavrT(body.headline)" not in code, (
        "the runtime headline is being translated whole again; it contains the "
        "Space name. Use `headline_template` and pass {space}.")
    assert "headline_template" in rt, (
        "runtime.js no longer prefers the templated headline")

    # The writer still un-marks the slot it writes a name into.
    assert 'slot.removeAttribute("data-i18n")' in rt, (
        "showSpaceName stopped clearing the mark, so the next repaint looks "
        "the household's Space name up in the catalogue")


def test_the_core_offers_a_headline_with_a_slot_where_the_name_goes():
    """The frontend half only works if the Core supplies the template. Both
    halves, or the fallback silently reintroduces the leak."""
    from wavr.runtime_status import assess
    body = assess(uptime_s=600, space_name="Casa de Teste").to_dict()
    assert "{space}" in body["headline_template"], body["headline_template"]
    assert "Casa de Teste" not in body["headline_template"]
    # And the plain one is untouched, because the tray and `wavr status` do not
    # translate and would render the literal `{space}`.
    assert "Casa de Teste" in body["headline"]


def test_no_surface_still_points_at_a_tab_named_system():
    """The primary nav's `#tab-sistema` was relabelled "Overview" when the ten
    top-level destinations collapsed to four (Space / Activity / Routines /
    Manage, with Devices/Network/Overview one level down inside Manage). Four
    sentences elsewhere on these same pages kept sending a reader to "the
    System tab" / "in System →" — a destination `switchTab()` cannot reach
    because nothing is named that any more.

    Checked over `declared_keys()` (markup `data-i18n` bodies + every
    `WavrT(...)` call in the JS), which is exactly the set a person can end up
    reading — not a raw-text grep, which would also flag the developer
    comments that still say "the System tab" as shorthand for the Overview
    section and are not read by anyone using the product.
    """
    keys = declared_keys()
    offenders = sorted(k for k in keys
                        if "System tab" in k or "in System →" in k)
    assert not offenders, (
        f"copy still sends a reader to a tab literally named 'System', which "
        f"the nav renamed to 'Overview': {offenders}")
