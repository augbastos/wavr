from wavr.housemap import DEFAULT_MAP, load_house_map, room_names, room_polygon


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


def test_room_polygon_returns_named_polygon():
    poly = room_polygon(DEFAULT_MAP, "quarto")
    assert poly == [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]


def test_room_polygon_unknown_room_is_none():
    assert room_polygon(DEFAULT_MAP, "no-such-room") is None


def test_room_polygon_level_filter():
    house = {"floors": [
        {"level": 0, "rooms": [{"name": "sala", "polygon": [[0, 0], [1, 0], [1, 1]]}]},
        {"level": 1, "rooms": [{"name": "loft", "polygon": [[0, 0], [2, 0], [2, 2]]}]},
    ]}
    assert room_polygon(house, "loft", level=1) == [[0, 0], [2, 0], [2, 2]]
    assert room_polygon(house, "loft", level=0) is None      # wrong level -> not found


def test_room_polygon_rejects_degenerate_polygon():
    house = {"floors": [{"level": 0, "rooms": [
        {"name": "line", "polygon": [[0, 0], [1, 1]]}]}]}    # < 3 vertices
    assert room_polygon(house, "line") is None


# Task 2: Validation tests
import pytest
from wavr.housemap import validate_house_map, HouseMapError, MAX_STR_LEN


def _valid():
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala", "polygon": [[0,0],[4,0],[4,3],[0,3]]}],
         "walls": [{"id": "w1", "a": [4,0], "b": [4,3]}],
         "features": [{"id": "s1", "type": "stairs", "at": [3.5,2.5], "to_level": 1}],
         "backdrop": None}]}


def test_default_map_validates():
    validate_house_map(DEFAULT_MAP)          # must not raise


def test_valid_doc_passes():
    validate_house_map(_valid())


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.update(version=1), "version"),
    (lambda d: d.update(units="ft"), "units"),
    (lambda d: d.update(floors=[]), "floors"),
    (lambda d: d["floors"].append(dict(d["floors"][0])), "level"),           # duplicate level
    (lambda d: d["floors"][0]["rooms"][0].update(polygon=[[0,0],[1,1]]), "polygon"),  # <3 verts
    (lambda d: d["floors"][0]["rooms"][0]["polygon"].__setitem__(0, ["x", 0]), "finite"),
    (lambda d: d["floors"][0]["walls"][0].update(a=[float("inf"), 0]), "finite"),
    (lambda d: d["floors"][0]["features"][0].update(type="teleporter"), "type"),
])
def test_invalid_docs_raise(mutate, msg):
    d = _valid()
    mutate(d)
    with pytest.raises(HouseMapError) as e:
        validate_house_map(d)
    assert msg in str(e.value).lower()


def test_over_cap_rooms_raise():
    d = _valid()
    d["floors"][0]["rooms"] = [{"id": f"r{i}", "name": str(i), "polygon": [[0,0],[1,0],[1,1]]} for i in range(513)]
    with pytest.raises(HouseMapError):
        validate_house_map(d)


def test_non_list_features_raise_housemaperror():
    for bad in (None, 5, "x"):
        d = _valid()
        d["floors"][0]["features"] = bad
        with pytest.raises(HouseMapError):
            validate_house_map(d)


# Task 3: Point-in-polygon room assignment
from wavr.housemap import room_at


def _house_L():
    # concave (L-shaped) room on level 0
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0, "walls": [], "features": [], "backdrop": None,
         "rooms": [{"id": "r1", "name": "L", "polygon": [[0,0],[4,0],[4,2],[2,2],[2,4],[0,4]]}]}]}


def test_point_inside_polygon():
    assert room_at(_house_L(), 0, 1.0, 1.0) == "L"


def test_point_in_concave_notch_is_outside():
    # (3,3) is in the cut-out notch of the L -> not inside
    assert room_at(_house_L(), 0, 3.0, 3.0) is None


def test_point_outside_polygon():
    assert room_at(_house_L(), 0, 10.0, 10.0) is None


def test_unknown_floor_returns_none():
    assert room_at(_house_L(), 5, 1.0, 1.0) is None


# Task 4: Atomic writer save_house_map
from wavr.housemap import save_house_map


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "house.json"
    doc = _valid()
    save_house_map(str(p), doc)
    assert load_house_map(str(p)) == doc


