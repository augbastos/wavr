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
