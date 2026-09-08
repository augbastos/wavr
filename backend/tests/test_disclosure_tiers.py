"""What each person is shown, and the tier ladder that decides it.

Wavr discloses in four tiers, and until now that was an implementation detail
spread across three files rather than a property anything checked:

  1. **Ambient face** (`?core` / `window.WAVR_CORE`) — a clock, a wave, room
     names. No controls. What a wall panel shows somebody walking past.
  2. **Behind the PIN** — the full dashboard, once a person proves they are a
     person who knows the PIN.
  3. **Companion** — a paired phone, further split by whether its credential is
     `central`. A plain `user` companion is shown fewer controls, not shown
     controls that will 403.
  4. **Developer mode** — off by default, and the section explains itself when
     off rather than vanishing, because a rail item that disappears reads as a
     missing feature and one that explains itself reads as a switch.

## The fail-safe this pins

`fetchPinConfigured()` once read a 401 — which is what the Core's OWN
`WAVR_LOCAL_TOKEN` hardening returns to a page that does not send the token —
as `{pin_set: false}`, identically to an unreachable backend. Turning on a
hardening flag unlocked the kiosk. It fails safe now, and this is the
regression test that finding never had.

The unreachable-backend case USED to fail open on the same reasoning, and that
was only half right — see
`test_an_unreachable_core_keeps_a_configured_kiosk_LOCKED`. It now depends on
evidence: a Core that has confirmed a PIN stays locked through a blip, and one
that never has cannot lock anybody out.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# `ALL` is index.html plus every js/ module the shell loads. This file
# searches for behaviour, and behaviour moved out of the document when
# the shell was modularised; reading index.html alone would fail on code
# that is still shipped.
from tests.frontend_source import ALL as SHELL      # noqa: E402
from tests.frontend_source import MODULES           # noqa: E402
from tests.frontend_source import SHELL as DOCUMENT  # noqa: E402

# The translation catalogue is a dictionary keyed BY the English source string,
# so every user-facing sentence in the product appears in it verbatim whether
# or not anything still renders it. A test asking "does the product say this?"
# must never be allowed to answer itself out of the catalogue.
CATALOGUE = "locale-pt.js"


def _fn(name: str, src: str = SHELL, span: int = 1800) -> str:
    """The source from `name` onwards. `span` because a fixed window is a
    silent failure mode: adding a paragraph of reasoning above a line pushes
    it out of view and the assertion below reports the CODE as missing."""
    i = src.index(name)
    return src[i:i + span]


def _strip_comments(src: str) -> str:
    """`src` with its comments removed and its string literals left intact.

    Two tests in this file used to pass on a comment describing the behaviour
    instead of on the behaviour. To `in`, a sentence in a `//` line and the
    same sentence inside a `WavrT(...)` call are the same string — and the
    comment is the half that survives somebody deleting the code beneath it.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src[i:i + 4] == "<!--":
            j = src.find("-->", i)
            i = n if j < 0 else j + 3
        elif c in "\"'`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                if c != "`" and src[j] == "\n":
                    break            # unterminated: stop rather than eat on
                j += 1
            out.append(src[i:j])
            i = j
        elif c == "\\":
            # An escape outside a string is a regex literal's `\/`. Taking the
            # pair whole is what stops `/https:\/\//` reading as a comment.
            out.append(src[i:i + 2])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _function_body(header: str, src: str) -> str:
    """One function's braces, matched rather than guessed.

    `_fn` above takes a fixed span, which has a silent failure mode of its own:
    the function grows and the assertion reports missing code. Where the
    question is "does THIS function do X", match the braces. Expects `src` to
    have been through `_strip_comments` — a brace in a comment is still a brace
    to this.
    """
    i = src.index(header)
    start = src.index("{", i)
    depth, j, n = 0, start, len(src)
    while j < n:
        c = src[j]
        if c in "\"'`":
            j += 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise AssertionError(f"braces never close after {header!r}")


# What the browser actually RUNS: every module the shell loads except the
# translation catalogue, with the comments taken out. Anything asserted against
# this is asserted against shipped behaviour.
CODE: str = _strip_comments(
    "\n".join(text for name, text in MODULES.items() if name != CATALOGUE))


# -- tier 1/2: the kiosk face and its lock ------------------------------------

def test_the_ambient_face_runs_only_in_core_mode():
    body = _fn('CORE_MODE = new URLSearchParams')
    assert 'if(!CORE_MODE) return;' in body, (
        "the Core Panel block no longer exits for ordinary modes, so the "
        "ambient face can appear over the dashboard")


def test_an_unauthorised_pin_check_is_read_as_LOCKED():
    """The regression this file exists for. A 401 is the Core's own hardening
    saying "ask again once authorized" — never "there is no lock"."""
    body = _fn('function fetchPinConfigured()')
    assert 'if(r.status === 401) return {pin_set:true};' in body, (
        "a 401 from /api/core/pin/status is no longer read as locked. That is "
        "the exact shape that unlocked the kiosk the moment WAVR_LOCAL_TOKEN "
        "was turned on.")