def test_save_rejects_invalid_and_writes_nothing(tmp_path):
    p = tmp_path / "house.json"
    bad = _valid(); bad["version"] = 1
    with pytest.raises(HouseMapError):
        save_house_map(str(p), bad)
    assert not p.exists()


def test_save_empty_path_raises(tmp_path):
    with pytest.raises(HouseMapError):
        save_house_map("", _valid())


# === Fix 5: size/unknown-key bounds on validate_house_map ==========================

def test_editor_emitted_doc_still_accepted():
    # The exact shape frontend/index.html's DEMO_HOUSE / saveHouseDoc() emits (mirrors
    # DEFAULT_MAP): top-level {version,units,floors}, floor {id,name,level,rooms,walls,
    # features,backdrop}, room {id,name,polygon}. Must NOT be broken by the new bounds.
    validate_house_map(DEFAULT_MAP)
    validate_house_map(_valid())


def test_backdrop_none_accepted():
    d = _valid()
    d["floors"][0]["backdrop"] = None
    validate_house_map(d)   # must not raise


def test_backdrop_small_dict_accepted():
    d = _valid()
    d["floors"][0]["backdrop"] = {"image_ref": "backdrop.png", "m_per_px": 0.01,
                                   "offset": [0, 0], "opacity": 0.5}
    validate_house_map(d)   # reserved Phase-2 shape, small -> must not raise


def test_backdrop_oversized_dict_rejected():
    d = _valid()
    d["floors"][0]["backdrop"] = {"image_ref": "x" * 20000}
    with pytest.raises(HouseMapError):
        validate_house_map(d)


@pytest.mark.parametrize("path", [
    lambda d: d["floors"][0],                 # floor id/name
    lambda d: d["floors"][0]["rooms"][0],      # room id/name
])
def test_over_long_id_rejected(path):
    d = _valid()
    path(d)["id"] = "x" * 500
    with pytest.raises(HouseMapError) as e:
        validate_house_map(d)
    assert "id" in str(e.value).lower()


@pytest.mark.parametrize("path", [
    lambda d: d["floors"][0],                 # floor id/name
    lambda d: d["floors"][0]["rooms"][0],      # room id/name
])
def test_over_long_name_rejected(path):
    d = _valid()
    path(d)["name"] = "x" * 500
    with pytest.raises(HouseMapError) as e:
        validate_house_map(d)
    assert "name" in str(e.value).lower()


def test_huge_doc_rejected():
    # Individually-small-but-valid rooms (id/name each right at the per-field cap),
    # spread across many floors -- neither MAX_FLOORS (64) nor MAX_ROOMS_PER_FLOOR
    # (512) is exceeded on its own, so only the whole-doc byte guard catches this.
    d = _valid()
    room_tpl = dict(d["floors"][0]["rooms"][0], id="r" * MAX_STR_LEN, name="n" * MAX_STR_LEN)
    big_rooms = [dict(room_tpl) for _ in range(500)]
    d["floors"] = [dict(d["floors"][0], id=f"f{fi}", level=fi, rooms=big_rooms)
                   for fi in range(60)]
    with pytest.raises(HouseMapError):
        validate_house_map(d)


def test_unknown_top_level_key_rejected():
    d = _valid()
    d["extra"] = "nope"
    with pytest.raises(HouseMapError) as e:
        validate_house_map(d)
    assert "unknown" in str(e.value).lower()


def test_unknown_floor_key_rejected():
    d = _valid()
    d["floors"][0]["extra"] = "nope"
    with pytest.raises(HouseMapError):
        validate_house_map(d)


def test_unknown_room_key_rejected():
    d = _valid()
    d["floors"][0]["rooms"][0]["extra"] = "nope"
    with pytest.raises(HouseMapError):
        validate_house_map(d)


# === F2: upsert_room merge helper (phone "medir com o celular") =====================
import copy
from wavr.housemap import upsert_room


def _house_multi():
    # two rooms + walls/features/backdrop-shaped extras on level 0, so a merge that
    # wipes siblings or floor extras is caught.
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala",   "polygon": [[0,0],[4,0],[4,3],[0,3]]},
                   {"id": "r2", "name": "quarto", "polygon": [[5,0],[8,0],[8,3],[5,3]]}],
         "walls": [{"id": "w1", "a": [4,0], "b": [4,3]}],
         "features": [{"id": "s1", "type": "stairs", "at": [3.5,2.5], "to_level": 1}],
         "backdrop": None}]}


