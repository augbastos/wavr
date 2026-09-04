"""The reference experiences have to actually run.

A reference application that has drifted from the API it demonstrates is worse
than none: somebody copies it, and the thing they copy is wrong. So the MCP one
is executed here, and the web ones are checked against the SDK surface they call.

This file exists because of a real defect. The MCP script read
`disagreement["disagrees"]` where the tool returns `disagree` — so the single
most important line of that demonstration, the one where Wavr admits its sensors
contradict each other, silently never printed. Nothing failed; the output simply
looked complete.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_DEMO = ROOT / "experiences" / "mcp-agent" / "ask_wavr.py"
WEB = ROOT / "experiences"
SDK_JS = ROOT / "sdk" / "javascript" / "wavr.js"


def run_demo(*args) -> str:
    out = subprocess.run(
        [sys.executable, str(MCP_DEMO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


# -- The MCP experience runs ---------------------------------------------------

def test_the_mcp_demo_runs_and_names_its_scenario():
    assert "A headcount arrives" in run_demo("--scenario",
                                             "count_appears_and_vanishes")


def test_it_refuses_to_turn_presence_into_a_headcount():
    """The tempting wrong answer is "1 person". An agent that rounds presence to
    one will eventually tell somebody their house is empty when three people are
    in it."""
    out = run_demo("--scenario", "count_appears_and_vanishes")
    assert "I cannot say how many" in out
    assert not re.search(r"\b1 person\b", out)


def test_a_disagreement_is_actually_printed():
    """The regression this file was written for. It read the wrong key, so the
    line never appeared and the output still looked complete."""
    out = run_demo("--scenario", "sensors_disagree")
    assert "its sensors disagree" in out
    assert "says empty" in out and "says occupied" in out


def test_simulated_readings_are_labelled_in_the_output():
    """A demo that looked like a real house would be a demo that lies."""
    assert "[simulated]" in run_demo("--scenario", "occupancy_arrives")


def test_the_json_mode_returns_the_raw_tool_output():
    import json
    body = json.loads(run_demo("--scenario", "sensors_disagree", "--json"))
    assert "bedroom" in body
    assert body["bedroom"]["explanation"]["disagreement"]["disagree"] is True


def test_it_lists_its_scenarios():
    assert "occupancy_arrives" in run_demo("--list")


def test_an_unknown_scenario_fails_with_the_list_rather_than_a_traceback():
    out = subprocess.run(
        [sys.executable, str(MCP_DEMO), "--scenario", "nope"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=120)
    assert out.returncode != 0
    assert "occupancy_arrives" in (out.stderr + out.stdout)


def test_it_survives_a_console_that_cannot_print_an_arrow():
    """Wavr's explanations contain typographic characters and a Windows console
    defaults to cp1252. A reference script that crashes on the platform half its
    readers use is not a reference."""
    src = MCP_DEMO.read_text(encoding="utf-8")
    assert "reconfigure" in src and "errors=\"replace\"" in src


# -- The web experiences call an SDK that exists -------------------------------

WEB_PAGES = ["spatial-web", "capability-aware", "anchor-demo"]


@pytest.mark.parametrize("page", WEB_PAGES)
def test_each_web_experience_exists_and_imports_the_real_sdk(page):
    html = (WEB / page / "index.html").read_text(encoding="utf-8")
    assert "sdk/javascript/wavr.js" in html, "must use the real SDK, not a copy"


@pytest.mark.parametrize("page", WEB_PAGES)
def test_every_sdk_method_a_page_calls_actually_exists(page):
    """The drift this whole file guards against, checked statically because a
    browser test would need a browser and this needs to run everywhere."""
    html = (WEB / page / "index.html").read_text(encoding="utf-8")
    sdk = SDK_JS.read_text(encoding="utf-8")
    called = set(re.findall(r"\bwavr\.([a-zA-Z]\w*)\s*\(", html))
    for method in called:
        assert re.search(rf"^\s+(async\s+)?{method}\s*\(", sdk, re.M), (
            f"{page} calls wavr.{method}(), which the SDK does not define")


@pytest.mark.parametrize("page", WEB_PAGES)
def test_no_page_renders_an_unknown_count_as_zero(page):
    """The single most tempting lie in the product. A page that reads the
    occupancy PROPERTY must consult `occupancyKnown` before printing a number.

    Matched on `.occupancy` rather than the bare word: a page may perfectly well
    have a local variable about occupancy without ever touching the count, and a
    test that fired on the word would push somebody into renaming a variable to
    satisfy it."""
    html = (WEB / page / "index.html").read_text(encoding="utf-8")
    if re.search(r"\.occupancy\b", html):
        assert "occupancyKnown" in html, (
            f"{page} reads .occupancy without checking whether it is known")


def test_the_pages_escape_what_they_render():
    """Room names and anchor names are operator input, and these pages build
    HTML with template strings."""
    for page in WEB_PAGES:
        html = (WEB / page / "index.html").read_text(encoding="utf-8")
        assert "function esc(" in html or "const esc =" in html, page
