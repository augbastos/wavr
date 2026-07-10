import asyncio

from wavr.dhcp_monitor import DhcpRogueAlert, RogueDhcpMonitor


def _collector(cycles):
    """Injectable collect() transport: pops one {server_key: {...}} dict per
    call (no real network) -- tests size `cycles` to the number of expected
    check_once() calls."""
    it = iter(cycles)

    async def collect() -> dict:
        return next(it)
    return collect


def _obs(*server_ids) -> dict:
    return {sid: {"ip": sid, "yiaddr": None, "offers": 1} for sid in server_ids}


# ---- baseline auto-adopts on first cycle without alerting -------------------

async def test_first_cycle_settles_baseline_without_alerting():
    fired = []
    m = RogueDhcpMonitor(collect=_collector([_obs("192.168.1.1")]),
                         on_rogue=fired.append)
    await m.check_once()
    assert m.status()["known_servers"] == ["192.168.1.1"]
    assert fired == []


async def test_explicit_empty_known_servers_is_strict_not_auto_baseline():
    # An explicitly-passed EMPTY set means "nothing is known-good" (strict),
    # distinct from leaving known_servers unset (auto-adopt first cycle).
    fired = []
    m = RogueDhcpMonitor(collect=_collector([_obs("192.168.1.1")]),
                         known_servers=set(), alert_threshold=1, on_rogue=fired.append)
    await m.check_once()
    assert len(fired) == 1
    assert fired[0].extra_server == "192.168.1.1"


async def test_explicit_known_servers_seed_the_baseline():
    fired = []
    m = RogueDhcpMonitor(collect=_collector([_obs("192.168.1.1")]),
                         known_servers={"192.168.1.1"}, on_rogue=fired.append)
    await m.check_once()
    assert fired == []


# ---- debounce: a single stray extra server must not alert -------------------

async def test_single_cycle_extra_server_does_not_alert_below_threshold():
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([_obs("192.168.1.1"), _obs("192.168.1.1", "10.0.0.66")]),
        alert_threshold=2, on_rogue=fired.append)
    await m.check_once()   # settles baseline = {192.168.1.1}
    await m.check_once()   # extra seen once (1/2) -- not yet alerted
    assert fired == []


async def test_extra_server_alerts_after_consecutive_threshold_met():
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([
            _obs("192.168.1.1"),
            _obs("192.168.1.1", "10.0.0.66"),
            _obs("192.168.1.1", "10.0.0.66"),
        ]),
        alert_threshold=2, on_rogue=fired.append)
    await m.check_once()
    await m.check_once()
    await m.check_once()
    assert len(fired) == 1
    alert = fired[0]
    assert isinstance(alert, DhcpRogueAlert)
    assert alert.extra_server == "10.0.0.66"
    assert alert.known_servers == ("192.168.1.1",)
    assert alert.severity == "alert"   # unified wavr.alert_severity ladder
    assert alert.to_dict()["kind"] == "rogue_dhcp"


# ---- audit fix #5: an intermittent extra server still accumulates ----------

async def test_intermittent_extra_server_still_accumulates_and_fires():
    # Audit fix #5: the OLD strict consecutive-streak reset meant an
    # intermittent rogue (present, absent, present, ...) NEVER reached
    # `alert_threshold` consecutive cycles and never fired -- a real gap in
    # the detector. The leaky N-of-M window credits "present" cycles without
    # wiping progress on a single absent one, so this now correctly fires.
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([
            _obs("192.168.1.1"),                       # baseline
            _obs("192.168.1.1", "10.0.0.66"),           # extra present (1/2 in-window)
            _obs("192.168.1.1"),                        # extra absent this cycle only
            _obs("192.168.1.1", "10.0.0.66"),           # extra present again -> 2/window -> fires
        ]),
        alert_threshold=2, on_rogue=fired.append)
    for _ in range(4):
        await m.check_once()
    assert len(fired) == 1
    assert fired[0].extra_server == "10.0.0.66"


