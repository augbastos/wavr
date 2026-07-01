from wavr.config import load_config

def test_defaults_load_without_env():
    cfg = load_config()
    assert cfg.db_path == "wavr.db"
    assert cfg.sim_interval == 1.0
    assert cfg.fusion_threshold == 0.5
