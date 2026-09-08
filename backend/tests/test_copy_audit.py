"""The words a household reads must not be the words the wire uses.

## What this checks, and what it deliberately does not

Only VISIBLE copy: text nodes, and the four attributes a person actually reads
(`data-tip`, `aria-label`, `placeholder`, `title`). A grep over the whole file
finds sixty uses of "token" and every one of them is an identifier — measuring
that would produce a large number and no information.

## Why some jargon is allowed, in named places

"RTSP URL" is the correct term on the form where somebody pastes an RTSP URL.
"MQTT broker" is correct on the MQTT connector's own settings. Replacing those
with softer words would make an advanced screen harder to use in order to make a
beginner's screen no easier, because a beginner never opens it.

So the rule is not "no jargon". It is: **jargon only where the audience already
has the word**, and each exemption is listed here with the surface it belongs
to. Adding one is a decision somebody has to write down.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# Words that mean nothing to a household. Not a style list — every one of these
# names a thing the product can describe without it.
JARGON = ("provider", "manifest", "coordinate frame", "epoch", "homography",
          "websocket", "payload", "schema", "endpoint", "daemon", "middleware",
          "JSON", "UUID", "API")

# Allowed, with the surface that earns it. A phrase here must be specific enough
# that it cannot silently cover a new leak elsewhere.
EXEMPT = (
    "rtsp://user:password@ip:554/stream",   # the field's own example
    "RTSP URL",                             # its label, on the advanced form
    "ONVIF/RTSP cameras",                   # what the scan looks for
    "MQTT (shared broker)",                 # the connector's own name
    "MQTT",                                 # its settings, same screen
    "OpenAI-compatible endpoint",           # the field is for exactly that
    "flashed ESP32",                        # the "what is a node" explainer
    "Wavr Assistant (built-in)",            # names the built-in provider by name

    # -- found when this audit was widened to read the modules -----------------
    #
    # It had only ever read index.html, which WAS the whole product until
    # 783 KB of it moved into `frontend/js/`. Every one of these has been on
    # somebody's screen the whole time; the audit simply could not see them.
    # Each is allowed for the same reason the entries above are: the surface
    # has already told the person what they are configuring.

    # Connectors, and the Assistant's engine picker. A person on this screen is
    # choosing between OpenAI, Anthropic and a local server. "Provider" and
    # "API key" are the words those companies use on the page where the key is
    # copied FROM; softening them here would only make the key harder to find.
    "add a provider API key",
    "API key env var",
    "its API key",
    "Endpoint: {url}",
    "your configured endpoint",
    "narration not configured",

    # The config import screen, where somebody is picking a file they exported
    # from Wavr and can see named `.json` in their own file manager.
    "invalid JSON",
    "Not valid JSON",
    "not readable JSON",

    # Developer mode. It is off by default, it says so, and its whole audience
    # is people building against the API.
    "real-time (WebSocket)",
    "Manifest checker",

    # Diagnostics: the sentence exists to tell an operator WHICH environment
    # variable to set. Naming it is the actionable half.
    "WAVR_DIAG_ENDPOINT",
)


def visible_strings() -> list[str]:
    """Every string a person can read — the markup AND the modules.

    This read index.html only, which WAS the whole product until 783 KB of it
    moved into `frontend/js/`. Afterwards it audited the chrome and missed
    every sentence the modules write: connector descriptions, the device
    taxonomy, diagnosis explanations — which is precisely where wire
    vocabulary leaks, because that is the code sitting closest to the wire.

    From a module the readable strings are the ones handed to `WavrT(...)` —
    this product's translation call, which by construction wraps everything a
    person reads — and the literals assigned to `textContent`. A bare literal
    anywhere else is far more likely an id, a class name or a URL than copy.
    """
    from tests.frontend_source import MODULES, SHELL as MARKUP

    body = re.sub(r"<script[\s\S]*?</script>", " ", MARKUP, flags=re.I)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
    attrs = re.findall(
        r'(?:data-tip|aria-label|placeholder|title)="([^"]{4,})"', body)
    text = re.findall(r">([^<>{}]{4,})<", body)
    # `data-i18n-attr="aria-label:…"` carries copy the plain attribute scan
    # above cannot see, because that attribute is written at runtime.
    for pair in re.findall(r'data-i18n-attr="([^"]+)"', body):
        for part in pair.split("|"):
            _, _, after = part.partition(":")
            if after.strip():
                attrs.append(after.strip())

    from_js: list[str] = []
    call = re.compile(r'WavrT\(\s*"((?:[^"\\]|\\.){4,})"')
    assign = re.compile(r'textContent\s*=\s*"((?:[^"\\]|\\.){4,})"')
    for src in MODULES.values():
        from_js += [m.group(1) for m in call.finditer(src)]
        from_js += [m.group(1) for m in assign.finditer(src)]

    out = [html.unescape(t).strip() for t in attrs + text + from_js]
    return [t for t in out if t and not t.startswith(("http", "//", "/*"))]


def test_the_shell_does_not_show_a_household_the_wire_vocabulary():
    strings = visible_strings()
    assert len(strings) > 300, (
        f"only {len(strings)} visible strings found — the extractor is broken, "
        f"and a broken extractor passes this test forever")

    leaks = []
    for line in strings:
        if any(ok in line for ok in EXEMPT):
            continue
        for word in JARGON:
            if re.search(rf"\b{re.escape(word)}\b", line, re.I):
                leaks.append((word, line[:100]))
    assert not leaks, (
        "jargon in copy a household reads:\n  "
        + "\n  ".join(f"{w}: {t}" for w, t in leaks)
        + "\n\nEither say it in plain words, or add the exact phrase to EXEMPT "
          "with the surface that earns it.")


def test_every_exemption_still_corresponds_to_real_copy():
    """A stale exemption is a hole. If the phrase it was written for is gone,
    the entry now silently covers whatever else happens to contain it."""
    strings = visible_strings()
    unused = [e for e in EXEMPT if not any(e in s for s in strings)]
    assert not unused, f"exemptions that no longer match any copy: {unused}"
