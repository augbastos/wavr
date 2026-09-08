"""Does the install site, served from this laptop, ever leave this laptop?

`site/README.md` states the site is "zero external requests (no CDN, no
web fonts, no analytics)". That is a claim about behaviour, and the only
way to check a claim about behaviour is to open a real browser against a
real server and count what actually goes out -- not grep the HTML for
`<script src="http`, which would miss a `fetch()`, a redirect, or a font
loaded from CSS.

This drives the site through `first_user_static_server` (the same server
tomorrow's phones will use) bound to `127.0.0.1` on an isolated port, and
records every request Chromium makes while loading each public page.
Follows the house convention in `test_browser_ui.py`: skipped, not failed,
when Playwright's browser is not installed locally -- CI installs it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import first_user_static_server as server_mod  # noqa: E402

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

SITE_PUBLIC = ROOT / "site" / "public"

PAGES = ["index.html", "install.html", "compatibility.html", "nodes.html",
         "privacy.html", "docs.html"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def portal():
    """The real site tree, served by the real portal server. Downloads are
    irrelevant to this test (only page-load network behaviour is), so the
    download map is deliberately empty rather than pointed at the real
    100+ MB `_local/first-user-rc/`."""
    rc_dir_placeholder = SITE_PUBLIC.parent  # never read: manifest below has no products
    httpd = server_mod.build_server(
        "127.0.0.1", 0, static_root=SITE_PUBLIC, rc_dir=rc_dir_placeholder,
        manifest={"products": []})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("page", PAGES)
def test_page_makes_no_request_to_a_foreign_host(browser, portal, page):
    if not (SITE_PUBLIC / page).is_file():
        pytest.skip(f"{page} does not exist in site/public/ (owned by another pass)")

    served_host = urlsplit(portal).netloc
    requests: list[str] = []
    ctx = browser.new_context(service_workers="block")
    pg = ctx.new_page()
    pg.on("request", lambda req: requests.append(req.url))

    pg.goto(f"{portal}/{page}", wait_until="networkidle")
    ctx.close()

    foreign = [url for url in requests
               if urlsplit(url).scheme in ("http", "https")
               and urlsplit(url).netloc != served_host]
    assert foreign == [], (
        f"{page} made a request to a host other than the server that served "
        f"it ({served_host}): {foreign}")


def test_install_page_lists_no_dead_link_when_nothing_is_released(browser, portal):
    """`site/README.md`: "There is no dead-link state." -- an install page
    pointed at an empty manifest must not render a link a click 404s on."""
    if not (SITE_PUBLIC / "install.html").is_file():
        pytest.skip("install.html does not exist (owned by another pass)")

    ctx = browser.new_context(service_workers="block")
    pg = ctx.new_page()
    responses: dict[str, int] = {}
    pg.on("response", lambda resp: responses.__setitem__(resp.url, resp.status))
    pg.goto(f"{portal}/install.html", wait_until="networkidle")
    ctx.close()

    same_origin_failures = {
        url: status for url, status in responses.items()
        if urlsplit(url).netloc == urlsplit(portal).netloc and status >= 400
    }
    assert same_origin_failures == {}
