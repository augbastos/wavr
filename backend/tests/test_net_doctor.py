"""network-doctor: table-driven `diagnose()` (pure, no I/O) + `apply_fixes()`
(the only I/O, injected async fakes) + `DoctorLog` bounded-ring tests.

Every `diagnose()` case asserts the SAFE-AUTO allowlist enforced in code: a
disabled/privacy-off source is never even inspected, let alone proposed as a
fix candidate, and gateway/rogue-DHCP checks never produce a `FixCandidate`
at all (report-only by construction)."""
from wavr.net_doctor import (
    DoctorAction,
    DoctorLog,
    SEVERITY_CRITICAL,
    SEVERITY_MINOR,
    SEVERITY_OK,
    apply_fixes,
    diagnose,
)


def _diag(**overrides):
    """Baseline diagnose() kwargs -- every check reads as "off/no data" so a
    single overridden kwarg exercises exactly one check in isolation."""
    base = dict(
        health={"severity": SEVERITY_OK, "gateway": {"ok": True}, "failed": [],
                "resolvers": {}},
        gateway_status=None, gateway_alerts=[],
        dhcp_status=None, dhcp_alerts=[],
        source_status={"sources": []},
        camera_down=[], camera_privacy=[],
        room_sources={},
        last_inventory_scan_ts=None, net_scan_interval=30.0,
        mdns_expected=False, mdns_alive=False,
    )
    base.update(overrides)
    return diagnose(**base)


def _by_id(checks, id_):
    return next(c for c in checks if c.id == id_)


# ---- 1. internet ------------------------------------------------------------

def test_internet_ok_passthrough():
    checks, fixable = _diag(health={"severity": SEVERITY_OK, "gateway": {"ok": True},
                                     "failed": [], "resolvers": {}})
    c = _by_id(checks, "internet")
    assert c.ok is True and c.severity == SEVERITY_OK
    assert not any(f.id == "internet" for f in fixable)   # no fix -- external


def test_internet_critical_passthrough():
    checks, _ = _diag(health={"severity": SEVERITY_CRITICAL, "gateway": {"ok": False},
                               "failed": ["gateway"], "resolvers": {}})
    c = _by_id(checks, "internet")
    assert c.ok is False and c.severity == SEVERITY_CRITICAL


# ---- 2. dns -------------------------------------------------------------------

def test_dns_empty_resolvers_is_honest_none():
    checks, fixable = _diag(health={"severity": SEVERITY_OK, "gateway": {"ok": True},
                                     "failed": [], "resolvers": {}})
    c = _by_id(checks, "dns")
    assert c.ok is None and c.severity is None
    assert not any(f.id == "dns" for f in fixable)   # no fix -- external


def test_dns_one_down_is_minor_not_bad():
    checks, _ = _diag(health={"severity": SEVERITY_MINOR, "gateway": {"ok": True},
                               "failed": ["8.8.8.8"],
                               "resolvers": {"1.1.1.1": True, "8.8.8.8": False}})
    c = _by_id(checks, "dns")
    assert c.ok is False and c.severity == SEVERITY_MINOR


# ---- 3. gateway_identity -- REPORT-ONLY, never a fix -------------------------

def test_gateway_identity_disabled_is_none():
    checks, fixable = _diag(gateway_status=None)
    c = _by_id(checks, "gateway_identity")
    assert c.ok is None
    assert not any(f.id == "gateway_identity" for f in fixable)


def test_gateway_identity_recent_critical_alert_is_bad_but_never_a_fix():
    checks, fixable = _diag(
        gateway_status={"trusted_bindings": {}},
        gateway_alerts=[{"severity": "critical", "gateway_ip": "192.168.1.1"}],
    )
    c = _by_id(checks, "gateway_identity")
    assert c.ok is False
    # NEVER a fix candidate -- the router is never touched by this module.
    assert not any(f.id == "gateway_identity" for f in fixable)


