import json

from wavr.housemap import load_house_map, DEFAULT_MAP


def test_missing_path_returns_default():
    assert load_house_map("") == DEFAULT_MAP
    assert load_house_map("nope/does-not-exist.json") == DEFAULT_MAP


def test_valid_file_loads(tmp_path):
    p = tmp_path / "house.json"
    m = {"rooms": [{"name": "lab", "x": 0, "y": 0, "w": 5, "h": 4}]}
    p.write_text(json.dumps(m), encoding="utf-8")
    assert load_house_map(str(p)) == m


def test_garbage_file_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_house_map(str(p)) == DEFAULT_MAP
