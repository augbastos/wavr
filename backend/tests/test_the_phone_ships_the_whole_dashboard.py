"""The APK carries every file the page it bundles asks for.

## Why this was not caught by anything

The mobile shell is built by copying `frontend/` into `mobile/www/` and then
packing that. The copy list was written when the dashboard was one enormous
inline `<script>`: index.html, the manifest, the service worker, the icon,
`measure.html`, and `vendor/`. The page was later split into 44 classic scripts
under `js/`, in a pinned order `test_shell_modules.py` holds — and nothing added
`js/` to the copy list, because this build had never been run against a
dashboard that had them.

The result is not a crash. It is 44 requests that 404 and a page that paints:
chrome, tabs, tiles, headings, all visible, and nothing behind them — no
`WavrT`, no `MODE`, no tab switching. `app.py` has a long comment about this
exact shape arriving through a different door, when a token setting 401'd every
module and left "a photograph of a product, with no message saying why".

The anchor the shim is injected at had rotted the same way and worse. It looked
for the first `<script>` with no `type=` and no `src=` — the old inline app
script. On the modular page that regex still MATCHED: on the word `<script>`
inside an HTML comment 373kB in, where a note about the first-run wizard
mentions it having its "own <script>". The shim would have been injected into
commented-out markup. No error, a build that reports success, and an app with no
native bridge at all.

Both failures share a shape this repository keeps meeting: a build step whose
checking half was written against a page that no longer exists, reporting
success about a thing it can no longer see.

## What is checked

Not "the list contains js". That pins today's answer. What is pinned is the
property: every script and stylesheet the bundled page references exists in the
bundle. A future split into `js/panels/` or a new `assets/` folder fails here
rather than on somebody's phone.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest

from tests.mobile_tree import mobile_dir   # noqa: E402 -- shared lookup

_MOBILE = mobile_dir()
WWW_CANDIDATES = [
    _MOBILE / "www",
    Path(__file__).resolve().parents[2] / "mobile" / "www",
]


@pytest.fixture(scope="module")
def www() -> Path:
    for p in WWW_CANDIDATES:
        if (p / "index.html").is_file():
            return p
    pytest.skip("mobile/www is not built in this checkout (run sync-frontend)")


@pytest.fixture(scope="module")
def pagina(www: Path) -> str:
    return io.open(www / "index.html", encoding="utf-8", newline="").read()


def _locais(refs: list[str]) -> list[str]:
    return [r for r in refs
            if not r.startswith(("http://", "https://", "//", "data:"))]


def test_every_script_the_page_loads_is_in_the_bundle(www, pagina):
    refs = _locais(re.findall(r'<script[^>]*\bsrc="([^"]+)"', pagina))
    assert len(refs) >= 40, (
        f"only {len(refs)} scripts referenced — the bundled page is not the "
        f"modular dashboard, so this check is measuring the wrong thing")
    faltando = [r for r in refs
                if not (www / r.replace("/", os.sep)).exists()]
    assert not faltando, (
        f"{len(faltando)} scripts are referenced by the bundled page and are "
        f"not in the bundle: {faltando[:8]}. On a phone these are 404s, and a "
        f"404'd module does not stop the page — it paints the chrome and does "
        f"nothing.")


def test_every_stylesheet_the_page_loads_is_in_the_bundle(www, pagina):
    refs = _locais(re.findall(r'<link[^>]*\bhref="([^"]+)"[^>]*>', pagina))
    refs = [r for r in refs if r.endswith(".css")]
    faltando = [r for r in refs if not (www / r.replace("/", os.sep)).exists()]
    assert not faltando, f"stylesheets referenced but not bundled: {faltando}"


def test_the_shim_runs_before_the_first_app_module(www, pagina):
    """Order, not presence. The shim installs the native fetch/WebSocket
    routing; a module that runs before it talks to the wrong place."""
    lib = pagina.find('<script src="wavr-lib.js">')
    shim = pagina.find('<script src="wavr-mobile-shim.js">')
    primeiro = re.search(r'<script[^>]*\bsrc="js/[^"]+"', pagina)
    assert lib != -1, "wavr-lib.js is not in the page"
    assert shim != -1, "the native shim is not in the page"
    assert primeiro, "the page loads no app modules at all"
    assert lib < shim < primeiro.start(), (
        f"wrong order — lib@{lib}, shim@{shim}, first module@{primeiro.start()}. "
        f"The shim must set window.WAVR_MOBILE before any app code runs.")


def test_the_shim_tag_is_not_inside_a_comment(pagina):
    """How this went wrong once: the injector's anchor matched the word
    `<script>` inside a comment, and injected there — silently."""
    at = pagina.find('<script src="wavr-mobile-shim.js">')
    assert at != -1, "the shim tag is missing"
    antes = pagina[:at]
    assert antes.rfind("<!--") <= antes.rfind("-->"), (
        "the shim <script> tag sits inside an HTML comment, so it never runs "
        "and the app has no native bridge")


def test_every_file_the_shim_loads_at_runtime_is_in_the_bundle(www):
    """Not everything the app needs is in a `<script src>` tag.

    The shim fetches two vendored libraries lazily, by writing a `<script>` at
    the moment they are first needed — `qrcode.js` to DRAW a code, `jsqr.js` to
    READ one. Neither appears in index.html, so the check above cannot see them,
    and the build had no reason to notice when one stopped being copied.

    `jsqr.js` did stop. It lives in the mobile branch's own `frontend/vendor/`
    because nothing on a desktop reads a QR from a camera; pointing the build at
    the current dashboard's `vendor/` dropped it. The pairing scanner then could
    not load the thing whose only job is reading the QR it exists to read.

    What made it expensive: the scanner loads the decoder and opens the camera in
    one promise chain, so it reported "Couldn't open the camera. Check no other
    app is using it." The camera had never been asked for anything, and the
    person was sent to close other apps and try again.
    """
    shim = www / "wavr-mobile-shim.js"
    if not shim.is_file():
        pytest.skip("the shim is not in the bundle")
    fonte = io.open(shim, encoding="utf-8", newline="").read()
    fonte = re.sub(r"^\s*//.*$", "",
                   re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S), flags=re.M)
    # `s.src = "vendor/jsqr.js";` and friends.
    refs = sorted(set(re.findall(r"""\.src\s*=\s*["']([^"']+\.js)["']""", fonte)))
    assert refs, (
        "the shim no longer loads anything lazily — if that is deliberate, this "
        "check has nothing to do; if the pattern changed, update the regex")
    faltando = [r for r in refs if not (www / r.replace("/", os.sep)).exists()]
    assert not faltando, (
        f"the shim loads {faltando} at runtime and they are not in the bundle. "
        f"These fail as a 404 inside a promise, which every one of these call "
        f"sites reports as something else entirely.")


def test_the_bundled_dashboard_is_not_the_frozen_july_one(pagina):
    """A build pointed at the branch's own stale `frontend/` produces an app
    that is a different product from the dashboard. `data-i18n` is the cheapest
    marker: the modular page is full of it and the July one had none."""
    marcas = pagina.count("data-i18n")
    assert marcas > 200, (
        f"the bundled page carries {marcas} data-i18n attributes. The current "
        f"dashboard has hundreds; the July snapshot had none. This build is "
        f"pointed at a stale frontend — set WAVR_FRONTEND_DIR.")
