"""Where the frontend's source actually is, now that it is not one file.

Twenty-six test modules read `frontend/index.html` and search it for a string —
a branch, a copy line, a `data-i18n` key, a fetch call. That worked while the
shell was one 18,847-line file with 783 KB of inline `<script>` in it. It stops
working the moment the same code lives in `frontend/js/*.js`, and it stops
working in the worst possible way: the assertion is looking for something that
IS still shipped, in a file it no longer reads, so the test fails while the
product is correct. Twenty-four did exactly that in the run after the split.

The fix is not to point each test at a specific module. That would trade one
brittleness for a worse one — every future extraction would break the tests
again, and each would have to be re-pointed by hand at wherever the code moved
this time. What these tests mean by "the shell" is *what the browser loads*, so
that is what this exposes.

    SHELL    just index.html — for markup, styles, and the script tags
    MODULES  {name: text} — when a test really does care which file
    ALL      index.html plus every module the shell loads, concatenated

Use `ALL` for "does the product contain this behaviour". Use `SHELL` only when
the question is genuinely about the document — markup, the `<style>` block, the
order of the `<script>` tags. Use `MODULES` when the question is about
ownership, which is rarer than it looks.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INDEX = FRONTEND / "index.html"
JS = FRONTEND / "js"

SHELL: str = INDEX.read_text(encoding="utf-8")

# Only the modules the shell actually loads, and in load order. A stray file in
# `frontend/js/` that nothing loads is not part of the product, and letting it
# satisfy an assertion here would be a test passing on dead code.
LOADED: list[str] = re.findall(r'<script src="js/([a-z0-9.-]+)"', SHELL)

MODULES: dict[str, str] = {
    name: (JS / name).read_text(encoding="utf-8") for name in LOADED
}

# Joined with a newline between files so a search cannot match across a
# boundary and report a string that exists in neither module.
ALL: str = "\n".join([SHELL, *MODULES.values()])


def module_holding(needle: str) -> str | None:
    """Which module contains `needle`, for an error message worth reading."""
    for name, text in MODULES.items():
        if needle in text:
            return name
    return "index.html" if needle in SHELL else None
