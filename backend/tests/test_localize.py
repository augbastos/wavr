import math

import numpy as np
import pytest

from wavr.localize import (
    MountPose,
    MovementAccumulator,
    apply_h,
    default_mount_for_room,
    homography_from_points,
    localize,
    make_localizer,
    monocular_floor_point,
    polygon_min_corner,
    ptz_bearing_floor_point,
    to_room_local,
)


# --------------------------------------------------------------------------- #
# Homography (accurate path) -- known-point proofs + degeneracy guards.
# --------------------------------------------------------------------------- #

def test_homography_recovers_identity_square():
    # Image unit square -> floor unit square == identity; every point round-trips.
    img = [(0, 0), (1, 0), (1, 1), (0, 1)]
    flr = [(0, 0), (1, 0), (1, 1), (0, 1)]
    h = homography_from_points(img, flr)
    for (u, v) in [(0.5, 0.5), (0.25, 0.75), (1, 1)]:
        x, y = apply_h(h, u, v)
        assert x == pytest.approx(u, abs=1e-9)
        assert y == pytest.approx(v, abs=1e-9)


def test_homography_known_affine_scale_and_offset():
    # Image [0,640]x[0,480] mapped to a 4m x 3m room. Pure scale+offset.
    img = [(0, 0), (640, 0), (640, 480), (0, 480)]
    flr = [(0, 0), (4, 0), (4, 3), (0, 3)]
    h = homography_from_points(img, flr)
    x, y = apply_h(h, 320, 240)     # image centre -> room centre
    assert x == pytest.approx(2.0, abs=1e-6)
    assert y == pytest.approx(1.5, abs=1e-6)
    x, y = apply_h(h, 640, 480)     # corner -> far corner
    assert (x, y) == (pytest.approx(4.0, abs=1e-6), pytest.approx(3.0, abs=1e-6))


def test_homography_projective_keystone():
    # A trapezoid (keystone from a tilted camera) -> a rectangle. The 4 corners must
    # map exactly; a mid-edge point lands between them (projective, not linear).
    img = [(100, 400), (540, 400), (640, 100), (0, 100)]
    flr = [(0, 0), (4, 0), (4, 3), (0, 3)]
    h = homography_from_points(img, flr)
    for (u, v), (fx, fy) in zip(img, flr):
        x, y = apply_h(h, u, v)
        assert x == pytest.approx(fx, abs=1e-6)
        assert y == pytest.approx(fy, abs=1e-6)


def test_homography_rejects_too_few_points():
    with pytest.raises(ValueError):
        homography_from_points([(0, 0), (1, 0), (1, 1)], [(0, 0), (1, 0), (1, 1)])


def test_homography_rejects_length_mismatch():
    with pytest.raises(ValueError):
        homography_from_points([(0, 0), (1, 0), (1, 1), (0, 1)], [(0, 0), (1, 0)])


def test_homography_rejects_collinear_points():
    # All image points on one line -> degenerate, must be refused not silently solved.
    img = [(0, 0), (1, 1), (2, 2), (3, 3)]
    flr = [(0, 0), (1, 0), (2, 0), (3, 0)]
    with pytest.raises(ValueError):
        homography_from_points(img, flr)


def test_homography_rejects_nonfinite():
    img = [(0, 0), (1, 0), (1, float("nan")), (0, 1)]
    flr = [(0, 0), (1, 0), (1, 1), (0, 1)]
    with pytest.raises(ValueError):
        homography_from_points(img, flr)


def test_apply_h_point_at_infinity_returns_none():
    # A homography whose bottom row sends this pixel's denominator to ~0.
    h = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])
    assert apply_h(h, 0.0, 5.0) is None   # w = 1*0 + 0 + 0 = 0


# --------------------------------------------------------------------------- #
# Monocular (approximate path) -- derived-by-hand ground truth.
# --------------------------------------------------------------------------- #

