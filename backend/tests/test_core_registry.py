"""Core leadership: the epoch fence, and the split-brain the fence cannot fix."""
from datetime import datetime, timedelta, timezone

import pytest

from wavr.core_registry import (
    LEASE_SECONDS, STATUS_PRIMARY, STATUS_STANDBY, VERDICT_CONTESTED,
    VERDICT_HOLD, VERDICT_YIELD, CoreRegistry, CoreRegistryError,
)

SPACE = "space-abc"


@pytest.fixture
def reg():
    r = CoreRegistry(":memory:")
    yield r
    r.close()


def _at(reg, iso):
    """Register with a pinned clock so lease tests are deterministic."""
    reg._now = lambda: iso        # noqa: SLF001 -- the injected clock seam


# -- Registration ------------------------------------------------------------

def test_register_starts_as_standby_never_primary(reg):
    core = reg.register("core-laptop-01", SPACE, "Laptop")
    assert core.status == STATUS_STANDBY and core.epoch == 0


def test_reregister_refreshes_metadata_without_self_promoting(reg):
    reg.register("core-laptop-01", SPACE, "Laptop")
    reg.promote("core-laptop-01", 2)
    again = reg.register("core-laptop-01", SPACE, "Laptop Renamed", platform="windows")
    # A restart must never change who is in charge, in either direction.
    assert again.status == STATUS_PRIMARY and again.epoch == 2
    assert again.name == "Laptop Renamed" and again.platform == "windows"


def test_only_one_row_is_ever_is_self(reg):
    reg.register("core-aaaaaaaa", SPACE, "A", is_self=True)
    reg.register("core-bbbbbbbb", SPACE, "B", is_self=True)
    assert reg.self_core().core_id == "core-bbbbbbbb"
    assert sum(1 for c in reg.list_cores() if c.is_self) == 1


def test_short_core_id_is_rejected(reg):
    with pytest.raises(CoreRegistryError):
        reg.register("x", SPACE, "Tiny")


def test_heartbeat_does_not_enrol_an_unknown_core(reg):
    assert reg.heartbeat("core-stranger01") is False
    assert reg.list_cores() == []


# -- The epoch fence ---------------------------------------------------------

