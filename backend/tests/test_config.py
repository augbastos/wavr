from wavr.config import load_config

def test_defaults_load_without_env():
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