def test_monocular_centre_pixel_hits_expected_floor_point():
    # Camera at origin, height 2m, looking along +x, depressed 45deg. The centre
    # pixel travels along the optical axis (0.707,0,-0.707); it strikes h=0 at
    # t = 2/0.707 = 2.828 -> floor (2, 0). Hand-derived, exact.
    mount = MountPose(pos_x=0.0, pos_y=0.0, height=2.0, tilt_deg=45.0,
                      yaw_deg=0.0, hfov_deg=90.0)
    p = monocular_floor_point(320, 240, 640, 480, mount)
    assert p is not None
    assert p[0] == pytest.approx(2.0, abs=1e-6)
    assert p[1] == pytest.approx(0.0, abs=1e-6)


def test_monocular_yaw_rotates_into_floor_frame():
    # Same camera yawed 90deg (facing +y): the centre pixel now lands on +y axis.
    mount = MountPose(pos_x=0.0, pos_y=0.0, height=2.0, tilt_deg=45.0,
                      yaw_deg=90.0, hfov_deg=90.0)
    p = monocular_floor_point(320, 240, 640, 480, mount)
    assert p is not None
    assert p[0] == pytest.approx(0.0, abs=1e-6)
    assert p[1] == pytest.approx(2.0, abs=1e-6)


def test_monocular_lower_pixel_is_nearer_than_upper():
    # Feet lower in the frame (larger v) are physically closer to the camera.
    mount = MountPose(pos_x=0.0, pos_y=0.0, height=2.4, tilt_deg=30.0, yaw_deg=0.0)
    near = monocular_floor_point(320, 460, 640, 480, mount)   # near bottom edge
    far = monocular_floor_point(320, 260, 640, 480, mount)    # nearer horizon
    assert near is not None and far is not None
    assert near[0] < far[0]     # +x = away along optical axis; nearer = smaller x


def test_monocular_above_horizon_returns_none():
    # A pixel high in the frame whose ray points at/above the horizon -> no floor hit.
    mount = MountPose(pos_x=0.0, pos_y=0.0, height=2.4, tilt_deg=5.0, yaw_deg=0.0,
                      hfov_deg=90.0, vfov_deg=90.0)
    assert monocular_floor_point(320, 0, 640, 480, mount) is None


def test_monocular_rejects_bad_image_size():
    mount = MountPose(pos_x=0.0, pos_y=0.0)
    assert monocular_floor_point(320, 240, 0, 480, mount) is None


# --------------------------------------------------------------------------- #
# Room-local conversion + top-level localize precedence.
# --------------------------------------------------------------------------- #

def test_polygon_min_corner_and_room_local():
    poly = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]   # quarto (DEFAULT_MAP)
    assert polygon_min_corner(poly) == (4.2, 0.0)
    # A floor point at (5.2, 1.0) is 1.0m right, 1.0m down inside the room.
    assert to_room_local((5.2, 1.0), poly) == pytest.approx((1.0, 1.0))


def test_localize_prefers_homography_over_mount():
    img = [(0, 0), (640, 0), (640, 480), (0, 480)]
    flr = [(0, 0), (4, 0), (4, 3), (0, 3)]
    h = homography_from_points(img, flr)
    mount = MountPose(pos_x=99.0, pos_y=99.0)   # would give a wildly different point
    res = localize((320, 240), (640, 480), homography=h, mount=mount)
    assert res is not None and res.method == "homography"
    assert res.x == pytest.approx(2.0, abs=1e-6) and res.y == pytest.approx(1.5, abs=1e-6)
    assert res.confidence > 0.8


def test_localize_falls_back_to_monocular():
    mount = MountPose(pos_x=0.0, pos_y=0.0, height=2.0, tilt_deg=45.0, yaw_deg=0.0)
    res = localize((320, 240), (640, 480), homography=None, mount=mount)
    assert res is not None and res.method == "monocular"
    assert res.x == pytest.approx(2.0, abs=1e-6)
    assert res.confidence < 0.6      # honestly lower than the homography path


def test_localize_none_without_calibration():
    assert localize((320, 240), (640, 480)) is None


def test_localize_rejects_bad_feet_point():
    mount = MountPose(pos_x=0.0, pos_y=0.0)
    assert localize((float("nan"), 240), (640, 480), mount=mount) is None


def test_default_mount_faces_room_centroid():
    poly = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]   # sala
    m = default_mount_for_room(poly)
    assert (m.pos_x, m.pos_y) == (0.0, 0.0)            # min corner
    # centroid is (2, 1.5) -> yaw = atan2(1.5, 2) ~ 36.87deg
    assert m.yaw_deg == pytest.approx(math.degrees(math.atan2(1.5, 2.0)), abs=1e-6)


