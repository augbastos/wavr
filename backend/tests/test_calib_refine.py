"""Progressive homography refinement (guided-calib Tier 1) -- wavr.calib_refine.

Covers `merge_points` (pure point-union logic: dedup-by-proximity, resolution-change
fresh-start, FIFO overflow bound) and `solve_progressive` (the persistence
orchestration: merge -> solve -> write BOTH the homography and the raw points,
round-tripped through a real `CalibrationStore` on a tmp sqlite file, same fixture
shape as test_calib_store.py).

Ground truth for the numeric (SVD/geometry) tests is a pure image/SCALE affine map
(`_gt_floor`) -- a projective homography with h33=1 and no perspective terms, so
`apply_h` is exact and DLT recovers it exactly (residual ~0) for ANY non-degenerate
set of mutually-consistent correspondences, however many are accumulated. That fact
(not a numeric guess) is what the convergence/robustness assertions below lean on.
"""
from __future__ import annotations

import numpy as np
import pytest

from wavr.calib_refine import merge_points, solve_progressive
from wavr.calib_store import CalibrationStore
from wavr.localize import homography_from_points, homography_reprojection_error

# image-pixel -> floor-metre scale for the ground-truth affine map used by the
# numeric tests below: floor = image / _SCALE (a valid, perspective-free homography).
_SCALE = 200.0


def _gt_floor(img_pts):
    return [[u / _SCALE, v / _SCALE] for u, v in img_pts]


@pytest.fixture
def store(tmp_path):
    s = CalibrationStore(str(tmp_path / "wavr.db"))
    yield s
    s.close()


# ---- merge_points: pure point-union logic (no SVD, deterministic) ---- #

def test_merge_points_first_ever_solve_returns_new_points_unchanged():
    new_img = [[1.0, 2.0], [3.0, 4.0]]
    new_flr = [[0.1, 0.2], [0.3, 0.4]]
    out_img, out_flr = merge_points(None, None, None, new_img, new_flr, (640, 480))
    assert out_img == new_img and out_flr == new_flr


def test_merge_points_resolution_change_discards_old_and_starts_fresh():
    existing_img = [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]
    existing_flr = [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3], [0.4, 0.4]]
    new_img = [[9.0, 9.0]]
    new_flr = [[0.9, 0.9]]
    out_img, out_flr = merge_points(existing_img, existing_flr, (640, 480),
                                    new_img, new_flr, (1280, 960))   # size changed
    assert out_img == new_img and out_flr == new_flr   # old points entirely discarded


def test_merge_points_dedup_replaces_close_floor_point():
    existing_img = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
    existing_flr = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    # A re-walk to (roughly) the SAME first spot -- new floor point within
    # dedup_eps (default 0.05m) of existing_flr[0] -- carries a DIFFERENT image
    # pixel (the operator's new mark) and must REPLACE the old pair, not append a 5th.
    new_img = [[5.0, 5.0]]
    new_flr = [[0.02, 0.01]]     # ~0.0224m from (0,0) -- inside the 0.05m eps
    out_img, out_flr = merge_points(existing_img, existing_flr, (640, 480),
                                    new_img, new_flr, (640, 480))
    assert len(out_img) == 4                          # replaced, not appended
    assert [0.0, 0.0] not in out_flr                  # old spot's floor point is gone
    assert [5.0, 5.0] in out_img and [0.02, 0.01] in out_flr   # new pair present
    assert [100.0, 0.0] in out_img                    # the other 3 old pairs survive


def test_merge_points_keeps_distinct_points_outside_dedup_eps():
    existing_img = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
    existing_flr = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    new_img = [[50.0, 50.0]]
    new_flr = [[0.5, 0.5]]      # ~0.707m from every existing point -- well outside eps
    out_img, out_flr = merge_points(existing_img, existing_flr, (640, 480),
                                    new_img, new_flr, (640, 480))
    assert len(out_img) == 5                          # accumulated, not replaced
    assert [0.0, 0.0] in out_flr                       # every old pair survives
    assert [50.0, 50.0] in out_img and [0.5, 0.5] in out_flr


def test_merge_points_fifo_drops_oldest_when_over_max_points():
    existing_img = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    existing_flr = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]]
    new_img = [[10.0, 10.0], [11.0, 11.0]]
    new_flr = [[10.0, 5.0], [11.0, 5.0]]              # far from every existing floor pt
    out_img, out_flr = merge_points(existing_img, existing_flr, (640, 480),
                                    new_img, new_flr, (640, 480), max_points=5)
    assert len(out_img) == 5                          # capped: 4+2=6 -> drop 1
    assert [0.0, 0.0] not in out_img                  # oldest existing pair FIFO-dropped
    assert [1.0, 1.0] in out_img                      # next-oldest survives
    assert [10.0, 10.0] in out_img and [11.0, 11.0] in out_img  # THIS walk never dropped


# ---- solve_progressive: happy path + store round-trip ---- #