def test_gateway_identity_clean_is_ok():
    checks, _ = _diag(gateway_status={"trusted_bindings": {"192.168.1.1": "aa:bb"}},
                       gateway_alerts=[])
    c = _by_id(checks, "gateway_identity")
    assert c.ok is True


# ---- 4. rogue_dhcp -- REPORT-ONLY, never a fix -------------------------------

def test_rogue_dhcp_disabled_is_none():
    checks, fixable = _diag(dhcp_status=None)
    c = _by_id(checks, "rogue_dhcp")
    assert c.ok is None
    assert not any(f.id == "rogue_dhcp" for f in fixable)


def test_rogue_dhcp_unavailable_is_honest_none_not_bad():
    checks, _ = _diag(dhcp_status={"available": False, "unavailable_reason": "PermissionError"})
    c = _by_id(checks, "rogue_dhcp")
    assert c.ok is None and "PermissionError" in c.detail


def test_rogue_dhcp_recent_alert_is_bad_but_never_a_fix():
    checks, fixable = _diag(
        dhcp_status={"available": True, "known_servers": [], "observed_servers": []},
        dhcp_alerts=[{"severity": "alert", "extra_server": "10.0.0.99"}],
    )
    c = _by_id(checks, "rogue_dhcp")
    assert c.ok is False
    assert not any(f.id == "rogue_dhcp" for f in fixable)


# ---- 5. capture_stalled -------------------------------------------------------

def test_capture_stalled_enabled_inactive_source_is_fixable():
    checks, fixable = _diag(source_status={"sources": [
        {"name": "network", "enabled": True, "active": False},
    ]})
    c = _by_id(checks, "capture_stalled:network")
    assert c.ok is False
    fix = next(f for f in fixable if f.target == "network")
    assert fix.kind == "restart_source"


def test_capture_stalled_never_inspects_disabled_source():
    # A disabled source (e.g. the currently-OFF camera) must never even be
    # SURFACED as a problem, let alone proposed as a fix -- hard constraint.
    checks, fixable = _diag(source_status={"sources": [
        {"name": "camera1", "enabled": False, "active": False},
    ]})
    assert not any(c.id.startswith("capture_stalled:") for c in checks)
    assert not any(f.target == "camera1" for f in fixable)


def test_capture_stalled_active_enabled_source_is_healthy():
    checks, fixable = _diag(source_status={"sources": [
        {"name": "network", "enabled": True, "active": True},
    ]})
    assert not any(c.id.startswith("capture_stalled:") for c in checks)
    assert not fixable


# ---- 6. camera_stalled --------------------------------------------------------

def test_camera_stalled_down_and_enabled_is_fixable():
    checks, fixable = _diag(
        source_status={"sources": [{"name": "cam1", "enabled": True, "active": True}]},
        camera_down=["cam1"], camera_privacy=[],
    )
    c = _by_id(checks, "camera_stalled:cam1")
    assert c.ok is False
    fix = next(f for f in fixable if f.target == "cam1")
    assert fix.kind == "restart_source"


def test_camera_stalled_excludes_privacy_mode_camera():
    # A deliberately-covered Tapo camera must NEVER be treated as faulty.
    checks, fixable = _diag(
        source_status={"sources": [{"name": "cam1", "enabled": True, "active": True}]},
        camera_down=["cam1"], camera_privacy=["cam1"],
    )
    assert not any(c.id == "camera_stalled:cam1" for c in checks)
    assert not any(f.target == "cam1" for f in fixable)


def test_camera_stalled_excludes_disabled_camera():
    # A camera left OFF (hard constraint) must never be auto-restarted, even
    # if camera_health still reports it as latched-down from before it was
    # disabled.
    checks, fixable = _diag(
        source_status={"sources": [{"name": "cam1", "enabled": False, "active": False}]},
        camera_down=["cam1"], camera_privacy=[],
    )
    assert not any(c.id == "camera_stalled:cam1" for c in checks)
    assert not any(f.target == "cam1" for f in fixable)


