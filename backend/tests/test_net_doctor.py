"""network-doctor: table-driven `diagnose()` (pure, no I/O) + `apply_fixes()`
(the only I/O, injected async fakes) + `DoctorLog` bounded-ring tests.

Every `diagnose()` case asserts the SAFE-AUTO allowlist enforced in code: a
disabled/privacy-off source is never even inspected, let alone proposed as a
fix candidate, and gateway/rogue-DHCP checks never produce a `FixCandidate`
at all (report-only by construction)."""
from wavr.net_doctor import (
    CAUSE_AP_ISOLATION,
    CAUSE_HOST_MULTICAST_UNAVAILABLE,
    CAUSE_INCONCLUSIVE_SMALL,
    CAUSE_MULTICAST_DEAD,
    CAUSE_SECOND_NETWORK,
    DoctorAction,
    DoctorCheck,
    DoctorLog,
    DoctorSuggestion,
    DoctorVerdict,
    SEVERITY_CRITICAL,
    SEVERITY_DEGRADED,
    SEVERITY_MINOR,
    SEVERITY_OK,
    apply_fixes,
    build_doctor_report,
    diagnose,
    redact_macs,
)
import re as _re

_RAW_MAC = _re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


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


# ---- 10. discovery_reach (CL-02, PR1) ---------------------------------------
# The verdict is STRUCTURED (DoctorVerdict), never a flat string, and REPORT-ONLY
# (never a FixCandidate -- this module never touches the router).

def test_discovery_reach_names_the_pathology_when_multicast_is_silent():
    checks, fixable = _diag(arp_count=15, mcast_responders=0)
    c = _by_id(checks, "discovery_reach")
    assert c.ok is False and c.severity == SEVERITY_DEGRADED
    assert c.verdict.cause == CAUSE_MULTICAST_DEAD
    assert c.verdict.arp_count == 15 and c.verdict.mcast_responders == 0
    assert c.verdict.copy_key == "discovery_multicast_dead"
    # report-only: NEVER proposes a fix (no router touch), even when unhealthy
    assert not any(f.id == "discovery_reach" for f in fixable)
    # the structured verdict survives serialization for the frontend
    assert c.to_dict()["verdict"]["cause"] == CAUSE_MULTICAST_DEAD


def test_discovery_reach_healthy_when_multicast_answers():
    checks, _ = _diag(arp_count=15, mcast_responders=5)
    c = _by_id(checks, "discovery_reach")
    assert c.ok is True and c.verdict.cause is None and c.verdict.copy_key == "discovery_ok"


def test_discovery_reach_small_net_never_false_positives():
    # a studio with 3 devices must NOT be flagged as isolated
    checks, _ = _diag(arp_count=3, mcast_responders=0)
    c = _by_id(checks, "discovery_reach")
    assert c.ok is None and c.verdict.cause == CAUSE_INCONCLUSIVE_SMALL
    assert c.verdict.copy_key == "discovery_small_net"


def test_discovery_reach_probe_unavailable_is_honest():
    # probe couldn't run -> "can't tell", never a false "multicast dead"
    checks, _ = _diag(arp_count=15, mcast_responders=None)
    c = _by_id(checks, "discovery_reach")
    assert c.ok is None and c.verdict.cause is None
    assert c.verdict.copy_key == "discovery_probe_unavailable"


def test_discovery_reach_boundary_at_the_floor():
    # exactly at the ARP floor (5) with a lone responder (<=1) still reads dead;
    # one more responder flips it healthy.
    dead = _by_id(_diag(arp_count=5, mcast_responders=1)[0], "discovery_reach")
    assert dead.ok is False and dead.verdict.cause == CAUSE_MULTICAST_DEAD
    ok = _by_id(_diag(arp_count=5, mcast_responders=2)[0], "discovery_reach")
    assert ok.ok is True


# ---- 10b. discovery_reach cause discrimination (CL-02, PR2) ------------------
# The HARD RULE: never blame the router without the host's multicast viability PROVEN.

def test_discovery_reach_host_unavailable_when_viability_false():
    # hub receives NO inbound LAN multicast (e.g. proot/container) -> blame the HOST, not router
    checks, fixable = _diag(arp_count=15, mcast_responders=0, host_multicast_viable=False)
    c = _by_id(checks, "discovery_reach")
    assert c.ok is False and c.severity == SEVERITY_DEGRADED
    assert c.verdict.cause == CAUSE_HOST_MULTICAST_UNAVAILABLE
    assert c.verdict.copy_key == "discovery_host_unavailable"
    assert not any(f.id == "discovery_reach" for f in fixable)   # still report-only


def test_discovery_reach_ap_isolation_when_viable_and_single_net():
    # viability PROVEN + one subnet/one DHCP -> the network is filtering discovery (router blame OK)
    checks, _ = _diag(arp_count=15, mcast_responders=0, host_multicast_viable=True)
    c = _by_id(checks, "discovery_reach")
    assert c.verdict.cause == CAUSE_AP_ISOLATION
    assert c.verdict.copy_key == "discovery_ap_isolation"