# --------------------------------------------------------------------------- #
# Scaffolds -- honest, non-fabricating.
# --------------------------------------------------------------------------- #

def test_movement_accumulator_bounds_and_readiness():
    acc = MovementAccumulator(maxlen=8, min_samples=3, min_spread_px=5.0)
    assert not acc.ready()
    for i in range(20):
        acc.add((i * 10.0, i * 10.0))
    assert len(acc) == 8                    # bounded deque
    assert acc.coverage() > 5.0
    assert acc.ready()
    assert acc.refine() is None             # scaffold: never fabricates a matrix


def test_movement_accumulator_ignores_bad_samples():
    acc = MovementAccumulator()
    acc.add((float("nan"), 1.0))
    acc.add((1.0, float("inf")))
    assert len(acc) == 0


def test_ptz_bearing_projects_along_yaw():
    # Camera at (1,1) facing +x (yaw 0), target centred (pan 0), level (tilt 0),
    # 3m away -> floor point (4, 1).
    mount = MountPose(pos_x=1.0, pos_y=1.0, yaw_deg=0.0)
    p = ptz_bearing_floor_point(mount, pan_rad=0.0, tilt_rad=0.0, distance_m=3.0)
    assert p == (pytest.approx(4.0), pytest.approx(1.0))


def test_ptz_bearing_rejects_nonpositive_distance():
    mount = MountPose(pos_x=0.0, pos_y=0.0)
    assert ptz_bearing_floor_point(mount, 0.0, 0.0, 0.0) is None


# ---- make_localizer: the camera-facing closure --------------------------------

# A room offset well away from the origin so room-local != floor is observable.
_POLY = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]   # quarto; min corner (4.2, 0)


def test_make_localizer_homography_round_trips_to_room_local():
    # Identity image<->floor homography: a floor point at (5.2, 1.0) -> room-local
    # (5.2 - 4.2, 1.0 - 0.0) = (1.0, 1.0).
    img = [[0, 0], [10, 0], [10, 10], [0, 10]]
    h = homography_from_points(img, img)   # identity map pixels==floor metres
    loc = make_localizer(_POLY, homography=h)
    x, y, conf = loc((5.2, 1.0), (10, 10))
    assert (x, y) == (pytest.approx(1.0), pytest.approx(1.0))
    assert conf == pytest.approx(0.85)     # homography position-quality


def test_make_localizer_accepts_flat_nine_list():
    # The CalibrationStore persists a homography as a flat 9-list; the localizer must
    # accept that shape directly.
    img = [[0, 0], [10, 0], [10, 10], [0, 10]]
    h = homography_from_points(img, img)
    loc = make_localizer(_POLY, homography=[float(v) for v in h.flatten()])
    x, y, conf = loc((4.2, 0.0), (10, 10))
    assert (x, y) == (pytest.approx(0.0), pytest.approx(0.0))   # min corner -> local origin


def test_make_localizer_monocular_in_room():
    mount = MountPose(pos_x=4.2, pos_y=0.0, height=2.4, tilt_deg=40.0,
                      yaw_deg=45.0, hfov_deg=90.0)
    loc = make_localizer(_POLY, mount=mount)
    res = loc((640.0, 360.0), (1280, 720))   # centre pixel
    assert res is not None
    x, y, conf = res
    assert conf == pytest.approx(0.45)       # monocular position-quality
    # floor point should land ahead of the mount (>= its corner) -> non-negative local
    assert x >= -1e-9 and y >= -1e-9


def test_make_localizer_none_when_no_calibration():
    assert make_localizer(_POLY) is None      # neither homography nor mount -> no localizer


def test_make_localizer_returns_none_for_offfloor_ray():
    # A pixel above the horizon (top of frame) with a shallow tilt -> ray misses the
    # floor -> None, never a fabricated point.
    mount = MountPose(pos_x=4.2, pos_y=0.0, height=2.4, tilt_deg=5.0,
                      yaw_deg=45.0, hfov_deg=90.0)
    loc = make_localizer(_POLY, mount=mount)
    assert loc((640.0, 0.0), (1280, 720)) is None