def test_camera_stalled_and_capture_stalled_dedupe_by_target():
    # A camera that is BOTH SourceManager-inactive AND health-down must
    # produce exactly ONE fix candidate for that target, not two competing
    # restarts.
    checks, fixable = _diag(
        source_status={"sources": [{"name": "cam1", "enabled": True, "active": False}]},
        camera_down=["cam1"], camera_privacy=[],
    )
    matches = [f for f in fixable if f.target == "cam1"]
    assert len(matches) == 1


# ---- 7. mdns_advertise --------------------------------------------------------

def test_mdns_advertise_disabled_is_none():
    checks, fixable = _diag(mdns_expected=False, mdns_alive=False)
    c = _by_id(checks, "mdns_advertise")
    assert c.ok is None
    assert not any(f.kind == "reannounce_mdns" for f in fixable)


def test_mdns_advertise_expected_but_dead_is_fixable():
    checks, fixable = _diag(mdns_expected=True, mdns_alive=False)
    c = _by_id(checks, "mdns_advertise")
    assert c.ok is False
    fix = next(f for f in fixable if f.kind == "reannounce_mdns")
    assert fix.target == "self"


def test_mdns_advertise_alive_is_ok():
    checks, fixable = _diag(mdns_expected=True, mdns_alive=True)
    c = _by_id(checks, "mdns_advertise")
    assert c.ok is True
    assert not any(f.kind == "reannounce_mdns" for f in fixable)


# ---- 8. inventory_freshness ---------------------------------------------------

def test_inventory_freshness_no_scan_yet_is_none():
    checks, fixable = _diag(last_inventory_scan_ts=None)
    c = _by_id(checks, "inventory_freshness")
    assert c.ok is None
    assert not any(f.kind == "reprobe_inventory" for f in fixable)


def test_inventory_freshness_stale_is_fixable():
    import datetime as dt
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1000)).isoformat()
    checks, fixable = _diag(last_inventory_scan_ts=old, net_scan_interval=30.0)
    c = _by_id(checks, "inventory_freshness")
    assert c.ok is False
    fix = next(f for f in fixable if f.kind == "reprobe_inventory")
    assert fix.target == "inventory"


def test_inventory_freshness_fresh_is_ok():
    import datetime as dt
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    checks, fixable = _diag(last_inventory_scan_ts=recent, net_scan_interval=30.0)
    c = _by_id(checks, "inventory_freshness")
    assert c.ok is True
    assert not any(f.kind == "reprobe_inventory" for f in fixable)


# ---- 9. signal_freshness -- REPORT-ONLY ---------------------------------------

def test_signal_freshness_no_rooms_is_none():
    checks, fixable = _diag(room_sources={})
    c = _by_id(checks, "signal_freshness")
    assert c.ok is None
    assert not any(f.id == "signal_freshness" for f in fixable)   # never a fix


def test_signal_freshness_total_loss_is_critical():
    checks, fixable = _diag(room_sources={
        "kitchen": [{"modality": "network", "health": "dead"}],
    })
    c = _by_id(checks, "signal_freshness")
    assert c.ok is False and c.severity == SEVERITY_CRITICAL
    assert not any(f.id == "signal_freshness" for f in fixable)


def test_signal_freshness_partial_stale_is_visible_but_not_bad():
    checks, fixable = _diag(room_sources={
        "kitchen": [{"modality": "network", "health": "stale"},
                    {"modality": "camera", "health": "fresh"}],
    })
    c = _by_id(checks, "signal_freshness")
    assert c.ok is True and c.severity == SEVERITY_MINOR
    assert "kitchen/network" in c.detail
    assert not any(f.id == "signal_freshness" for f in fixable)


