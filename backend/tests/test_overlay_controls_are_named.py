"""Every control the Settings overlay renders says what it is.

## The class, not the two instances

Two controls in this overlay reached a screen reader as nothing: the manifest
textarea in `js/developer.js` had no name at all, and the guided-walk room
field in `js/trust.js` had one only because Chromium quietly borrows the
`placeholder` when a field carries nothing better. Both were found by opening
the accessibility tree and reading it, which is the only way to find them —
neither is visible on the screen, and neither breaks anything a sighted person
would notice.

So this file does not assert on those two. It walks EVERY control the overlay
renders and asserts each one has a computed accessible name, which is the
property that was violated, and it does it from the accessibility tree rather
than from attributes — a control can be named by its text, by `aria-label`, by
`aria-labelledby`, by a wrapping `<label>` or by `title`, and only the name the
browser computes out of all of them is the one an assistive technology reads.

## Why `placeholder` is measured separately, and rejected

A placeholder-named field passes "has a name" in Chromium and fails everywhere
it matters: the accessible-name spec treats `placeholder` as a last-resort
fallback, `axe-core`'s `label` rule does not accept it, and the text it borrows
is by definition the text that vanishes the moment somebody types. Asking the
tree WHERE each name came from is still asking about the computed name — it is
the same computation, reporting its own sources — so both checks are one pass
over one tree.

## Driven through CDP, not `page.accessibility`

`page.accessibility.snapshot()` was removed from Playwright. The protocol
underneath it was not: `Accessibility.getPartialAXTree` is what the snapshot
called and what the DevTools accessibility pane shows, and it returns the name
together with the sources it was computed from, which the YAML aria-snapshot
does not.

The Core, the browser and the desktop-width page come from `test_browser_ui`,
copied rather than imported so that neither file can silently change the other's
harness. See that file's module docstring for why the viewport is a desktop one.
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

pytest.importorskip("playwright.sync_api",
                    reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]

# Everything in the overlay a keyboard can land on. `summary` is in the list
# because the erase panel's "this is starting over" disclosure is one, and a
# disclosure with no name is a triangle that announces nothing.
FOCUSABLE = ", ".join(
    "#gearOverlay " + s for s in
    ("a[href]", "button", "input", "select", "textarea", "summary",
     "[tabindex]:not([tabindex='-1'])"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A real Core, on a real port, in a throwaway database.

    The `test_browser_ui` fixture, copied. Developer mode is ON because the
    Developer section is half the controls this file is about, and a Core with
    it off renders "Developer mode is off" and nothing to measure.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("named") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
        "WAVR_DEVELOPER_MODE": "1",
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
            data=json.dumps({"name": "Named Test", "kind": "home",
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


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1100})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
    pg.script_errors = errors
    yield pg
    ctx.close()


def _open_settings_fully(page, base):
    """Open the overlay and wait until all three async panels have FINISHED.

    Developer, Trust and Privacy each clear their host and rebuild it from
    several requests. Waiting for "not empty" is satisfied by whichever answers
    first, and the read that follows then measures a panel that is still being
    appended to — the controls this file is looking for would simply not be
    there yet, and the test would pass by counting nothing.

    So each panel is waited on by the LAST thing it renders — matched
    case-insensitively, because a tile heading is `text-transform: uppercase`
    and `innerText` reports what is RENDERED. A wait for "Manifest checker"
    spelled the way the source spells it never comes true, and the failure is a
    45-second timeout that looks like a slow Core.
    """
    page.goto(f"{base}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.click("#gearNavBtn")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    page.wait_for_function(
        """() => {
             const done = (id, text) => {
               const el = document.getElementById(id);
               return !!el && el.innerText.toUpperCase().includes(text);
             };
             // The manifest checker is the last tile renderDeveloper appends;
             // the walk card is the last one renderTrust appends.
             if (!done('developerBody', 'MANIFEST CHECKER')) return false;
             if (!done('trustBody', 'CHECK A ROOM')) return false;
             // The delete tile is built last and ends in either a button (the
             // route answered) or a note (403 from anywhere but the Core).
             const host = document.getElementById('privacyDataBody');
             if (!host) return false;
             const tile = Array.from(host.querySelectorAll('.tile')).find(
               t => t.innerText.toUpperCase()
                     .includes('DELETE WHAT WAVR LEARNED'));
             if (!tile) return false;
             return !!tile.querySelector('button')
                 || tile.innerText.toUpperCase().includes('AT THE CORE');
           }""", timeout=45000)


def _named_controls(page):
    """Every rendered control in the overlay, with its computed name and where
    that name came from.

    Nodes the tree marks `ignored` are dropped: that is what a hidden control
    is, and the settings rail alone is eleven buttons that `display:none` at
    this width. A control nobody can reach cannot fail to announce itself.
    """
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("Accessibility.enable")
    root = cdp.send("DOM.getDocument", {"depth": 1})["root"]["nodeId"]
    ids = cdp.send("DOM.querySelectorAll",
                   {"nodeId": root, "selector": FOCUSABLE})["nodeIds"]

    out = []
    for node_id in ids:
        tree = cdp.send("Accessibility.getPartialAXTree",
                        {"nodeId": node_id, "fetchRelatives": False})
        nodes = tree.get("nodes") or []
        if not nodes or nodes[0].get("ignored"):
            continue
        ax = nodes[0]
        described = cdp.send("DOM.describeNode", {"nodeId": node_id})["node"]
        attrs = described.get("attributes") or []
        pairs = dict(zip(attrs[::2], attrs[1::2]))

        # The winning source is the first the computation did not supersede or
        # reject — the same order the browser walked to arrive at the name.
        source = None
        for candidate in (ax.get("name") or {}).get("sources", []):
            if candidate.get("superseded") or candidate.get("invalid"):
                continue
            if (candidate.get("value") or {}).get("value"):
                source = candidate.get("type")
                break

        out.append({
            "where": "<{}{}{}>".format(
                described.get("localName"),
                f' id="{pairs["id"]}"' if pairs.get("id") else "",
                f' class="{pairs["class"]}"' if pairs.get("class") else ""),
            "role": (ax.get("role") or {}).get("value"),
            "name": ((ax.get("name") or {}).get("value") or "").strip(),
            "source": source,
        })
    return out


# A floor, so that an overlay which failed to render cannot pass by having
# nothing to check. The three panels alone build well over this.
ENOUGH = 40


def test_every_control_in_the_settings_overlay_has_an_accessible_name(page, core):
    """"button" is not a name.

    A control with no accessible name is announced by its role alone. On a
    screen with thirty of them that is thirty identical announcements, and the
    person listening has no way to tell which one runs a scenario and which one
    deletes what Wavr learned.
    """
    _open_settings_fully(page, core)
    controls = _named_controls(page)
    assert len(controls) >= ENOUGH, (
        f"only {len(controls)} reachable controls were found in the overlay, "
        f"which means it did not finish rendering — this test measured nothing")

    nameless = [c for c in controls if not c["name"]]
    assert not nameless, (
        f"{len(nameless)} control(s) in the settings overlay reach a screen "
        f"reader as their bare role:\n  "
        + "\n  ".join(f'{c["where"]} announces only "{c["role"]}"'
                      for c in nameless))
    assert page.script_errors == [], page.script_errors


def test_no_control_is_named_only_by_its_placeholder(page, core):
    """A placeholder is a hint, and Chromium's fallback to it is a courtesy.

    It is the one naming route that both passes the check above and fails the
    person: `axe-core` rejects it, the accessible-name spec has it as a last
    resort, and the text itself disappears the moment somebody types into the
    field — so the sighted user loses the label at exactly the point the field
    stops being empty. Every other named field in this product carries an
    `aria-label` beside its placeholder; this is what keeps the next one honest.
    """
    _open_settings_fully(page, core)
    controls = _named_controls(page)
    assert len(controls) >= ENOUGH, (
        f"only {len(controls)} reachable controls were found in the overlay, "
        f"which means it did not finish rendering — this test measured nothing")

    borrowed = [c for c in controls if c["source"] == "placeholder"]
    assert not borrowed, (
        f"{len(borrowed)} control(s) are named only by their placeholder, "
        f"which is not a label:\n  "
        + "\n  ".join(f'{c["where"]} borrows "{c["name"]}"' for c in borrowed))
