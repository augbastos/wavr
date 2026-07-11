import pytest

from wavr.calib_store import (
    CalibrationError,
    CalibrationStore,
    validate_homography,
    validate_mount,
)
from wavr.localize import MountPose, homography_from_points


@pytest.fixture
def store(tmp_path):
    s = CalibrationStore(str(tmp_path / "wavr.db"))
    yield s
    s.close()


# ---- validation ---- #

def test_validate_mount_roundtrips_a_good_dict():
    m = validate_mount({"pos_x": 4.2, "pos_y": 0.0, "height": 2.4,
                        "tilt_deg": 30.0, "yaw_deg": 45.0, "hfov_deg": 90.0})
    assert isinstance(m, MountPose)
    assert (m.pos_x, m.pos_y, m.yaw_deg) == (4.2, 0.0, 45.0)


def test_validate_mount_requires_position():
    with pytest.raises(CalibrationError):
        validate_mount({"height": 2.4})


def test_validate_mount_rejects_out_of_range_angles():
    with pytest.raises(CalibrationError):
        validate_mount({"pos_x": 0, "pos_y": 0, "tilt_deg": 200})
    with pytest.raises(CalibrationError):
        validate_mount({"pos_x": 0, "pos_y": 0, "hfov_deg": 0})


def test_validate_mount_rejects_nonnumeric():
    with pytest.raises(CalibrationError):
        validate_mount({"pos_x": "x", "pos_y": 0})


def test_validate_homography_accepts_nine_finite():
    h = validate_homography([1, 0, 0, 0, 1, 0, 0, 0, 1])
    assert len(h) == 9 and h[8] == 1.0


def test_validate_homography_rejects_wrong_length():
    with pytest.raises(CalibrationError):
        validate_homography([1, 0, 0, 1])


def test_validate_homography_rejects_nonfinite_and_zero_h33():
    with pytest.raises(CalibrationError):
        validate_homography([1, 0, 0, 0, 1, 0, 0, 0, float("inf")])
    with pytest.raises(CalibrationError):
        validate_homography([1, 0, 0, 0, 1, 0, 0, 0, 0])


# Audit HIGH regression: a raw `10**400`-shaped JSON int (json.loads decodes it as an
# arbitrary-precision Python int, not a float) used to raise an unhandled OverflowError
# out of float() -- an unhandled 500 via PUT /api/cameras/{name}/calibration -- instead
# of the clean CalibrationError (422) every other malformed value already gets.
def test_validate_mount_rejects_huge_int_literal_not_overflowerror():
    with pytest.raises(CalibrationError):
        validate_mount({"pos_x": 10**400, "pos_y": 0})


def test_validate_mount_rejects_huge_int_literal_vfov_not_overflowerror():
    with pytest.raises(CalibrationError):
        validate_mount({"pos_x": 0, "pos_y": 0, "vfov_deg": 10**400})


def test_validate_homography_rejects_huge_int_literal_not_overflowerror():
    with pytest.raises(CalibrationError):
        validate_homography([10**400, 0, 0, 0, 1, 0, 0, 0, 1])


# ---- store round-trips ---- #

def test_get_unknown_camera_returns_none(store):
    assert store.get("nope") is None


def test_set_and_get_mount(store):
    store.set_mount("quarto-1", MountPose(pos_x=4.2, pos_y=0.0, yaw_deg=45.0))
    got = store.get("quarto-1")
    assert got["mount"].pos_x == 4.2
    assert got["mount"].yaw_deg == 45.0
    assert got["homography"] is None


def test_set_homography_from_solved_matrix(store):
    img = [(0, 0), (640, 0), (640, 480), (0, 480)]
    flr = [(0, 0), (4, 0), (4, 3), (0, 3)]
    h = homography_from_points(img, flr)
    store.set_homography("quintal", list(h.flatten()), 640, 480)
    got = store.get("quintal")
    assert got["homography"] is not None and len(got["homography"]) == 9
    assert got["img_w"] == 640 and got["img_h"] == 480


def test_mount_and_homography_coexist(store):
    store.set_mount("cam", MountPose(pos_x=1.0, pos_y=2.0))
    store.set_homography("cam", [1, 0, 0, 0, 1, 0, 0, 0, 1], 320, 240)
    got = store.get("cam")
    assert got["mount"].pos_x == 1.0           # mount preserved by the homography upsert
    assert got["homography"][0] == 1.0
    assert got["img_w"] == 320


def test_set_homography_rejects_bad_image_size(store):
    with pytest.raises(CalibrationError):
        store.set_homography("cam", [1, 0, 0, 0, 1, 0, 0, 0, 1], 0, 240)


def test_delete(store):
    store.set_mount("cam", MountPose(pos_x=0.0, pos_y=0.0))
    assert store.delete("cam") is True
    assert store.get("cam") is None
    assert store.delete("cam") is False


def test_corrupt_blob_degrades_to_none(store):
    store.set_mount("cam", MountPose(pos_x=0.0, pos_y=0.0))
    # Simulate a corrupt row: overwrite mount_json with junk directly.
    store._conn.execute("UPDATE camera_calib SET mount_json = ? WHERE name = ?",
                        ("{not json", "cam"))
    store._conn.commit()
    got = store.get("cam")
    assert got is not None and got["mount"] is None   # one bad row never raises