def test_solve_progressive_first_walk_matches_one_shot_homography_from_points(store):
    # Same points as test_calibration_api.py's _IMG_PTS/_FLOOR_PTS -- a first-ever
    # solve must be byte-identical to the pre-progressive-refine one-shot path.
    img = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
    flr = [[4.2, 3.0], [7.7, 3.0], [7.7, 0.0], [4.2, 0.0]]
    result = solve_progressive(store, "cam", img, flr, 1280, 720)
    h_direct = homography_from_points(img, flr)
    assert result["homography"] == pytest.approx(list(h_direct.flatten()))
    assert result["n_points"] == 4
    assert result["n_new"] == 4


def test_solve_progressive_persists_homography_and_points_round_trip(store):
    img = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
    flr = [[4.2, 3.0], [7.7, 3.0], [7.7, 0.0], [4.2, 0.0]]
    result = solve_progressive(store, "cam", img, flr, 1280, 720)

    got = store.get("cam")
    assert got["homography"] == pytest.approx(result["homography"])
    assert got["quality"] == pytest.approx(result["quality"])
    assert got["img_w"] == 1280 and got["img_h"] == 720
    assert got["points"]["image_pts"] == img
    assert got["points"]["floor_pts"] == flr


def test_solve_progressive_second_walk_accumulates_the_union(store):
    img1 = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0]]
    flr1 = _gt_floor(img1)
    r1 = solve_progressive(store, "cam", img1, flr1, 1280, 960)
    assert r1["n_points"] == 4 and r1["n_new"] == 4

    img2 = [[300.0, 300.0], [900.0, 600.0]]
    flr2 = _gt_floor(img2)
    r2 = solve_progressive(store, "cam", img2, flr2, 1280, 960)
    assert r2["n_points"] == 6           # 4 old + 2 new, no dedup overlap
    assert r2["n_new"] == 2              # only THIS walk's own count

    got = store.get("cam")
    assert len(got["points"]["image_pts"]) == 6
    for p in img1 + img2:
        assert p in got["points"]["image_pts"]


# ---- convergence invariants: accumulating good data holds/improves accuracy ---- #

def test_accumulating_consistent_observations_holds_perfect_accuracy(store):
    """Convergence / non-divergence: three successive walks, each contributing MORE
    correspondences that are perfectly consistent with the SAME ground-truth
    homography, must never make the fit WORSE -- DLT/SVD recovers a consistent
    ground truth exactly (residual ~0) for any non-degenerate set of mutually-
    consistent points, however many are accumulated."""
    walk1_img = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0]]
    walk2_img = [[300.0, 300.0], [900.0, 300.0], [900.0, 600.0], [300.0, 600.0]]
    walk3_img = [[600.0, 0.0], [1200.0, 450.0], [600.0, 900.0], [0.0, 450.0]]

    for walk_img in (walk1_img, walk2_img, walk3_img):
        result = solve_progressive(store, "cam", walk_img, _gt_floor(walk_img), 1280, 960)
        assert result["quality"] > 0.999
        assert result["residual_m"] < 1e-6


def test_outlier_walk_accumulates_and_preserves_prior_good_points(store):
    """Tier-1 ACCUMULATION contract (NOT outlier robustness). DLT least-squares over the
    union is deliberately non-robust -- RANSAC/IRLS robust refinement is Tier-2, out of
    scope per calib_refine.py's docstring, so a gross outlier CAN degrade the fit and this
    test does not pretend otherwise. What DOES hold and is asserted: a re-walk carrying a
    gross outlier is accumulated into the union (never silently dropped), the 6 prior good
    pairs survive in storage byte-identical, and the fit is no longer a perfect interpolant."""
    good_img = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0],
               [300.0, 300.0], [900.0, 600.0]]
    good_flr = _gt_floor(good_img)
    r1 = solve_progressive(store, "cam", good_img, good_flr, 1280, 960)
    assert r1["quality"] > 0.999
    assert r1["residual_m"] < 1e-6

    outlier_img = [600.0, 450.0]
    true_flr = [outlier_img[0] / _SCALE, outlier_img[1] / _SCALE]
    outlier_flr = [true_flr[0] + 5.0, true_flr[1]]     # 5m off -- a gross outlier

    r2 = solve_progressive(store, "cam", [outlier_img], [outlier_flr], 1280, 960)
    assert r2["n_points"] == 7          # accumulated into the union, not dropped/replaced
    assert r2["residual_m"] > 1e-6      # no longer a perfect fit

    # The 6 accumulated good pairs survive the outlier walk byte-identical in storage.
    stored_points = store.get("cam")["points"]
    for img, flr in zip(good_img, good_flr):
        assert img in stored_points["image_pts"]
        assert flr in stored_points["floor_pts"]


