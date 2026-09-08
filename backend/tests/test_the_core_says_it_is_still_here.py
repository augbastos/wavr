"""A Core must keep saying it is here, or its own screen calls it absent.

Found by the first user, scrolling through Settings on a machine that was at
that moment serving him the page he was reading:

    The current primary Core hasn't checked in recently.

`CoreRegistry` decides that from `last_seen_ts` against `LEASE_SECONDS`, which
is 120. `CoreRegistry.heartbeat()` exists to refresh it, is correct, and had
**no callers anywhere** — not in the backend, not in the tests, not in the
frontend. `last_seen_ts` was written once, at registration, and never again.

So two minutes after any start, every Wavr in the world declared its own primary
Core absent and went on saying so forever.

Nothing crashed. The check worked, the lease worked, the warning rendered
correctly. What was missing was the producer, and the shape of that failure is
the reason this file exists: a guarantee is not held by the code that checks it,
it is held by the code that feeds it, and the checking half is the half that is
easy to write and easy to believe.

## Why the test is about the CALL and not about the loop

Asserting "a background task exists" would pass on a loop that sleeps forever,
or one whose interval is longer than the lease. So the tests below assert the
two things that actually decide whether the panel tells the truth: that the
refresh happens at all, and that it happens often enough to matter.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from wavr.core_registry import LEASE_SECONDS, CoreRegistry

APP = Path(__file__).resolve().parents[1] / "wavr" / "app.py"


def test_something_actually_calls_heartbeat():
    """The regression itself: a method with no caller.

    Read out of the source rather than exercised through a running app, because
    the failure was *absence* — and an integration test that happens not to wait
    two minutes would pass just as happily with nothing calling it.
    """
    source = APP.read_text(encoding="utf-8", errors="replace")
    callers = re.findall(r"\.heartbeat\s*\(", source)
    assert callers, (
        "nothing in app.py calls CoreRegistry.heartbeat(). `last_seen_ts` is "
        "then written only at registration, every Core reports itself stale "
        f"{LEASE_SECONDS:.0f}s after start, and the Space screen says so about "
        "the machine the reader is looking at.")


def test_the_refresh_is_faster_than_the_lease():
    """A heartbeat slower than the lease is decoration.

    The interval is read from the source for the same reason as above: this must
    fail if somebody widens it past the lease, and no realistic test run is long
    enough to notice that by waiting.
    """
    source = APP.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"interval\s*=\s*max\(\s*([\d.]+)\s*,\s*LEASE_SECONDS\s*/\s*([\d.]+)\s*\)",
                  source)
    assert m, ("the heartbeat interval is no longer written as a fraction of "
               "LEASE_SECONDS; whatever replaced it has to be checked against "
               "the lease here, or the two can drift apart silently")
    floor, divisor = float(m.group(1)), float(m.group(2))
    interval = max(floor, LEASE_SECONDS / divisor)
    assert interval < LEASE_SECONDS / 2, (
        f"the heartbeat fires every {interval:.0f}s against a "
        f"{LEASE_SECONDS:.0f}s lease. One missed tick should be slack, not a "
        f"false 'this Core is gone'.")


def test_a_fresh_heartbeat_clears_the_stale_flag(tmp_path):
    """And the mechanism it feeds actually responds to being fed.

    Cheap, and it closes the other half of the loophole: the two tests above
    would both pass if `heartbeat()` were called on a Core id that does not
    exist, which is exactly what it returns False for.
    """
    reg = CoreRegistry(str(tmp_path / "wavr.db"))
    core = reg.register("core-0001", "space-0001", "Wavr", is_self=True)
    assert core is not None

    # Backdate the lease by hand: the registry's own clock is the thing under
    # test, so the test must not wait two real minutes to reach the same state.
    with reg._lock:                                    # noqa: SLF001 -- fixture surgery
        reg._conn.execute(
            "UPDATE cores SET last_seen_ts = ? WHERE core_id = ?",
            ("2000-01-01T00:00:00Z", "core-0001"))
        reg._conn.commit()
    assert reg.get("core-0001").stale(), "the backdated Core should read as stale"

    assert reg.heartbeat("core-0001") is True, "heartbeat() did not find the Core"
    assert not reg.get("core-0001").stale(), (
        "a fresh heartbeat did not clear the stale flag, so refreshing the "
        "lease does not do what the panel reads")

    assert reg.heartbeat("nobody") is False, (
        "heartbeat() invented a Core that was never enrolled")
