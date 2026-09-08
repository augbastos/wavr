"""Nothing wrong is not the same as nothing watching.

`compose_house_status` answers "is everything okay at home?" by ranking the
worst thing any layer reported. With no reasons it said `ok`, and `ok` renders
as **"Everything looks normal."** on the landing surface and lets the
`get_house_status` MCP tool tell an agent "everything's fine".

On an install with the network monitor off, no Watch and no occupancy log,
every layer is silent and no reason can exist. Nothing was wrong because
nothing was looking, and the product said the house was fine. That is the most
damaging sentence this codebase can produce, and it is the same mistake the
map's screen-reader summary once made when it announced "Empty home — no
presence detected" while every camera was offline.

A caller now declares which layers could report. No reasons and no live layer
is `unknown` — a refusal to answer, not a severity between the others.

## The compatibility rule, and why it is not laziness

Omitting `sources_checked` behaves exactly as before. A caller that never says
what it checked cannot be second-guessed by a pure function, and every existing
caller and test predates the idea. `app.py` knows, so `app.py` says.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.house_status import (LAYER_NETWORK, LAYER_PHYSICAL, STATUS_ALERT,
                               STATUS_OK, STATUS_UNKNOWN,
                               compose_house_status)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _alert(kind="rogue_dhcp", severity="alert"):
    return {"kind": kind, "severity": severity, "ts": NOW.isoformat()}


def test_no_reasons_and_no_live_layer_is_unknown_not_ok():
    out = compose_house_status(now=NOW, sources_checked=set())
    assert out["status"] == STATUS_UNKNOWN, (
        "a house nothing is watching was reported as fine")
    assert out["score"] == 0
    assert out["reasons"] == []


def test_no_reasons_with_a_live_layer_is_genuinely_ok():
    """The distinction has to cut both ways or it is just pessimism."""
    out = compose_house_status(now=NOW, sources_checked={LAYER_NETWORK})
    assert out["status"] == STATUS_OK


def test_a_real_alert_still_outranks_everything():
    """`unknown` must never mask a finding — it applies only to silence."""
    out = compose_house_status(network_alerts=[_alert()], now=NOW,
                               sources_checked=set())
    assert out["status"] == STATUS_ALERT, (
        "a live alert was swallowed by the not-watching branch")


def test_the_verdict_shows_which_layers_it_consulted():
    """This module's rule is that a verdict always shows its work."""
    out = compose_house_status(now=NOW,
                               sources_checked={LAYER_PHYSICAL, LAYER_NETWORK})
    assert out["checked"] == sorted([LAYER_NETWORK, LAYER_PHYSICAL])


def test_omitting_the_argument_keeps_the_old_shape_exactly():
    """Every existing caller predates this. None of them should change."""
    out = compose_house_status(now=NOW)
    assert out["status"] == STATUS_OK
    assert "checked" not in out, (
        "a key appeared for callers that did not ask for it — the old payload "
        "shape is pinned by other tests and by the MCP projection.")


def test_the_dashboard_has_words_for_the_new_state():
    """A backend that reports `unknown` to a frontend with no branch for it
    renders the fallback, which used to be "Worth a glance" — a verdict
    invented for a value the screen could not interpret."""
    from pathlib import Path
    from tests.frontend_source import ALL as shell
    assert 'status === "unknown"' in shell, (
        "the tile has no branch for `unknown`")
    assert "Nothing is being checked" in shell
    assert "does not recognise" in shell, (
        "an unrecognised status should say so rather than pick a verdict")


def test_the_app_declares_what_it_checked():
    """The pure function cannot help a caller that never tells it anything."""
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "wavr" / "app.py"
           ).read_text(encoding="utf-8")
    assert "sources_checked=checked" in app, (
        "app.py stopped declaring which layers could report, so a blind Core "
        "reports itself as fine again.")
