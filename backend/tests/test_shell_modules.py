"""index.html is a shell, and the order it loads modules in is a contract.

## What changed, and why a test has to hold it

`index.html` was 18,847 lines, of which 783,432 characters were inline
`<script>`. Six of those blocks were feature-sized — one was 481,591
characters, roughly 8,700 lines carrying twenty-five unrelated product areas —
and the practical effect was that every question about any feature started with
finding it in a file too large to open.

The blocks are now forty-four files under `frontend/js/`, cut at brace depth
zero and moved verbatim. The shell is markup, styles, and the list of scripts
that compose it.

## The two things splitting a classic script does not give you for free

**Order.** These are CLASSIC scripts, not ES modules, and several of them chain
onto `window.__wavr*` by wrapping whatever handler is already installed —
`__wavrRS`, `__wavrInventory`, `__wavrStatus`. The order of the `<script>` tags
IS the order those renderers run in. Nothing throws if somebody sorts the list
alphabetically or moves a tag down beside its friends; the page just renders in
a different order, occasionally wrongly, with nothing to point at. So the order
is written out below, and changing it means changing this file on purpose.

**Hoisting.** `function f(){}` hoists to the top of its own script. Inside one
giant block, a line that ran at parse time could call something declared four
thousand lines further down. Across two files it cannot. Every cut was placed
after its section's own bootstrap call for that reason, and the browser suite —
which visits every destination and asserts no page errors — is what proves it
held.

Identifier visibility, by contrast, IS free and worth stating so nobody
"fixes" it: top-level `var`/`function` become properties of `window`, and
top-level `let`/`const`/`class` go into the global lexical record that all
classic scripts share. A later module sees both. Adding `type="module"` to any
of these tags would break every cross-module reference at once, which is why
that has a test too.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INDEX = FRONTEND / "index.html"
JS = FRONTEND / "js"

_RAW = INDEX.read_text(encoding="utf-8")

# HTML comments are stripped before anything is counted, because one of them
# explains that the setup wizard has its "own <style>, own markup, own
# <script>" — and a `<script>` regex reads that as an opening tag and swallows
# everything up to the next real `</script>`. The first version of this file
# reported a 5,944-character inline block that does not exist. A browser never
# sees inside a comment; neither should a test that claims to measure what the
# browser loads.
SHELL = re.sub(r"<!--.*?-->", "", _RAW, flags=re.S)

# The load order, written out. A diff to this list is the review.
#
# Grouped by what each group is for, in the order the page needs them:
EXPECTED_ORDER = [
    # 1. Translation and formatting, before anything that renders a string.
    "i18n.js", "locale-pt.js", "format.js",

    # 1b. The client every feature module talks to the Core through. Depends on
    #     nothing but `location` and `fetch`, and everything below depends on
    #     it, so it sits as early as it can.
    "api.js",

    # 2. The data spine: reconnection UI, the DataProvider contract, mode
    #    selection and the capability manifest. Everything below reads `MODE`
    #    and the provider this installs.
    "core-connection.js", "shared.js",

    # 3. Dashboard surfaces, each one declaring its renderer and then calling
    #    it. This is the order they paint in.
    "house-indicator.js", "status-panel.js", "control-plane.js", "cameras.js",
    "core-lock.js", "narration.js", "network.js", "house-status.js",
    "transparency.js", "pairing.js", "identity.js", "known-presence.js",
    "peers.js", "nodes.js", "core-settings.js", "space-admin.js",
    "connectors.js", "assistant.js", "routines.js",

    # 4. The Space: the map, its geometry, the 3D view, and the renderer that
    #    consumes RoomState. `render.js` ends with the boot IIFE that replays
    #    history and starts the provider, so it must come after every module
    #    whose renderer that IIFE calls.
    "radar.js", "housemap.js", "house3d.js", "render.js",

    # 5. Shell chrome and the feature layers that CHAIN onto the hooks the
    #    modules above installed. Order matters most here: each of these wraps
    #    an existing `window.__wavr*` handler, so a tag moved above the module
    #    that installs the hook silently loses the wrap.
    "pwa.js", "shell-nav.js", "devices.js", "features.js", "tooltips.js",
    "whoshome.js", "new-devices.js", "trust.js", "developer.js", "runtime.js",
    "discoveries.js",

    # 6. Surfaces that sit over everything, and the language control last so it
    #    can enumerate every catalogue that registered above it.
    "whats-new.js", "core-panel.js", "wizard.js", "language.js",
]

TAG = re.compile(r'<script([^>]*)>', re.I)
SRC = re.compile(r'<script[^>]*\bsrc="js/([a-z0-9.-]+)"[^>]*></script>', re.I)
INLINE = re.compile(r'<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>', re.I | re.S)


def loaded() -> list[str]:
    return SRC.findall(SHELL)


# -- the shell is a shell ------------------------------------------------------

def test_no_feature_sized_block_is_still_inline():
    """Criterion 7: nothing stayed inline because moving it was inconvenient.

    The bar is deliberately low — a few hundred characters is a bootstrap, not
    a feature — and it is an upper bound on the WHOLE remaining inline surface
    as well as on any single block, so twenty small blocks cannot creep back in
    one at a time.
    """
    blocks = [(attrs.strip(), body) for attrs, body in INLINE.findall(SHELL)
              # An import map is data the browser needs before any module
              # loads. It cannot be an external file: the spec requires it
              # inline, ahead of the first import.
              if "importmap" not in attrs]
    biggest = max((len(b) for _a, b in blocks), default=0)
    total = sum(len(b) for _a, b in blocks)
    assert biggest <= 600, (
        f"an inline block of {biggest:,} characters is back in index.html. "
        f"Feature logic belongs in frontend/js/ — see this module's docstring "
        f"for where the cut points go.")
    assert total <= 1500, (
        f"{total:,} characters of inline script across {len(blocks)} blocks. "
        f"The shell composes; it does not implement.")


def test_the_shell_is_mostly_markup_now():
    """Criterion 1, measured rather than asserted. If this ever fails upward,
    something large went back in — and the number in the message says how
    much."""
    lines = SHELL.count("\n") + 1
    assert lines <= 6000, (
        f"index.html is {lines:,} lines. It was 18,847 before the split and "
        f"4,541 after; a jump means feature logic is being written into the "
        f"shell again instead of into a module.")


# -- order ---------------------------------------------------------------------

def test_the_module_load_order_is_exactly_this():
    """Criterion 5. Not "these modules load" — this ORDER.

    Several modules wrap a `window.__wavr*` handler installed by an earlier
    one. A reordering does not throw; it changes which renderers run, and the
    page looks nearly right. That is the failure mode this list exists for.
    """
    assert loaded() == EXPECTED_ORDER, (
        "the shell's script order changed.\n"
        f"  now:      {loaded()}\n"
        f"  expected: {EXPECTED_ORDER}\n"
        "If the change is deliberate, edit EXPECTED_ORDER and say in the "
        "commit which hook moved and why.")


def test_translation_loads_before_anything_that_renders_a_string():
    """`WavrT` is called at parse time by several modules. A catalogue that
    registers after its first reader leaves those strings in English on the
    first paint and correct after the next one — a bug that reproduces only on
    a cold load."""
    order = loaded()
    assert order[0] == "i18n.js", order[:3]
    assert order.index("locale-pt.js") < order.index("core-connection.js")
    assert order.index("format.js") < order.index("core-connection.js")


def test_the_language_control_loads_after_every_catalogue():
    """It fills its selector from `WavrI18n.available()`, which lists what has
    registered SO FAR. Loading it early gives a selector with one language in
    it and no error anywhere."""
    order = loaded()
    assert order[-1] == "language.js", order[-3:]


def test_the_boot_module_loads_after_every_renderer_it_calls():
    """`render.js` ends with the async IIFE that replays `provider.history()`
    and starts the stream. It calls renderers declared in earlier modules;
    across files there is no hoisting to rescue a tag that moved above them."""
    order = loaded()
    boot = order.index("render.js")
    for earlier in ("core-connection.js", "network.js", "status-panel.js",
                    "control-plane.js", "radar.js", "housemap.js"):
        assert order.index(earlier) < boot, (
            f"{earlier} must load before render.js — its boot IIFE calls into "
            f"it, and function hoisting does not cross a <script> boundary")


def test_nothing_is_an_es_module():
    """`type="module"` would give each file its own top-level scope, and every
    cross-module reference in the shell — `MODE`, `provider`, `confWord`, the
    `window.__wavr*` chain — would break at once. Classic is load-bearing."""
    for attrs in TAG.findall(SHELL):
        if 'src="js/' in attrs:
            assert "module" not in attrs, attrs


# -- no orphans, no duplicates -------------------------------------------------

def test_every_module_on_disk_is_loaded_exactly_once():
    """Criterion 6, from the other side: a file nobody loads is a file whose
    behaviour nobody can reason about, and the next person to change it will
    not find out it was dead."""
    order = loaded()
    on_disk = {p.name for p in JS.iterdir() if p.suffix == ".js"}
    assert len(order) == len(set(order)), (
        f"a module is loaded twice: "
        f"{sorted({n for n in order if order.count(n) > 1})}")
    assert set(order) == on_disk, (
        f"loaded but missing from disk: {sorted(set(order) - on_disk)}; "
        f"on disk but never loaded: {sorted(on_disk - set(order))}")


def test_no_module_is_empty_or_a_stub():
    """A cut that landed wrong produces a file that parses and does nothing.
    Nothing downstream notices."""
    thin = {p.name: p.stat().st_size for p in JS.glob("*.js")
            if p.stat().st_size < 400}
    assert not thin, f"suspiciously small modules: {thin}"


def _header(path: Path) -> str:
    """The file's leading comment block, however long it runs.

    A fixed byte window was the first attempt and it was wrong: `i18n.js` opens
    with a genuinely long explanation, its note landed past the cutoff, and the
    test reported a missing note that was there. Measuring "the first 2 KB"
    when the thing being measured is "the header" penalises the files that
    explain themselves best.
    """
    text = path.read_text(encoding="utf-8")
    if text.startswith("/*"):
        return text[:text.index("*/") + 2]
    out = []
    for line in text.splitlines():
        if not line.startswith("//") and line.strip():
            break
        out.append(line)
    return "\n".join(out)


def test_every_module_carries_the_note_about_why_order_matters():
    """The banner is not decoration. Somebody opening one of these files needs
    to know, before they move the tag, that the position is the contract."""
    missing = [p.name for p in sorted(JS.glob("*.js"))
               if not any(word in _header(p)
                          for word in ("position", "order", "ORDER"))]
    assert not missing, (
        f"these modules do not say why their load position matters: {missing}")


# -- the shared concerns stay shared -------------------------------------------

def test_no_module_reinvents_the_api_client():
    """Criterion 4, as a ratchet.

    `trust.js` and `wizard.js` each carried a byte-identical fourteen-line
    `api(path, opts)`: same header composition, same JSON encode, same "Wavr
    answered 404" fallback. Two implementations of one decision is the shape
    that eventually disagrees — somebody improves the error message in one of
    them, and the product says two different things about the same failure
    depending on which screen you were looking at.

    `js/api.js` owns it. This fails if a third copy appears.
    """
    from tests.frontend_source import MODULES
    culprits = []
    for name, text in MODULES.items():
        if name == "api.js":
            continue
        for m in re.finditer(r"function api\(path, opts\)\s*\{", text):
            body = text[m.end():m.end() + 700]
            if "WavrAPI" not in body.split("}")[0] + body[:120]:
                culprits.append(name)
    assert not culprits, (
        f"{culprits} define their own API client instead of calling WavrAPI. "
        f"See frontend/js/api.js and docs/FRONTEND-MODULES.md.")


def test_the_csrf_header_is_not_being_hand_written_again():
    """The `X-Wavr-Local` header is the backend's CSRF guard, and it used to be
    typed out at a hundred and ten call sites — a hundred and ten copies of one
    security decision, each of which a new call can forget.

    Ninety-four went through `WavrAPI` in one pass. The rest are calls this
    could not rewrite safely: a headers variable holding a bearer token, an
    init shape with something unusual in it, a request that deliberately sends
    no CSRF header at all. The number may fall freely. It may not rise: a new
    hand-written copy means somebody bypassed the client, and the next one to
    do it will forget the header rather than write it.
    """
    from tests.frontend_source import MODULES
    ceiling = 30
    hand = {name: text.count('"X-Wavr-Local"')
            for name, text in MODULES.items()
            if name != "api.js" and '"X-Wavr-Local"' in text}
    total = sum(hand.values())
    assert total <= ceiling, (
        f"{total} hand-written CSRF headers (ceiling {ceiling}), in "
        f"{dict(sorted(hand.items(), key=lambda kv: -kv[1])[:6])}. "
        f"Use WavrAPI.fetch — it composes the header, and a call site that "
        f"cannot forget it is the point.")


def test_the_module_map_names_every_module():
    """Criterion 6. "Where does this go?" has to have a written answer, and an
    answer that silently stops covering new modules is worse than none."""
    doc = (FRONTEND.parent / "docs" / "FRONTEND-MODULES.md").read_text(
        encoding="utf-8")
    missing = [n for n in loaded() if f"`{n}`" not in doc]
    assert not missing, (
        f"docs/FRONTEND-MODULES.md does not mention {missing}. A map that is "
        f"missing the newest module is the one somebody will be reading when "
        f"they ask where to put the next one.")