def test_an_unreachable_core_keeps_a_configured_kiosk_LOCKED():
    """The open question, closed.

    A flat fail-open was the old behaviour, on the reasoning that a hiccup
    should not lock an owner out of their own panel. Half right. This is
    loopback: if the fetch fails the Core is down — and the dashboard behind
    the lock is a single-page app still rendering the household's last state to
    whoever walks up. Failing open there hands a passer-by real data, and
    failing closed costs nothing, because a panel whose Core is down has
    nothing live to show.

    So the answer depends on evidence: closed if this Core has ever confirmed a
    PIN, open only if it never has. A fresh kiosk still cannot lock anybody out
    over a blip; a locked one stays locked.
    """
    body = _fn('function fetchPinConfigured()', span=3000)
    assert 'return pinEverConfirmed;' in body, (
        "the unreachable branch went back to a flat answer. It must return "
        "what this Core last confirmed, not a constant.")
    assert 'pinEverConfirmed = true' in body, (
        "nothing records that a PIN was ever seen, so the branch above can "
        "only ever fail open")


def test_release_notes_never_unlock_the_panel():
    """A takeover layered over the lock is still over the lock."""
    body = _fn('function whatsNewTakeover()')
    assert 'Do NOT reveal the dashboard' in body, (
        "the What's New takeover no longer hands control back to the lock")


def test_authentication_resets_when_the_panel_goes_ambient():
    """A panel that stays authenticated after returning to its ambient face is
    a wall panel anybody can wake into the dashboard.

    This asserted the COMMENT on the declaration — `var authenticated = false;
    // ... reset by returnToAmbient()` — and ended in "check it still is",
    which is a note to a reader, not a test. Deleting the reset from
    `returnToAmbient()` left both the declaration and the comment in place, so
    the assertion passed over an unlocked kiosk. Assert the assignment inside
    the function that performs it, and that something still schedules that
    function.
    """
    body = _function_body("function returnToAmbient(", CODE)
    assert re.search(r"\bauthenticated\s*=\s*false\b", body), (
        "returnToAmbient() no longer clears the authenticated flag, so the "
        "panel drops back to its ambient face still authenticated: the next "
        "touch opens the dashboard with no PIN and no biometric.\n" + body)
    assert re.search(r"setTimeout\(\s*returnToAmbient\b", CODE), (
        "nothing schedules the return to ambient any more, so the reset above "
        "only happens if somebody taps the brand mark — a panel left on the "
        "dashboard stays on the dashboard, unlocked")


# -- tier 3: the companion split ----------------------------------------------

def test_the_companion_split_is_actually_used():
    """`companionIsCentral()` is what keeps a plain `user` companion from being
    offered controls its credential cannot use. A control offered and then
    403'd teaches somebody the product is broken."""
    uses = SHELL.count("companionIsCentral()")
    assert uses >= 10, (
        f"only {uses} places distinguish a central companion from a plain "
        f"user one; that split used to be applied in more of them")


def test_routines_refuses_rather_than_rendering_empty():
    """The pattern every gated surface here follows: hide the control, show a
    sentence saying why. An empty panel is indistinguishable from a broken
    one."""
    body = _fn('function renderRoutines()')
    assert 'companionIsCentral()' in body
    assert 'note' in body and 'hidden = false' in body, (
        "Routines no longer explains itself to somebody who cannot use it")


# -- tier 4: developer mode ---------------------------------------------------

def test_developer_mode_explains_itself_when_off():
    """A rail item that vanishes reads as a missing feature; one that explains
    itself reads as a switch.

    Read from `CODE` rather than from `ALL`, and this is the whole point of the
    test: the sentence "Developer mode is off" appears verbatim in the
    translation catalogue, because the catalogue is keyed by the English
    source, and again in the HTML comment above the section explaining that the
    section says it. The old assertion — `in SHELL.lower()`, over index.html
    plus every module — was satisfied by either of those with the renderer
    deleted. It could not fail at the thing it exists to catch.
    """
    assert 'id="gearSecDeveloper"' in DOCUMENT
    assert 'data-section="gearSecDeveloper"' in DOCUMENT, (
        "the Developer rail item is gone, so nothing can open the section")

    off = _function_body("function renderOff(", CODE)
    assert "Developer mode is off" in off, (
        "the Developer section no longer says why it is empty. An empty panel "
        "behind a rail item reads as a broken screen; the sentence is what "
        "makes it read as a switch.")

    entry = _function_body("async function renderStatus(", CODE)
    assert "renderOff(" in entry, (
        "nothing reaches renderOff any more: the explanation still exists in "
        "the source and no longer renders when the Core refuses")


def test_the_tiers_are_written_down_somewhere_a_person_will_find():
    """Four tiers spread over three files is how a tier quietly stops being
    enforced. This test is the index."""
    for marker in ('WAVR_CORE', 'fetchPinConfigured', 'companionIsCentral',
                   'gearSecDeveloper'):
        assert marker in SHELL, f"tier marker {marker} is gone"
