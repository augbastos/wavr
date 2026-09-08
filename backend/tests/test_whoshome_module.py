"""The Who's home block moved out of the shell, and its POSITION is the thing.

Lifting 9,451 characters out of an 18,000-line file is the easy part. The part
that would break quietly is where the tag goes.

## Why the tag sits in the middle of the document

This module chains onto `window.__wavrRS` by wrapping whatever handler is
already installed, and calling it before doing its own work. Several blocks in
the shell do exactly that, so the sequence of `<script>` tags decides the order
those renderers run in. Its tag therefore replaces the inline block at the
position that block occupied — NOT down beside the other `js/*.js` tags, which
would look tidier and would silently reorder the chain.

Nothing would fail if that happened. A different renderer would simply run
first, and somebody would eventually notice a panel painting with data one tick
old. That is the class of bug this file exists to make loud.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "frontend" / "index.html"
MODULE = ROOT / "frontend" / "js" / "whoshome.js"
APP = Path(__file__).resolve().parents[1] / "wavr" / "app.py"


def test_the_module_exists_and_is_the_real_thing():
    assert MODULE.exists(), "js/whoshome.js is gone"
    src = MODULE.read_text(encoding="utf-8")
    assert "qcRoomsEl" in src, "this is not the Who's home code"
    assert "__wavrRS" in src, "it no longer chains onto the room-state hook"


def test_the_shell_no_longer_carries_the_code():
    src = SHELL.read_text(encoding="utf-8")
    inline = [m.group(1) for m in
              re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src,
                          re.S)]
    assert not any("qcRoomsEl" in b for b in inline), (
        "the code is back inline as well as in the module, so two copies of "
        "the same renderer now chain onto the same hook")
    assert 'src="js/whoshome.js"' in src, "nothing loads the module"


def test_the_tag_sits_where_the_block_did_not_with_the_others():
    """The whole point. Its tag must come BEFORE the cluster of module tags at
    the end of the document, because the blocks between them also wrap
    `__wavrRS` and the wrapping order is the render order."""
    src = SHELL.read_text(encoding="utf-8")
    mine = src.index('src="js/whoshome.js"')
    others = [src.index(f'src="js/{name}.js"')
              for name in ("trust", "developer", "runtime", "discoveries")]
    assert all(mine < o for o in others), (
        "whoshome.js moved down beside the other module tags. That reorders "
        "the __wavrRS chain relative to the blocks between the two positions.")
    # And there is still shell code after it, which is what makes the ordering
    # matter at all.
    after = src[mine:]
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", after), (
        "nothing follows it any more, so this constraint may be obsolete — "
        "re-read the module docstring before relaxing it.")


def test_the_core_serves_it():
    """Asked of the app, not of app.py's source text.

    This used to grep for the literal `"/js/whoshome.js"` in the allowlist and
    for a `FileResponse(_FRONTEND / "js" / "whoshome.js")` route. Both strings
    are gone: one parameterised route serves every module and the allowlist is
    a `/js/` prefix, because at forty-four modules the per-file version was a
    chore whose single failure mode is losing offline launch entirely. The file
    is still served and still reachable without a credential — so ask THAT,
    which is what the test meant, and which no later refactor of the routing
    can quietly invalidate.
    """
    from fastapi.testclient import TestClient
    from wavr.app import _is_static_shell, create_app

    assert _is_static_shell("/js/whoshome.js"), (
        "the path is not in the static shell allowlist, so an unpaired device "
        "loading the pairing screen gets a 403 for it")
    r = TestClient(create_app()).get("/js/whoshome.js")
    assert r.status_code == 200, (
        "no route serves the file, so the tag 404s and the tab renders empty")
    assert "javascript" in r.headers["content-type"]
    assert "__wavrRS" in r.text, "that is not this module"


def test_it_declares_what_it_borrows():
    """It reads five things from the shell's shared top-level scope. If that
    list is not written down, the next person to move this file has no way to
    know what breaks."""
    src = MODULE.read_text(encoding="utf-8")
    head = src[:src.index("*/")]
    for name in ("MODE", "MIDDOT", "roomOcc", "roomConf", "confWord"):
        assert name in head, (
            f"the header does not mention {name}, which this file reads from "
            f"the shell")
