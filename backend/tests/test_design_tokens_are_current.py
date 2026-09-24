"""design/tokens.json and the Android, Apple and desktop token files are
generated from the dashboard's own stylesheet. A changed colour in the
dashboard that is not regenerated fails here, instead of three clients
quietly showing an old one."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "gen_design_tokens", REPO / "scripts" / "gen_design_tokens.py")
gdt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gdt)


def test_generated_design_tokens_are_current():
    assert gdt.main(["--check"]) == 0


def test_an_unknown_state_is_never_painted_as_a_known_one():
    t = gdt.tokens()
    unknown = t["state"]["unknown"]
    assert unknown not in (t["state"]["unavailable"], t["state"]["healthy"])
    for render in (gdt.kotlin, gdt.swift, gdt.rust):
        assert "unknown" in render(t).lower()
