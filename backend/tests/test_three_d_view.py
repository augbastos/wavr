"""The 3D house view loads its library, from a module, in a real browser.

`house3d.js` reaches three.js with a DYNAMIC import — `import("three")` and
`import("three/addons/controls/OrbitControls.js")` — resolved by the import map
at the top of index.html against the self-hosted bundle under `/vendor/`. Four
things have to hold at once for that to work, and the shell's modularisation
touched three of them:

  * the import map must still be INLINE and ahead of the first import (the spec
    requires it, so it is the one inline block that could not be extracted);
  * `import()` must work from a CLASSIC script, which is the kind every module
    now is — this is true, and it is the sort of true that people "fix";
  * `/vendor/` must still be served, and the service worker must still leave it
    to a lazy cache-first fetch rather than trying to precache 750 KB;
  * the code doing the importing had to survive being moved out of a
    481 KB inline block into a file of its own.

Nothing in the suite exercised any of it. The 3D view is the default view, so
the failure would have been the landing screen's main panel staying empty, with
one line in a console nobody has open.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="this needs a real browser")
pytest_plugins = ["tests.test_browser_ui"]


def test_the_3d_view_loads_three_and_draws(page, core):
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url)
            if "/vendor/" in r.url else None)
    failed: list[str] = []
    page.on("requestfailed",
            lambda r: failed.append(f"{r.url} {r.failure}")
            if "/vendor/" in r.url else None)

    page.goto(core, wait_until="load")
    page.wait_for_selector("#view3d", timeout=15000)

    # The stored preference decides the initial view, so ask for 3D explicitly
    # rather than assuming which one a previous test left behind.
    page.click("#view3d")

    # Wait for the IMPORT first, not for a canvas.
    #
    # The canvas wait below matches `#radarWrap canvas` among others, and the
    # 2D map's canvas is already drawn and still in the DOM at this point — so
    # it can resolve on the wrong element, immediately, while `import("three")`
    # has not yet been issued. Under the full suite (several Cores and a
    # browser competing) that is exactly what happened, and the assertion then
    # reported "nothing under /vendor/ was fetched", which was true at that
    # instant and was not the defect. Waiting for the thing the assertion is
    # ABOUT makes the failure mean what it says.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any("three" in u for u in requests):
        page.wait_for_timeout(250)

    page.wait_for_function(
        "() => { const c = document.querySelector('#radarWrap canvas,"
        " #houseCanvas, canvas.house3d, #radar canvas');"
        " return !!c && c.width > 0 && c.height > 0; }",
        timeout=30000)

    assert any("three" in u for u in requests), (
        f"nothing under /vendor/ was fetched in 30s, so the dynamic import "
        f"never started. Requests seen: {requests[:5]}")
    assert not failed, (
        f"the three.js import was requested and refused: {failed}. Check the "
        f"import map is still inline and ahead of the modules, and that "
        f"/vendor/ is still served.")
    assert page.script_errors == [], page.script_errors


def test_the_import_map_is_still_inline_and_first(page, core):
    """It cannot be an external file — the spec requires an import map inline
    and ahead of the first import — so it is the one block the modularisation
    could not extract. `test_shell_modules.py` exempts it by name; this checks
    the browser agrees it is usable, rather than merely present."""
    page.goto(core, wait_until="load")
    resolved = page.evaluate(
        "async () => { try { const m = await import('three');"
        " return typeof m.Scene === 'function' ? 'ok' : 'no Scene'; }"
        " catch (e) { return 'FAILED: ' + e.message; } }")
    assert resolved == "ok", (
        f"a bare `import(\"three\")` does not resolve in the page: {resolved}. "
        f"The import map is what maps it to /vendor/, and a module that needs "
        f"it renders an empty panel with one console line.")
