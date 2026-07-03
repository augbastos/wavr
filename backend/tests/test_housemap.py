from wavr.housemap import DEFAULT_MAP, load_house_map, room_names


def test_default_map_is_v2():
    assert DEFAULT_MAP["version"] == 2
    assert DEFAULT_MAP["units"] == "m"
    assert isinstance(DEFAULT_MAP["floors"], list) and DEFAULT_MAP["floors"]
    f0 = DEFAULT_MAP["floors"][0]
    assert f0["level"] == 0
    assert all("polygon" in r for r in f0["rooms"])


def test_load_missing_path_returns_v2_default():
    assert load_house_map("")["version"] == 2


def test_v1_rectangles_migrate_to_v2_polygons(tmp_path):
    import json
    p = tmp_path / "v1.json"
    p.write_text(json.dumps({"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}]}))
    m = load_house_map(str(p))
    assert m["version"] == 2
    floor = m["floors"][0]
    assert floor["level"] == 0
    room = floor["rooms"][0]
    assert room["name"] == "sala"
    # rectangle -> closed polygon corners (x,y)-(x+w,y)-(x+w,y+h)-(x,y+h)
    assert room["polygon"] == [[0, 0], [4, 0], [4, 3], [0, 3]]


def test_malformed_falls_back_to_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    assert load_house_map(str(p)) == DEFAULT_MAP


def test_room_names_flattens_v2_across_floors():
    house = {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0, "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[1,0],[1,1]]}], "walls": [], "features": [], "backdrop": None},
        {"id": "f1", "name": "1", "level": 1, "rooms": [{"id": "r2", "name": "quarto", "polygon": [[0,0],[1,0],[1,1]]}], "walls": [], "features": [], "backdrop": None},
    ]}
    assert room_names(house) == ["sala", "quarto"]


def test_room_names_tolerates_v1():
    assert room_names({"rooms": [{"name": "sala"}]}) == ["sala"]
