"""Native clients take their colours from the dashboard's stylesheet
(scripts/gen_tokens.py). The contract they rely on: every runtime state a person
can be shown has a `--state-<name>` token that resolves to a real value."""
import importlib.util
from pathlib import Path

import pytest

from wavr import runtime_status as rs

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_tokens", REPO / "scripts" / "gen_tokens.py")
gen_tokens = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_tokens)


def test_every_runtime_state_has_a_colour():
    toks = gen_tokens.tokens((REPO / "frontend" / "index.html").read_text(encoding="utf-8"))
    for state in (rs.STARTING, *rs.SEVERITY):
        value = toks.get(f"--state-{state}", "")
        assert value and "var(" not in value, f"no resolved token for state {state!r}"


def test_references_resolve_and_a_missing_one_is_an_error():
    assert gen_tokens.tokens(":root{--a:#fff;--b:var(--a);--c:var(--x, 1px);}") == {
        "--a": "#fff", "--b": "#fff", "--c": "1px"}
    with pytest.raises(ValueError):
        gen_tokens.tokens(":root{--b:var(--nope);}")
    with pytest.raises(ValueError):
        gen_tokens.tokens(":root{--a:var(--b);--b:var(--a);}")