async def test_extra_server_that_never_returns_is_eventually_forgotten():
    # A genuine one-off blip that never crosses the threshold and stays gone
    # long enough to fall completely out of the window is forgotten (bounds
    # memory) -- same end result as the old immediate-reset behavior for a
    # TRULY departed id: it never fires.
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([
            _obs("192.168.1.1"),                       # baseline
            _obs("192.168.1.1", "10.0.0.66"),           # extra present once (1/2)
            _obs("192.168.1.1"),
            _obs("192.168.1.1"),
            _obs("192.168.1.1"),
            _obs("192.168.1.1"),                        # 4 clean cycles -- well out of any window
        ]),
        alert_threshold=2, on_rogue=fired.append)
    for _ in range(6):
        await m.check_once()
    assert fired == []
    assert "10.0.0.66" not in m._windows   # forgotten, not lingering forever


async def test_router_reboot_single_known_server_blinking_never_alerts():
    # The router's own DHCP server briefly disappearing (reboot) and coming
    # back is NOT "an extra server" -- there is nothing new, just fewer/more
    # of the SAME known id. Must never alert.
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([_obs("192.168.1.1"), _obs(), _obs("192.168.1.1")]),
        known_servers={"192.168.1.1"}, alert_threshold=1, on_rogue=fired.append)
    for _ in range(3):
        await m.check_once()
    assert fired == []


# ---- edge-triggered: no repeat alert while the extra server persists -------

async def test_no_repeat_alert_while_extra_server_persists():
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([_obs("192.168.1.1", "10.0.0.66")] * 4),
        known_servers={"192.168.1.1"}, alert_threshold=1, on_rogue=fired.append)
    for _ in range(4):
        await m.check_once()
    assert len(fired) == 1   # fires once, not once per cycle


async def test_extra_server_realerts_after_departing_and_returning():
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([
            _obs("192.168.1.1", "10.0.0.66"),  # fires
            _obs("192.168.1.1"),                # departs -- forgotten
            _obs("192.168.1.1", "10.0.0.66"),  # returns -- fires again
        ]),
        known_servers={"192.168.1.1"}, alert_threshold=1, on_rogue=fired.append)
    for _ in range(3):
        await m.check_once()
    assert len(fired) == 2


# ---- audit fix #5: anti-flood cap + coalesced on_rogue ----------------------

async def test_multiple_ids_crossing_threshold_in_one_cycle_coalesce_to_one_notification():
    # Two brand-new server ids that BOTH reach the threshold in the SAME
    # cycle must fire `on_rogue` exactly ONCE (coalesced), not once per id --
    # the alert log still gets one row per id for triage.
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([_obs("192.168.1.1", "10.0.0.1", "10.0.0.2")]),
        known_servers={"192.168.1.1"}, alert_threshold=1, on_rogue=fired.append)
    await m.check_once()
    assert len(fired) == 1
    assert "10.0.0.1" in fired[0].extra_server and "10.0.0.2" in fired[0].extra_server
    assert len(m.recent_alerts()) == 2   # the log still records one row per id


async def test_flood_of_spoofed_server_ids_is_capped_and_does_not_evict_genuine_progress():
    # A genuine rogue has already begun accumulating (1/2 within its window)
    # when a flood of spoofed option-54 ids arrives in the SAME later cycle --
    # the cap must protect the genuine entry's progress and bound memory,
    # not let the flood crowd it out.
    flood = [f"10.0.0.{i}" for i in range(200)]
    fired = []
    m = RogueDhcpMonitor(
        collect=_collector([
            _obs("192.168.1.1"),                            # baseline
            _obs("192.168.1.1", "10.0.0.66"),                # genuine extra: 1/2 in-window
            _obs("192.168.1.1", "10.0.0.66", *flood),        # genuine extra again + flood arrives
        ]),
        alert_threshold=2, max_tracked_extras=10, on_rogue=fired.append)
    for _ in range(3):
        await m.check_once()
    assert len(fired) == 1                        # only the genuine, already-tracked id fired
    assert fired[0].extra_server == "10.0.0.66"
    assert len(m._windows) <= 10                   # cap held despite a 200-id flood


