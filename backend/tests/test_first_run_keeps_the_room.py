"""First-run setup keeps the room the operator names.

The release notes say "A fresh install starts with your own floor plan instead
of a sample one, and setup keeps the first room you name." The first half was
true: the default map is empty, because a floor plan Wavr invented is a map of
rooms that do not exist. The second half was not. `POST /api/setup/create-space`
takes a `room`, `seed_room` writes it onto the empty plan, and both were built
and tested — and the wizard never asked, so the only caller that exercised the
path was a test fixture.

This walks the real wizard in a browser: type a room, finish setup, and read
the floor plan back off the API.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

pytest.importorskip("playwright.sync_api",
                    reason="browser cases run in the browser CI job")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOM = "the workshop"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def unset_core(tmp_path):
    """A Core with NO Space, so the first-run wizard is the whole screen."""
    port = _free_port()
    env = {
        **os.environ,
        "WAVR_DB": str(tmp_path / "wavr.db"),
        "WAVR_HOUSE_MAP": str(tmp_path / "house.json"),
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
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _rooms(base: str) -> list[str]:
    req = urllib.request.Request(base + "/api/house",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        house = json.loads(r.read())
    if isinstance(house.get("floors"), list):
        return [rm.get("name") for f in house["floors"]
                for rm in f.get("rooms", []) if rm.get("name")]
    return [rm.get("name") for rm in house.get("rooms", []) if rm.get("name")]


def test_the_wizard_asks_for_a_room_and_the_map_keeps_it(unset_core):
    base = unset_core
    assert _rooms(base) == [], (
        "a fresh install already has rooms, so this test cannot tell whether "
        "setup added one")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # `test_browser_ui.py`'s `page` fixture attaches this; this file builds
        # its own page because it needs a Core with NO Space.
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{base}/?cachebust={time.time()}")
            page.wait_for_selector("#setupWizard.show", timeout=30000)
            # Step 0 is the choice between creating a Space, joining one, and
            # (on a Core that already has devices) adopting what is there.
            page.wait_for_selector('#swBody [data-p="create"]', timeout=30000)
            page.click('#swBody [data-p="create"]')
            page.click("#swActions .primary")
            page.wait_for_selector("#swName", timeout=30000)

            page.fill("#swName", "Bench Space")
            page.fill("#swOwner", "Tester")
            # The field this test exists for.
            room = page.locator("#swRoom")
            assert room.count() == 1, (
                "the wizard does not ask which room this machine is in, so "
                '"setup keeps the first room you name" is a promise about a '
                "question nothing puts")
            room.fill(ROOM)

            # Walk to the end. The steps differ by hardware, so drive the
            # primary button until setup reports it is done.
            for _ in range(8):
                if page.locator("#setupWizard.show").count() == 0:
                    break
                btn = page.locator("#swActions .primary")
                if btn.count() == 0 or btn.is_disabled():
                    break
                btn.click()
                page.wait_for_timeout(1500)
            page.wait_for_timeout(2500)
            assert errors == [], errors
        finally:
            page.close()
            browser.close()

    assert ROOM in _rooms(base), (
        f"setup did not keep the room the operator named; the plan holds "
        f"{_rooms(base)!r}")


def test_an_empty_room_field_still_leaves_an_empty_plan(unset_core):
    """The field is OPTIONAL, and an empty map is the honest default: a floor
    plan Wavr invented is a map of rooms that do not exist. Skipping the
    question must not put anything on it."""
    base = unset_core
    req = urllib.request.Request(
        base + "/api/setup/create-space",
        data=json.dumps({"name": "Bench", "kind": "office",
                         "owner_name": "Tester", "room": ""}).encode(),
        headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20):
        pass
    assert _rooms(base) == [], _rooms(base)
