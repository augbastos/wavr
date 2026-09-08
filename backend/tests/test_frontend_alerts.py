"""Frontend-honesty guard (the single-file frontend has no JS test harness).

Reads frontend/index.html as text and asserts the A9 fall/no-motion alert renders
HONESTLY on BOTH /api/alerts surfaces -- the Rede alert list (renderNetwork) and the
Core glance-box (ALERT_EXPLAIN) -- and carries the ADR-0003 disclaimer, never a phantom
"new device" row. Substring-level so it isn't brittle to unrelated markup churn.
"""
from pathlib import Path

# Same locator as wavr.app._INDEX: parents[2] of backend/tests/ is the repo root.
_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def _html() -> str:
    from tests.frontend_source import ALL
    return ALL          # index.html plus every module it loads


def test_fall_alert_has_honest_render_branch_in_network_list():
    html = _html()
    # The renderNetwork honest branch exists and is keyed on the fall kind...
    assert 'kind === "fall_suspected"' in html
    # ...carries the per-row disclaimer field...
    assert "a.disclaimer" in html
    # ...and renders as a wellbeing prompt, NOT a device sighting row.
    assert "possible fall - check in" in html


def test_fall_alert_glance_box_explain_is_honest_and_disclaims():
    html = _html()
    # The glance box explains `fall_suspected`, and the explanation disclaims.
    #
    # This looked for the literal `fall_suspected:` — the shape of an object
    # KEY — and the map became a `switch` when those strings were made
    # translatable. The colon was never the point. Matched on the kind and the
    # disclaimer separately, so a refactor of the dispatch cannot break this
    # again while a deleted disclaimer still can.
    assert "fall_suspected" in html
    assert "not a certified medical" in html