def test_upsert_new_room_keeps_siblings_and_floor_extras():
    house = _house_multi()
    merged = upsert_room(house, 0, {"name": "cozinha", "polygon": [[9,0],[12,0],[12,3],[9,3]]})
    f0 = merged["floors"][0]
    assert [r["name"] for r in f0["rooms"]] == ["sala", "quarto", "cozinha"]
    assert f0["walls"] == house["floors"][0]["walls"]          # walls untouched
    assert f0["features"] == house["floors"][0]["features"]    # features untouched
    assert f0["backdrop"] is None                               # backdrop untouched
    ids = [r["id"] for r in f0["rooms"]]
    assert len(ids) == len(set(ids))                            # unique room ids


def test_upsert_existing_name_replaces_polygon_and_keeps_id():
    house = _house_multi()
    orig_id = house["floors"][0]["rooms"][0]["id"]              # r1 (sala)
    new_poly = [[0,0],[6,0],[6,4],[0,4]]
    merged = upsert_room(house, 0, {"name": "sala", "polygon": new_poly})
    f0 = merged["floors"][0]
    assert [r["name"] for r in f0["rooms"]] == ["sala", "quarto"]   # replaced, not appended
    sala = next(r for r in f0["rooms"] if r["name"] == "sala")
    assert sala["id"] == orig_id                               # id preserved
    assert sala["polygon"] == new_poly                         # geometry replaced


def test_upsert_new_level_creates_floor_leaving_existing_alone():
    house = _house_multi()
    merged = upsert_room(house, 2, {"name": "sotao", "polygon": [[0,0],[3,0],[3,3],[0,3]]})
    assert [f["level"] for f in merged["floors"]] == [0, 2]
    fids = [f["id"] for f in merged["floors"]]
    assert len(fids) == len(set(fids))                         # unique floor ids
    new_floor = next(f for f in merged["floors"] if f["level"] == 2)
    assert [r["name"] for r in new_floor["rooms"]] == ["sotao"]
    assert new_floor["walls"] == [] and new_floor["features"] == []
    assert [r["name"] for r in merged["floors"][0]["rooms"]] == ["sala", "quarto"]


def test_upsert_does_not_mutate_input_house():
    house = _house_multi()
    snapshot = copy.deepcopy(house)
    upsert_room(house, 0, {"name": "nova", "polygon": [[0,0],[1,0],[1,1]]})
    assert house == snapshot                                   # deepcopy proof


def test_upsert_merged_doc_passes_validation():
    house = _house_multi()
    merged = upsert_room(house, 1, {"name": "quarto2", "polygon": [[0,0],[3,0],[3,3],[0,3]]})
    validate_house_map(merged)                                 # must not raise


def test_upsert_degenerate_polygon_fails_validation():
    # upsert itself does not validate; the merged doc fails the shared validator (the
    # exact path the endpoint takes via save_house_map -> HouseMapError -> 422).
    for bad in ([[0,0],[1,1]], [["x",0],[1,0],[1,1]]):         # <3 verts, non-finite
        merged = upsert_room(_house_multi(), 0, {"name": "ruim", "polygon": bad})
        with pytest.raises(HouseMapError):
            validate_house_map(merged)


def test_upsert_generated_floor_id_avoids_collision():
    # existing level-0 floor carries a custom id "f2"; upserting a NEW level 2 wants id
    # "f2" too -> the helper must pick a distinct id so validate (unique floor ids) passes.
    house = {"version": 2, "units": "m", "floors": [
        {"id": "f2", "name": "T", "level": 0, "rooms": [], "walls": [], "features": [], "backdrop": None}]}
    merged = upsert_room(house, 2, {"name": "x", "polygon": [[0,0],[3,0],[3,3],[0,3]]})
    fids = [f["id"] for f in merged["floors"]]
    assert fids[0] == "f2" and fids[1] != "f2"
    assert len(fids) == len(set(fids))
    validate_house_map(merged)                                 # unique ids -> passes