def test_signal_freshness_all_fresh_is_ok():
    checks, _ = _diag(room_sources={
        "kitchen": [{"modality": "network", "health": "fresh"}],
    })
    c = _by_id(checks, "signal_freshness")
    assert c.ok is True and c.severity == SEVERITY_OK


# ---- apply_fixes() -- the only I/O, injected async fakes ---------------------

class _Recorder:
    def __init__(self, raise_on=None):
        self.restarted = []
        self.reprobed = 0
        self.reannounced = 0
        self._raise_on = raise_on or set()

    async def restart_source(self, name):
        if "restart" in self._raise_on:
            raise RuntimeError("source restart boom")
        self.restarted.append(name)

    async def reprobe_inventory(self):
        if "reprobe" in self._raise_on:
            raise RuntimeError("reprobe boom")
        self.reprobed += 1

    def reannounce_mdns(self):
        if "mdns" in self._raise_on:
            raise RuntimeError("mdns boom")
        self.reannounced += 1


def _candidate(kind="restart_source", target="network"):
    from wavr.net_doctor import FixCandidate
    return FixCandidate(id=f"{kind}:{target}", kind=kind, target=target, explain="test fix")


async def test_apply_fixes_disabled_renders_suggestions_only():
    rec = _Recorder()
    log = DoctorLog()
    fixed, suggestions = await apply_fixes(
        [_candidate()], enabled=False,
        restart_source=rec.restart_source, reprobe_inventory=rec.reprobe_inventory,
        reannounce_mdns=rec.reannounce_mdns, log=log,
    )
    assert fixed == []
    assert len(suggestions) == 1 and suggestions[0].action_hint == "restart_source"
    assert rec.restarted == []          # NOTHING executed
    assert log.recent() == []


async def test_apply_fixes_enabled_dispatches_restart_source():
    rec = _Recorder()
    log = DoctorLog()
    fixed, suggestions = await apply_fixes(
        [_candidate(target="cam1")], enabled=True,
        restart_source=rec.restart_source, reprobe_inventory=rec.reprobe_inventory,
        reannounce_mdns=rec.reannounce_mdns, log=log,
    )
    assert rec.restarted == ["cam1"]
    assert len(fixed) == 1 and isinstance(fixed[0], DoctorAction)
    assert suggestions == []
    assert len(log.recent()) == 1


async def test_apply_fixes_enabled_dispatches_reprobe_and_mdns():
    rec = _Recorder()
    log = DoctorLog()
    fixed, _ = await apply_fixes(
        [_candidate(kind="reprobe_inventory", target="inventory"),
         _candidate(kind="reannounce_mdns", target="self")],
        enabled=True,
        restart_source=rec.restart_source, reprobe_inventory=rec.reprobe_inventory,
        reannounce_mdns=rec.reannounce_mdns, log=log,
    )
    assert rec.reprobed == 1 and rec.reannounced == 1
    assert len(fixed) == 2


async def test_apply_fixes_raising_fix_degrades_to_suggestion_never_raises():
    rec = _Recorder(raise_on={"restart"})
    log = DoctorLog()
    fixed, suggestions = await apply_fixes(
        [_candidate(target="cam1")], enabled=True,
        restart_source=rec.restart_source, reprobe_inventory=rec.reprobe_inventory,
        reannounce_mdns=rec.reannounce_mdns, log=log,
    )
    assert fixed == []
    assert len(suggestions) == 1 and "auto-fix failed" in suggestions[0].message
    assert log.recent() == []   # a failed fix is never logged as executed


# ---- DoctorLog bounded ring ----------------------------------------------------

def test_doctor_log_trims_to_bound():
    log = DoctorLog(max_len=3)
    for i in range(5):
        log.record(DoctorAction(ts=str(i), kind="restart_source", target=f"s{i}",
                                 detail="x"))
    recent = log.recent(10)
    assert len(recent) == 3
    assert [a.target for a in recent] == ["s2", "s3", "s4"]   # newest 3, oldest evicted
