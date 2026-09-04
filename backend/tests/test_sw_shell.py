"""The offline shell must contain every script the page loads.

## The bug this catches, which only appears offline

`index.html` loads a handful of `js/*.js` blocks that were lifted out of it.
The service worker precaches a list of them for offline launches. Those are two
hand-maintained lists, and they drift.

When they drift, nothing fails in development, nothing fails in CI, and nothing
fails on a machine that can reach the Core. It fails on a kiosk panel that lost
its network — the one place nobody is watching and the one place the product
promises to keep working.

`js/format.js` made it worse than a missing feature: it loads before every
inline block and the whole shell calls it, so an offline launch without it is a
blank page rather than a degraded one.

## Why this is a test and not a build step

Because the fix is one line and the failure is invisible. A test that names both
lists and the missing entry costs nothing and turns "the panel is blank and I
don't know why" into a red line with the filename in it.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INDEX = FRONTEND / "index.html"
SW = FRONTEND / "sw.js"

# `<script src="js/…">` — the shell's own scripts. Vendored bundles and modules
# loaded on demand are deliberately not precached (see sw.js: the three.js
# bundle is ~750KB and the shell must stay light), so only `js/` counts here.
LOADED = re.compile(r'<script[^>]+src="(js/[a-z0-9_.-]+\.js)"')


def scripts_the_page_loads() -> set[str]:
    return set(LOADED.findall(INDEX.read_text(encoding="utf-8")))


def scripts_the_worker_caches() -> tuple[set[str], set[str]]:
    text = SW.read_text(encoding="utf-8")
    shell = set(re.findall(r'"\./(js/[a-z0-9_.-]+\.js)"', text))
    paths = set(re.findall(r'"/(js/[a-z0-9_.-]+\.js)"', text))
    return shell, paths


def test_every_shell_script_the_page_loads_is_precached():
    loaded = scripts_the_page_loads()
    shell, _ = scripts_the_worker_caches()
    missing = sorted(loaded - shell)
    assert not missing, (
        "index.html loads these and sw.js does not precache them: "
        + ", ".join(missing)
        + ". An offline launch would be missing them, and nothing else in this "
          "suite can see that.")


def test_the_workers_two_lists_agree_with_each_other():
    """`SHELL` is what gets fetched at install; `SHELL_PATHS` is what gets
    SERVED from the cache. A file in one and not the other is either downloaded
    and never used, or expected and never downloaded."""
    shell, paths = scripts_the_worker_caches()
    assert shell == paths, (
        f"only in SHELL: {sorted(shell - paths)}; "
        f"only in SHELL_PATHS: {sorted(paths - shell)}")


def test_the_worker_does_not_promise_a_script_that_does_not_exist():
    """A precache entry for a missing file makes `addAll` reject, and a service
    worker whose install fails leaves the page with no offline support at all —
    a typo in this list silently disables the feature it configures."""
    shell, _ = scripts_the_worker_caches()
    absent = sorted(s for s in shell if not (FRONTEND / s).is_file())
    assert not absent, f"precached but not on disk: {absent}"


def test_the_page_actually_loads_the_formatter_first():
    """`format.js` is called while the shell is parsing, so it has to be
    evaluated before any inline block. Load order is not something the other
    tests here can see, and getting it wrong breaks the page only for whoever
    opens it fastest."""
    text = INDEX.read_text(encoding="utf-8")
    fmt = text.find('src="js/format.js"')
    first_inline = re.search(r"<script>\s", text)
    assert fmt != -1, "format.js is no longer loaded"
    assert first_inline is None or fmt < first_inline.start(), (
        "format.js is loaded after an inline block that may already call it")
