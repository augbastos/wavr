"""A fragment named outside the shell must land somewhere inside it.

The desktop tray, an OS notification and a bookmark can all name a surface by
URL fragment — `#tab-inicio`, `#gearSecTrust`. Three files have to agree for
that to work, and no single test could see all three:

  * `desktop/src-tauri/src/main.rs` decides which fragment each tray item
    opens. Its Rust test checks the SHAPE (`#tab-…` or `#gearSec…`) and cannot
    read the frontend, so a fragment naming a destination that no longer exists
    passes it.
  * `frontend/js/runtime.js` routes the fragment, by clicking a tab button or a
    settings rail item of that name.
  * `frontend/index.html` is where those ids either exist or do not.

The failure is silent by construction. `goTo` returns `false` and nothing
renders an error: the person clicks "Needs attention" in the tray, the
dashboard opens on whatever tab was default, and it looks exactly like the app
ignoring them. That is the bug the comment in `runtime.js` says already
happened once, with `#tab-novos`.

It is also live risk rather than history: primary navigation just went from ten
destinations to four, and five screens moved behind Manage. Nothing about
renaming or demoting a tab tells the Rust file.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.frontend_source import SHELL

ROOT = Path(__file__).resolve().parents[2]
TRAY = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"


def _playwright_installed() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except (ImportError, ValueError):        # a broken install is no install
        return False


HAS_PLAYWRIGHT = _playwright_installed()

# The `page` and `core` fixtures come from the browser module, which knows how
# to start a Core and hand back a page that records script errors.
#
# Imported only when playwright is here, and NOT with a module-level
# `importorskip`. That is what this file used to do, and it took the two static
# cross-file checks below down with it: `importorskip` raises `Skipped` while
# the module is being imported, so pytest skips the whole file, comment
# promising otherwise or not. The checks that need nothing but two files on
# disk — exactly the ones a machine without a browser can still run, and the
# ones that catch a tray fragment naming a tab that no longer exists — ran
# nowhere. `pytest_plugins` is read off the module after import, so leaving the
# name undefined is enough to not load it; the browser cases carry their own
# skip mark instead, which pytest evaluates before it looks for a fixture.
if HAS_PLAYWRIGHT:
    pytest_plugins = ["tests.test_browser_ui"]

needs_browser = pytest.mark.skipif(
    not HAS_PLAYWRIGHT, reason="the browser cases need playwright")


def tray_fragments() -> list[str]:
    return re.findall(r'TrayAction::OpenAt\("([^"]+)"\)',
                      TRAY.read_text(encoding="utf-8"))


def test_the_tray_names_at_least_one_surface():
    """A guard on the guard: if the regex stops matching, every assertion below
    passes over an empty list and this file becomes decoration."""
    assert len(tray_fragments()) >= 2, tray_fragments()


def test_every_tray_fragment_exists_in_the_shell():
    for frag in tray_fragments():
        name = frag.lstrip("#")
        if name.startswith("gearSec"):
            found = (f'id="{name}"' in SHELL
                     and f'data-section="{name}"' in SHELL)
            assert found, (
                f'the tray opens "{frag}", and the settings overlay has no '
                f'section with that id AND a rail item pointing at it. '
                f'`goTo` needs both: it clicks the rail item, which reveals '
                f'the section.')
        else:
            tab = name if name.startswith("tab-") else "tab-" + name
            assert f'id="{tab}"' in SHELL, (
                f'the tray opens "{frag}" and the shell has no #{tab}. '
                f'`goTo` returns false and nothing tells the person why — the '
                f'dashboard just opens on the default tab. If the destination '
                f'was renamed or demoted, update main.rs too.')


@needs_browser
def test_a_demoted_destination_is_still_reachable_by_fragment(page, core):
    """The IA change is the reason this file exists now.

    Five destinations moved behind Manage, and their buttons live in a
    `hidden` sub-rail. `goTo` calls `.click()` on the element by id — hidden is
    not removed, so the handler still fires and `switchTab` reveals the level
    from the DESTINATION. That is the design, and it is exactly the kind of
    design that works until somebody swaps `hidden` for `display:none` on a
    parent, or filters the router's button list to visible ones. Prove it in a
    browser rather than reasoning about it.
    """
    page.goto(f"{core}/#tab-transparencia", wait_until="load")
    page.wait_for_selector('.tab-panel[data-tab="transparencia"].active',
                           timeout=15000)
    assert page.locator("#navManageSub").is_visible(), (
        "the panel opened but the Manage rail stayed shut, so the person has "
        "no way to see where they are or to reach its siblings")
    assert page.script_errors == [], page.script_errors


@needs_browser
def test_a_primary_destination_is_reachable_by_fragment(page, core):
    page.goto(f"{core}/#tab-rotinas", wait_until="load")
    page.wait_for_selector('.tab-panel[data-tab="rotinas"].active',
                           timeout=15000)
    assert page.script_errors == [], page.script_errors


@needs_browser
def test_a_fragment_naming_nothing_does_not_break_the_page(page, core):
    """An old bookmark to a tab that no longer exists must leave the person on
    a working dashboard, not a blank one."""
    page.goto(f"{core}/#tab-novos", wait_until="load")
    page.wait_for_selector(".tab-panel.active", timeout=15000)
    assert page.script_errors == [], page.script_errors