def test_promote_demotes_the_previous_primary(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.promote("core-aaaaaaaa", 2)
    reg.promote("core-bbbbbbbb", 3)
    assert reg.get("core-aaaaaaaa").status == STATUS_STANDBY
    assert reg.primary().core_id == "core-bbbbbbbb"


def test_replaying_an_old_epoch_is_refused(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.promote("core-aaaaaaaa", 5)
    with pytest.raises(CoreRegistryError, match="not newer"):
        reg.promote("core-bbbbbbbb", 5)
    with pytest.raises(CoreRegistryError, match="not newer"):
        reg.promote("core-bbbbbbbb", 4)
    assert reg.primary().core_id == "core-aaaaaaaa"


def test_promoting_an_unknown_core_fails(reg):
    with pytest.raises(CoreRegistryError):
        reg.promote("core-ghostghost", 2)


def test_the_primary_cannot_be_forgotten(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.promote("core-aaaaaaaa", 2)
    with pytest.raises(CoreRegistryError, match="promote another Core first"):
        reg.forget("core-aaaaaaaa")


def test_a_standby_can_be_forgotten(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.promote("core-aaaaaaaa", 2)
    assert reg.forget("core-bbbbbbbb") is True
    assert reg.forget("core-bbbbbbbb") is False


# -- observe_peer: the whole point of the design -----------------------------

def test_a_standby_has_nothing_to_defend(reg):
    reg.register("core-me000000", SPACE, "Me", is_self=True)
    assert reg.observe_peer("core-other000", 99, True) == VERDICT_HOLD


def test_we_yield_to_a_higher_epoch_immediately(reg):
    reg.register("core-me000000", SPACE, "Me", is_self=True)
    reg.promote("core-me000000", 3)
    assert reg.observe_peer("core-other000", 4, True) == VERDICT_YIELD
    assert reg.self_core().status == STATUS_STANDBY


def test_we_hold_against_a_stale_claimant(reg):
    reg.register("core-me000000", SPACE, "Me", is_self=True)
    reg.promote("core-me000000", 7)
    # A Core that slept through three promotions still resolves correctly.
    assert reg.observe_peer("core-other000", 4, True) == VERDICT_HOLD
    assert reg.self_core().status == STATUS_PRIMARY


def test_a_peer_that_claims_nothing_changes_nothing(reg):
    reg.register("core-me000000", SPACE, "Me", is_self=True)
    reg.promote("core-me000000", 3)
    assert reg.observe_peer("core-other000", 99, False) == VERDICT_HOLD
    assert reg.self_core().status == STATUS_PRIMARY


def test_equal_epoch_split_is_reported_and_broken_deterministically():
    # Two Cores, each believing it is primary at the SAME epoch. Both run the
    # identical comparison with no communication and MUST reach the same answer:
    # the lower core_id keeps it. Anything else and the Space has two primaries.
    a, b = CoreRegistry(":memory:"), CoreRegistry(":memory:")
    try:
        a.register("core-aaaaaaaa", SPACE, "A", is_self=True)
        a.promote("core-aaaaaaaa", 4)
        b.register("core-bbbbbbbb", SPACE, "B", is_self=True)
        b.promote("core-bbbbbbbb", 4)

        assert a.observe_peer("core-bbbbbbbb", 4, True) == VERDICT_CONTESTED
        assert b.observe_peer("core-aaaaaaaa", 4, True) == VERDICT_CONTESTED

        # Converged: exactly one of them still thinks it is in charge.
        assert a.self_core().status == STATUS_PRIMARY     # "core-a..." < "core-b..."
        assert b.self_core().status == STATUS_STANDBY
    finally:
        a.close()
        b.close()


# -- Staleness and failover groundwork ---------------------------------------

def _now():
    return datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def test_a_core_that_never_checked_in_is_stale_not_healthy():
    from wavr.core_registry import Core

    # A Core we have never heard from must never read as healthy-by-default.
    assert Core(core_id="c", space_id=SPACE, name="A", last_seen_ts=None).stale(_now())


def test_an_unparseable_last_seen_is_stale_not_healthy():
    from wavr.core_registry import Core

    assert Core(core_id="c", space_id=SPACE, name="A",
                last_seen_ts="whenever").stale(_now())


def test_stale_after_the_lease(reg):
    old = (_now() - timedelta(seconds=LEASE_SECONDS + 1)).isoformat()
    _at(reg, old)
    assert reg.register("core-aaaaaaaa", SPACE, "A").stale(_now()) is True
    _at(reg, _now().isoformat())
    reg.heartbeat("core-aaaaaaaa")
    assert reg.get("core-aaaaaaaa").stale(_now()) is False


def test_an_offline_primary_is_still_reported_as_primary_plus_stale(reg):
    # Collapsing "we lost contact" into "it handed over" is how an operator
    # stops knowing which Core is in charge.
    _at(reg, (_now() - timedelta(hours=1)).isoformat())
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.promote("core-aaaaaaaa", 2)
    d = reg.get("core-aaaaaaaa").to_dict(_now())
    assert d["status"] == STATUS_PRIMARY and d["stale"] is True
    assert d["effective_status"] == STATUS_PRIMARY


def test_no_candidate_while_the_primary_is_healthy(reg):
    _at(reg, _now().isoformat())
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.promote("core-aaaaaaaa", 2)
    assert reg.promotion_candidate(_now()) is None


def test_candidate_prefers_mains_power_over_a_phone(reg):
    _at(reg, (_now() - timedelta(hours=1)).isoformat())
    reg.register("core-aaaaaaaa", SPACE, "Dead primary")
    reg.promote("core-aaaaaaaa", 2)
    _at(reg, _now().isoformat())
    reg.register("core-phone000", SPACE, "Phone", portable=True)
    reg.register("core-pi000000", SPACE, "Pi", portable=False)
    assert reg.promotion_candidate(_now()).core_id == "core-pi000000"


def test_no_candidate_when_every_standby_is_also_gone(reg):
    _at(reg, (_now() - timedelta(hours=1)).isoformat())
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.promote("core-aaaaaaaa", 2)
    reg.register("core-bbbbbbbb", SPACE, "B")
    assert reg.promotion_candidate(_now()) is None


def test_nothing_promotes_itself(reg):
    # The whole "automatic failover is deliberately absent" claim, asserted.
    _at(reg, (_now() - timedelta(hours=5)).isoformat())
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.promote("core-aaaaaaaa", 2)
    _at(reg, _now().isoformat())
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.heartbeat("core-bbbbbbbb")
    reg.topology(_now())
    reg.promotion_candidate(_now())
    assert reg.primary().core_id == "core-aaaaaaaa"
    assert reg.get("core-bbbbbbbb").status == STATUS_STANDBY


# -- Topology ----------------------------------------------------------------

def test_topology_reports_leaderless_honestly(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    t = reg.topology()
    assert t["leaderless"] is True and t["primary_core_id"] is None
    assert t["contested"] is False


def test_topology_flags_a_contested_space(reg):
    reg.register("core-aaaaaaaa", SPACE, "A")
    reg.register("core-bbbbbbbb", SPACE, "B")
    reg.promote("core-aaaaaaaa", 2)
    # Force the pathological row state a split-brain would leave behind.
    reg._conn.execute(                                   # noqa: SLF001
        "UPDATE cores SET status = 'primary', epoch = 2 WHERE core_id = 'core-bbbbbbbb'")
    reg._conn.commit()                                   # noqa: SLF001
    assert reg.topology()["contested"] is True


def test_topology_is_empty_and_calm_on_a_fresh_core(reg):
    t = reg.topology()
    assert t["cores"] == [] and t["leaderless"] is False and t["contested"] is False