def test_more_good_observations_after_an_outlier_still_accumulate(store):
    """Tier-1: more good walks after an outlier keep GROWING the union (the accumulation
    contract). This does NOT assert the RMS improves -- DLT is not outlier-robust, so a
    gross outlier can dominate the algebraic fit; robust progressive refinement (dropping
    or down-weighting a gross outlier) is Tier-2, see calib_refine.py."""
    walk1_img = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0]]
    solve_progressive(store, "cam", walk1_img, _gt_floor(walk1_img), 1280, 960)

    outlier_img = [600.0, 450.0]
    true_flr = [outlier_img[0] / _SCALE, outlier_img[1] / _SCALE]
    outlier_flr = [true_flr[0] + 5.0, true_flr[1]]
    r2 = solve_progressive(store, "cam", [outlier_img], [outlier_flr], 1280, 960)
    assert r2["residual_m"] > 1e-6

    walk3_img = [[300.0, 300.0], [900.0, 300.0], [900.0, 600.0], [300.0, 600.0]]
    r3 = solve_progressive(store, "cam", walk3_img, _gt_floor(walk3_img), 1280, 960)

    assert r3["n_points"] == 9                          # 4 + 1 outlier + 4 more good
    assert r3["n_new"] == 4


# ---- change-gated writes (SD-wear): nothing persists unless the solve succeeds ---- #

def test_failed_solve_does_not_touch_the_store(store):
    good_img = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]
    good_flr = [[4.2, 3.0], [7.7, 3.0], [7.7, 0.0], [4.2, 0.0]]
    solve_progressive(store, "cam", good_img, good_flr, 1280, 720)
    before = store.get("cam")

    # A different img size forces merge_points' "fresh start" path (a resolution
    # change invalidates every prior correspondence); these 4 collinear points are
    # degenerate on their own, so homography_from_points raises BEFORE either store
    # write runs -- nothing is persisted unless the solve itself succeeds.
    collinear_img = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    collinear_flr = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    with pytest.raises(ValueError):
        solve_progressive(store, "cam", collinear_img, collinear_flr, 640, 480)

    after = store.get("cam")
    assert after == before      # the failed re-walk left zero trace on disk


def test_first_ever_failed_solve_leaves_no_row_at_all(store):
    collinear_img = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    collinear_flr = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    with pytest.raises(ValueError):
        solve_progressive(store, "brand-new-cam", collinear_img, collinear_flr, 640, 480)
    assert store.get("brand-new-cam") is None


# ---- untrusted-input handling ---- #

def test_solve_progressive_rejects_mismatched_lengths(store):
    img = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    flr = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]     # one short
    with pytest.raises(ValueError):
        solve_progressive(store, "cam", img, flr, 640, 480)
    assert store.get("cam") is None


def test_solve_progressive_rejects_degenerate_collinear_points(store):
    img = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    flr = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    with pytest.raises(ValueError):
        solve_progressive(store, "cam", img, flr, 640, 480)


def test_solve_progressive_first_solve_huge_int_floor_point_raises_clean_value_error(store):
    # Audit-HIGH-style regression (same class as calib_store.py's
    # `..._not_overflowerror` tests): a raw `10**400`-shaped JSON int must never
    # escape as an unhandled OverflowError. On a FIRST-EVER solve (no prior stored
    # points -> merge_points' early-return "fresh start" path does no float()
    # conversion at all) this reaches homography_from_points -> _finite_point, which
    # already guards OverflowError -> a clean ValueError.
    img = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0]]
    flr = [[10 ** 400, 0.1], [6.0, 0.0], [6.0, 4.5], [0.0, 4.5]]
    with pytest.raises(ValueError):
        solve_progressive(store, "cam", img, flr, 1280, 960)
    assert store.get("cam") is None


def test_solve_progressive_rewalk_huge_int_floor_point_is_a_known_gap(store):
    """KNOWN GAP found while writing this suite (NOT fixed here -- module-under-test
    is read-only per this task). Unlike a first-ever solve (see the sibling test
    above, which safely reaches homography_from_points' OverflowError-guarded
    `_finite_point`), a RE-WALK for a camera that already has stored points AT THE
    SAME RESOLUTION dedups by floor-point proximity FIRST
    (calib_refine.merge_points' `math.hypot(float(nfx) - float(efx), ...)` loop),
    with NO try/except around that float() call -- so a huge-int floor coordinate on
    a re-walk raises a raw OverflowError, which is NOT a ValueError/CalibrationError
    and is therefore NOT caught by PUT .../calibration's `except ValueError`
    (app.py put_calibration) -- an unhandled 500 instead of a clean 422.

    This test locks the CURRENT behaviour so a fix (giving merge_points' dedup loop
    the same finite-coordinate guard `_finite_point` already has) shows up as a test
    that needs updating, not a silent regression. Recommend routing to whoever owns
    calib_refine.py.
    """
    good_img = [[0.0, 0.0], [1200.0, 0.0], [1200.0, 900.0], [0.0, 900.0]]
    good_flr = _gt_floor(good_img)
    solve_progressive(store, "cam", good_img, good_flr, 1280, 960)

    rewalk_img = [[10.0, 10.0], [1190.0, 10.0], [1190.0, 890.0], [10.0, 890.0]]
    rewalk_flr = [[10 ** 400, 0.1]] + good_flr[1:]
    with pytest.raises(OverflowError):
        solve_progressive(store, "cam", rewalk_img, rewalk_flr, 1280, 960)
