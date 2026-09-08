"""The app cold-launches with the network cut. Measured in a browser.

`test_sw_shell.py` compares two lists and `test_shell_modules.py` pins the load
order, and both are worth having — but neither one launches anything. The shell
went from eleven precached scripts to forty-four in one pass, and the failure
mode of getting that wrong is specific and quiet: `Cache.addAll` is
all-or-nothing, so ONE name missing from the list means nothing is cached at
all, the lists still look plausible to a reader, every test that talks to a
running Core still passes, and the panel on the wall goes blank the first time
the router reboots.

So this launches it. Load once with the network up, wait for the worker to take
control, cut the network at the browser, reload, and require the dashboard to
be there — not a cached HTML skeleton, but the actual product: navigation that
responds, a global that only exists if a module executed, and no script errors.

Kept out of `test_browser_ui.py` deliberately. That module's `page` fixture is
shared by every UI test; a context that has installed a service worker and been
switched offline is not a neutral starting point for the next one.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed").sync_playwright


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A real Core over loopback. Same shape as `test_browser_ui.core`.

    Loopback counts as a secure context, so the browser will register a service
    worker against it — which is the entire point of this module.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("offline") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
        "WAVR_LOCAL_TOKEN": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("the Core exited while starting")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("the Core did not start in time")

        import urllib.request
        req = urllib.request.Request(
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Offline Test", "kind": "home",
                             "owner_name": "Tester", "room": "sala"}).encode(),
            headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20):
            pass
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:      # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        yield b
        b.close()


def _install_worker(page, base):
    """Load the page and wait until a worker is controlling it.

    `navigator.serviceWorker.ready` resolves on ACTIVATION, which is earlier
    than control: the first load of a page is not controlled unless the worker
    claims it, and `sw.js` does claim — but the claim races the promise. Waiting
    on `controller` instead removes the race, and a reload is the documented
    fallback if the claim has not landed.
    """
    page.goto(base, wait_until="load")
    page.wait_for_function(
        "() => navigator.serviceWorker && navigator.serviceWorker.ready",
        timeout=30000)
    page.evaluate("() => navigator.serviceWorker.ready")
    try:
        page.wait_for_function(
            "() => !!navigator.serviceWorker.controller", timeout=10000)
    except Exception:                                  # noqa: BLE001
        page.reload(wait_until="load")
        page.wait_for_function(
            "() => !!navigator.serviceWorker.controller", timeout=15000)


def test_the_shell_cold_launches_with_the_network_cut(browser, core):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1100})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
    failed: list[str] = []
    page.on("requestfailed",
            lambda r: failed.append(f"{r.url} {r.failure}")
            if "/js/" in r.url else None)
    try:
        _install_worker(page, core)
        cached = page.evaluate(
            "async () => { const ks = await caches.keys();"
            " const k = ks.find(k => k.startsWith('wavr-shell-'));"
            " if (!k) return null;"
            " const c = await caches.open(k);"
            " return (await c.keys()).map(r => new URL(r.url).pathname); }")
        assert cached, (
            "no wavr-shell cache exists after the worker activated. "
            "`Cache.addAll` is all-or-nothing: one name in SHELL that the "
            "backend does not serve rejects the whole install, and nothing is "
            "stored. Check every entry resolves.")

        ctx.set_offline(True)
        errors.clear()
        failed.clear()
        page.reload(wait_until="load")

        # It is not enough that HTML came back. Require evidence that modules
        # executed: a global only a module defines, and navigation that works.
        assert page.evaluate("() => typeof window.WavrT === 'function'"), (
            "the page loaded offline but js/i18n.js did not run")
        assert page.evaluate("() => typeof window.switchTab === 'function'"), (
            "the page loaded offline but js/shell-nav.js did not run")
        page.wait_for_selector("#tab-inicio", timeout=15000)
        page.click("#tab-rotinas")
        page.wait_for_selector('.tab-panel[data-tab="rotinas"].active',
                               timeout=15000)

        assert not failed, (
            f"modules failed to load offline: {failed[:8]}. Each of these is "
            f"in index.html and missing from the service worker's precache.")
        assert errors == [], errors
    finally:
        ctx.close()


def test_every_module_is_actually_in_the_offline_cache(browser, core):
    """The list comparison in `test_sw_shell.py` proves the two FILES agree.
    This proves the browser stored what they name — the step in between, where
    a 404 or a MIME refusal turns an agreed list into an empty cache."""
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        _install_worker(page, core)
        stored = set(page.evaluate(
            "async () => { const ks = await caches.keys();"
            " const k = ks.find(k => k.startsWith('wavr-shell-'));"
            " const c = await caches.open(k);"
            " return (await c.keys()).map(r => new URL(r.url).pathname); }"))
        import re
        shell = (BACKEND.parent / "frontend" / "index.html").read_text(
            encoding="utf-8")
        wanted = {f"/js/{n}" for n in
                  re.findall(r'<script src="js/([a-z0-9.-]+)"', shell)}
        assert wanted <= stored, (
            f"the page loads these and the browser did not cache them: "
            f"{sorted(wanted - stored)}")
    finally:
        ctx.close()
