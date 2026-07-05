from wavr.config import load_config

def test_defaults_load_without_env(monkeypatch):
    # a developer's real .env (loaded by load_dotenv) must not leak into this test
    for var in ("WAVR_DB", "WAVR_SIM_INTERVAL", "WAVR_FUSION_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.db_path == "wavr.db"
    assert cfg.sim_interval == 1.0
    assert cfg.fusion_threshold == 0.5

def test_config_has_source_b_defaults(monkeypatch):
    for var in ("WAVR_NET_MACS", "WAVR_NET_INTERVAL", "WAVR_NET_GRACE",
                "WAVR_RUVIEW_URL", "WAVR_RUVIEW_ROOM", "WAVR_RUVIEW_RECONNECT"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.net_known_macs == set()
    assert cfg.net_interval == 15.0
    assert cfg.net_grace == 2
    assert cfg.ruview_url == "ws://localhost:3000/ws/sensing"
    assert cfg.ruview_room == "sala"
    assert cfg.ruview_reconnect == 3.0

def test_identity_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("WAVR_IDENTITY_ENABLED", raising=False)
    cfg = load_config()
    assert cfg.identity_enabled is False   # opt-in: off by default


def test_identity_enabled_parses_truthy(monkeypatch):
    for val in ("1", "true", "yes"):
        monkeypatch.setenv("WAVR_IDENTITY_ENABLED", val)
        assert load_config().identity_enabled is True
    monkeypatch.setenv("WAVR_IDENTITY_ENABLED", "no")
    assert load_config().identity_enabled is False


def test_net_known_parses_mac_person_and_folds_into_presence(monkeypatch):
    for v in ("WAVR_NET_KNOWN", "WAVR_NET_MACS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("WAVR_NET_KNOWN", "AA-BB-CC-DD-EE-FF=alice, 11:22:33:44:55:66=phone")
    monkeypatch.setenv("WAVR_NET_MACS", "99:99:99:99:99:99")
    cfg = load_config()
    assert cfg.net_known == {
        "aa:bb:cc:dd:ee:ff": "alice",
        "11:22:33:44:55:66": "phone",
    }
    # WAVR_NET_KNOWN keys also count toward presence (union with WAVR_NET_MACS).
    assert cfg.net_known_macs == {
        "aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "99:99:99:99:99:99",
    }


def test_net_known_defaults_empty(monkeypatch):
    monkeypatch.delenv("WAVR_NET_KNOWN", raising=False)
    assert load_config().net_known == {}


def test_config_has_camera_defaults(monkeypatch):
    for var in ("WAVR_CAM_INTERVAL", "WAVR_CAM_CONFIDENCE"):
        monkeypatch.delenv(var, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.cam_interval == 0.5
    assert cfg.cam_confidence == 0.4

def test_config_has_mqtt_defaults(monkeypatch):
    for v in ("WAVR_MQTT_ENABLED", "WAVR_MQTT_HOST", "WAVR_MQTT_PORT", "WAVR_MQTT_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.mqtt_enabled is False       # opt-in: off by default
    assert cfg.mqtt_host == "localhost"
    assert cfg.mqtt_port == 1883
    assert cfg.mqtt_prefix == "wavr"

def test_config_has_away_default(monkeypatch):
    monkeypatch.delenv("WAVR_AWAY_GRACE", raising=False)
    from wavr.config import load_config
    assert load_config().away_grace == 3

def test_config_has_gemini_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("WAVR_GEMINI_MODEL", raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.gemini_api_key == ""
    assert cfg.gemini_model == "gemini-1.5-flash"

def test_config_narrate_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("WAVR_NARRATE_ENABLED", raising=False)
    from wavr.config import load_config
    assert load_config().narrate_enabled is False

def test_config_has_ha_read_defaults(monkeypatch):
    # HA read-side (ADR-0005): both empty by default -> read tool disabled.
    for v in ("WAVR_HA_URL", "WAVR_HA_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.ha_url == ""
    assert cfg.ha_token == ""

def test_config_reads_ha_env(monkeypatch):
    monkeypatch.setenv("WAVR_HA_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("WAVR_HA_TOKEN", "long-lived-token")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.ha_url == "http://homeassistant.local:8123"
    assert cfg.ha_token == "long-lived-token"

def test_config_port_default_and_env(monkeypatch):
    # Serve port (WAVR_PORT) — used by `python -m wavr.serve` in both modes.
    monkeypatch.delenv("WAVR_PORT", raising=False)
    from wavr.config import load_config
    assert load_config().port == 8000
    monkeypatch.setenv("WAVR_PORT", "8443")
    assert load_config().port == 8443

def test_config_has_internet_monitor_defaults(monkeypatch):
    for v in ("WAVR_INTERNET_MONITOR", "WAVR_INTERNET_CHECK_HOST",
              "WAVR_INTERNET_CHECK_INTERVAL", "WAVR_INTERNET_FAIL_THRESHOLD"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.internet_monitor is False       # opt-in: off by default
    assert cfg.internet_check_host == ""       # empty -> auto-guess the LAN gateway
    assert cfg.internet_check_interval == 15.0
    assert cfg.internet_fail_threshold == 3

def test_config_reads_internet_monitor_env(monkeypatch):
    monkeypatch.setenv("WAVR_INTERNET_MONITOR", "1")
    monkeypatch.setenv("WAVR_INTERNET_CHECK_HOST", "1.1.1.1")
    monkeypatch.setenv("WAVR_INTERNET_CHECK_INTERVAL", "5")
    monkeypatch.setenv("WAVR_INTERNET_FAIL_THRESHOLD", "2")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.internet_monitor is True
    assert cfg.internet_check_host == "1.1.1.1"
    assert cfg.internet_check_interval == 5.0
    assert cfg.internet_fail_threshold == 2


def test_config_has_collectors_lote2_defaults(monkeypatch):
    # NetBIOS/SNMP/DHCP-fp/rogue-DHCP/health-ladder -- every one opt-in, off by
    # default (collectors-lote2). NetBIOS/SNMP scope defaults to known-only
    # (audit fix #4 -- an active unicast probe is more intrusive than passive
    # listening); health resolver egress defaults OFF (audit fix #1).
    for v in ("WAVR_NET_NETBIOS", "WAVR_NET_NETBIOS_SCOPE", "WAVR_NET_SNMP",
              "WAVR_NET_SNMP_COMMUNITY", "WAVR_NET_SNMP_SCOPE", "WAVR_NET_DHCP_FP",
              "WAVR_NET_DHCP_MONITOR", "WAVR_NET_DHCP_PROBE",
              "WAVR_NET_DHCP_KNOWN_SERVERS", "WAVR_NET_DHCP_INTERVAL",
              "WAVR_NET_DHCP_ALERT_THRESHOLD", "WAVR_HEALTH_EXTRA_TARGETS",
              "WAVR_HEALTH_RESOLVERS"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_netbios is False
    assert cfg.net_netbios_scope_known_only is True
    assert cfg.net_snmp is False
    assert cfg.net_snmp_community == "public"
    assert cfg.net_snmp_scope_known_only is True
    assert cfg.net_dhcp_fp is False
    assert cfg.net_dhcp_monitor is False
    assert cfg.net_dhcp_probe is False
    assert cfg.net_dhcp_known_servers == set()
    assert cfg.net_dhcp_interval == 30.0
    assert cfg.net_dhcp_alert_threshold == 2
    assert cfg.health_extra_targets == ()
    assert cfg.health_resolvers_enabled is False


def test_config_reads_collectors_lote2_env(monkeypatch):
    monkeypatch.setenv("WAVR_NET_NETBIOS", "1")
    monkeypatch.setenv("WAVR_NET_NETBIOS_SCOPE", "all")
    monkeypatch.setenv("WAVR_NET_SNMP", "1")
    monkeypatch.setenv("WAVR_NET_SNMP_COMMUNITY", "monitoring")
    monkeypatch.setenv("WAVR_NET_SNMP_SCOPE", "all")
    monkeypatch.setenv("WAVR_NET_DHCP_FP", "1")
    monkeypatch.setenv("WAVR_NET_DHCP_MONITOR", "1")
    monkeypatch.setenv("WAVR_NET_DHCP_KNOWN_SERVERS", "192.168.0.1, 192.168.0.2")
    monkeypatch.setenv("WAVR_HEALTH_EXTRA_TARGETS", "9.9.9.9, example.com")
    monkeypatch.setenv("WAVR_HEALTH_RESOLVERS", "1")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_netbios is True
    # SCOPE=all is the explicit opt-out of the known-only default (audit fix #4).
    assert cfg.net_netbios_scope_known_only is False
    assert cfg.net_snmp is True
    assert cfg.net_snmp_community == "monitoring"
    assert cfg.net_snmp_scope_known_only is False
    assert cfg.net_dhcp_fp is True
    assert cfg.net_dhcp_monitor is True
    assert cfg.net_dhcp_known_servers == {"192.168.0.1", "192.168.0.2"}
    assert cfg.health_extra_targets == ("9.9.9.9", "example.com")
    assert cfg.health_resolvers_enabled is True


def test_config_netbios_snmp_scope_known_only_is_the_default_without_env_var(monkeypatch):
    # Leaving WAVR_NET_NETBIOS_SCOPE/WAVR_NET_SNMP_SCOPE completely unset (not
    # even "known") must still land on known-only -- the safe default, not an
    # opt-in one (audit fix #4).
    monkeypatch.delenv("WAVR_NET_NETBIOS_SCOPE", raising=False)
    monkeypatch.delenv("WAVR_NET_SNMP_SCOPE", raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_netbios_scope_known_only is True
    assert cfg.net_snmp_scope_known_only is True


def test_config_has_a3_tools_defaults(monkeypatch):
    # A3 standalone tools -- WoL / diagnostics / speed test all opt-in, off by
    # default; the speed-test provider defaults to the lower-disclosure
    # `cloudflare` (only `ndt7` reaches the IP-publishing M-Lab path).
    for v in ("WAVR_NET_WOL", "WAVR_NET_DIAGNOSTICS", "WAVR_NET_SPEEDTEST",
              "WAVR_SPEEDTEST_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_wol is False
    assert cfg.net_diagnostics is False
    assert cfg.net_speedtest is False
    assert cfg.speedtest_provider == "cloudflare"


def test_config_reads_a3_tools_env(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.setenv("WAVR_SPEEDTEST_PROVIDER", "ndt7")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_wol is True
    assert cfg.net_diagnostics is True
    assert cfg.net_speedtest is True
    assert cfg.speedtest_provider == "ndt7"


def test_config_speedtest_provider_bad_value_falls_back_to_cloudflare(monkeypatch):
    # A typo / hostile value must never silently reach the IP-publishing path.
    monkeypatch.setenv("WAVR_SPEEDTEST_PROVIDER", "mlab-please")
    from wavr.config import load_config
    assert load_config().speedtest_provider == "cloudflare"


def test_config_netbios_snmp_scope_known_explicit_still_narrows(monkeypatch):
    # SCOPE=known (the old explicit opt-in) is still accepted and still narrows.
    monkeypatch.setenv("WAVR_NET_NETBIOS_SCOPE", "known")
    monkeypatch.setenv("WAVR_NET_SNMP_SCOPE", "known")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.net_netbios_scope_known_only is True
    assert cfg.net_snmp_scope_known_only is True


def test_config_cam_unhealthy_secs_default_and_override(monkeypatch):
    # F3: camera IP-drift health threshold. Default 30s; overridable.
    from wavr.config import load_config
    monkeypatch.delenv("WAVR_CAM_UNHEALTHY_SECS", raising=False)
    assert load_config().cam_unhealthy_secs == 30.0
    monkeypatch.setenv("WAVR_CAM_UNHEALTHY_SECS", "5")
    assert load_config().cam_unhealthy_secs == 5.0