# ---- recent_alerts() bounded ring -------------------------------------------

async def test_recent_alerts_bounded_ring():
    cycles = [_obs("192.168.1.1")]
    for i in range(5):
        cycles.append(_obs("192.168.1.1", f"10.0.0.{i}"))
    m = RogueDhcpMonitor(collect=_collector(cycles), known_servers={"192.168.1.1"},
                         alert_threshold=1, max_alerts=2)
    for _ in range(len(cycles)):
        await m.check_once()
    assert len(m.recent_alerts()) == 2


# ---- tolerance: a raising collect/on_rogue never propagates -----------------

async def test_raising_collect_counts_as_no_observation_and_does_not_raise():
    async def boom():
        raise RuntimeError("socket closed")
    m = RogueDhcpMonitor(collect=boom)
    observed = await m.check_once()   # must not raise
    assert observed == set()


# ---- honest availability signal (panel-review finding #9/#17) --------------

async def test_available_none_before_first_check():
    m = RogueDhcpMonitor(collect=_collector([_obs("192.168.1.1")]))
    assert m.available is None
    assert m.unavailable_reason is None
    assert m.status()["available"] is None
    assert m.status()["unavailable_reason"] is None


async def test_available_true_after_a_clean_cycle():
    m = RogueDhcpMonitor(collect=_collector([_obs("192.168.1.1")]))
    await m.check_once()
    assert m.available is True
    assert m.unavailable_reason is None


async def test_available_false_on_permission_error_from_collect():
    # Simulates a non-root proot/container lacking CAP_NET_BIND_SERVICE for
    # the UDP/68 raw bind DHCPCollector's transport performs.
    async def boom():
        raise PermissionError("[Errno 13] Permission denied")
    m = RogueDhcpMonitor(collect=boom)
    observed = await m.check_once()   # must not raise -- same tolerance as any collect failure
    assert observed == set()
    assert m.available is False
    assert "PermissionError" in m.unavailable_reason
    assert m.status()["available"] is False


async def test_available_false_on_plain_os_error_from_collect():
    async def boom():
        raise OSError("Address already in use")
    m = RogueDhcpMonitor(collect=boom)
    await m.check_once()
    assert m.available is False
    assert "Address already in use" in m.unavailable_reason


async def test_generic_exception_does_not_set_available_false():
    # A non-OSError failure (e.g. a bug, not an environment limitation) keeps
    # the honest tri-state: it must NOT be mislabeled "unavailable by
    # environment" -- that label is reserved for the bind-failure case.
    async def boom():
        raise RuntimeError("socket closed")
    m = RogueDhcpMonitor(collect=boom)
    await m.check_once()
    assert m.available is None
    assert m.unavailable_reason is None


async def test_available_reflects_most_recent_cycle():
    state = {"boom": True}

    async def collect():
        if state["boom"]:
            raise PermissionError("Permission denied")
        return _obs("192.168.1.1")
    m = RogueDhcpMonitor(collect=collect)
    await m.check_once()
    assert m.available is False

    state["boom"] = False
    await m.check_once()
    assert m.available is True
    assert m.unavailable_reason is None


async def test_raising_on_rogue_does_not_propagate():
    def boom(alert):
        raise RuntimeError("notifier down")
    m = RogueDhcpMonitor(
        collect=_collector([_obs("10.0.0.66")]),
        known_servers={"192.168.1.1"}, alert_threshold=1, on_rogue=boom)
    await m.check_once()   # "10.0.0.66" is extra and fires immediately -- must not raise
    assert len(m.recent_alerts()) == 1


# ---- start()/stop() cancel-safety (mirrors InternetMonitor) -----------------

async def test_start_runs_checks_then_stop_is_cancel_safe():
    calls = []

    async def collect():
        calls.append(1)
        return {}
    m = RogueDhcpMonitor(collect=collect, interval=0.01)
    await m.start()
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls
    await m.stop()
    await m.stop()   # idempotent