def test_discovery_reach_second_network_by_subnet():
    # viability PROVEN + devices span >1 subnet -> a second network / VLAN, not plain isolation
    checks, _ = _diag(arp_count=15, mcast_responders=0,
                      host_multicast_viable=True, arp_subnet_count=2)
    c = _by_id(checks, "discovery_reach")
    assert c.verdict.cause == CAUSE_SECOND_NETWORK
    assert c.verdict.copy_key == "discovery_second_network"


def test_discovery_reach_second_network_by_dhcp_count():
    # viability PROVEN + >1 DHCP server seen -> second network / VLAN
    checks, _ = _diag(arp_count=15, mcast_responders=0,
                      host_multicast_viable=True, dhcp_server_count=2)
    c = _by_id(checks, "discovery_reach")
    assert c.verdict.cause == CAUSE_SECOND_NETWORK


def test_discovery_reach_stays_neutral_without_viability():
    # HARD RULE: viability UNKNOWN (None) must NEVER produce a router accusation -> neutral cause
    checks, _ = _diag(arp_count=15, mcast_responders=0, host_multicast_viable=None)
    c = _by_id(checks, "discovery_reach")
    assert c.verdict.cause == CAUSE_MULTICAST_DEAD
    assert c.verdict.cause not in (CAUSE_AP_ISOLATION, CAUSE_SECOND_NETWORK)


# ---- 11. shareable report + MAC redaction (CL-02, PR4) ----------------------
# PRIVACY CONTRACT: a report pasted into a public GitHub issue must NEVER carry a raw MAC.

def test_redact_macs_masks_host_keeps_oui():
    assert redact_macs("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:**:**:**"
    assert redact_macs("AA-BB-CC-DD-EE-FF") == "AA-BB-CC-**-**-**"       # hyphen sep preserved
    # multiple in one string, both scrubbed; the OUI half survives for vendor debugging
    out = redact_macs("gw 11:22:33:44:55:66 vs de:ad:be:ef:00:11")
    assert _RAW_MAC.search(out) is None
    assert "11:22:33:**:**:**" in out and "de:ad:be:**:**:**" in out


def test_redact_macs_leaves_non_macs_alone():
    # IPs, plain hex, and UUIDs must not be mangled
    for s in ("192.168.1.1", "deadbeef", "550e8400-e29b-41d4-a716-446655440000", "port 5353"):
        assert redact_macs(s) == s


def _mac_check():
    # a gateway_identity-style detail that legitimately carries MACs (the realistic leak path)
    return DoctorCheck(id="gateway_identity", ok=False, severity=SEVERITY_CRITICAL,
                       detail="gateway 192.168.1.1 now aa:bb:cc:dd:ee:ff (was 11:22:33:44:55:66)")


def _verdict_check(cause, copy_key):
    return DoctorCheck(id="discovery_reach", ok=False, severity=SEVERITY_DEGRADED,
                       detail="9 devices reachable, 0 answered discovery",
                       verdict=DoctorVerdict(cause, "medium", 9, 0, copy_key))


def test_report_never_leaks_a_raw_mac():
    checks = [_mac_check(), _verdict_check(CAUSE_AP_ISOLATION, "discovery_ap_isolation")]
    actions = [DoctorAction(ts="2026-07-17T00:00:00+00:00", kind="restart_source",
                            target="network", detail="restarted network aa:bb:cc:dd:ee:ff")]
    suggestions = [DoctorSuggestion(id="x", message="check 99:88:77:66:55:44", action_hint="k")]
    report = build_doctor_report(checks, actions, suggestions)
    assert _RAW_MAC.search(report) is None            # <-- the core acceptance
    assert "aa:bb:cc:**:**:**" in report              # OUI preserved, host masked
    assert "AP_ISOLATION_OR_MDNS_FILTERING" in report and "discovery_ap_isolation" in report
    assert "checks: 2" in report and "auto-fixed: 1" in report and "suggestions: 1" in report


def test_report_matrix_every_cause_is_mac_free():
    matrix = [(CAUSE_AP_ISOLATION, "discovery_ap_isolation"),
              (CAUSE_SECOND_NETWORK, "discovery_second_network"),
              (CAUSE_HOST_MULTICAST_UNAVAILABLE, "discovery_host_unavailable"),
              (CAUSE_MULTICAST_DEAD, "discovery_multicast_dead")]
    for cause, key in matrix:
        report = build_doctor_report([_mac_check(), _verdict_check(cause, key)], [], [])
        assert _RAW_MAC.search(report) is None, f"raw MAC leaked for cause {cause}"
        assert cause in report


def test_report_includes_generated_timestamp_when_passed():
    report = build_doctor_report([], [], [], generated="2026-07-17T04:00:00+00:00")
    assert "generated: 2026-07-17T04:00:00+00:00" in report
