"""Progressive homography refinement across multiple guided-calibration walks
(Tier 1 of the guided-calib design). Pure geometry + one persistence-orchestration
function -- no I/O beyond calling into the `CalibrationStore` it is handed.

WHY: a single walk-to-calibrate session already gives >=4 correspondences (room
centroid + polygon corners, `localize.floor_spots_for_room`), which
`localize.homography_from_points` solves exactly (DLT/SVD). Re-walking the SAME
room across separate sessions gives MORE correspondences -- a genuinely
over-determined fit that lowers the reprojection residual
(`localize.homography_reprojection_error`), instead of just re-solving the same
5-8 points every time. This module accumulates those RAW correspondence pairs
(persisted via `CalibrationStore.set_points`) and re-solves the UNION on every walk.

Passive drift detection (Tier 2, an honest hint only -- no new ground truth exists
passively) is a SEPARATE, not-yet-wired concern and out of scope here; see the
design's cross-boundary flags (sensor-fusion-architect for any fusion-trust
interaction, privacy-compliance-license-auditor for the always-on-during-normal-use
hook once it lands).
"""
from __future__ import annotations

import math

from wavr.calib_store import CalibrationStore
from wavr.localize import (
    homography_from_points,
    homography_quality,
    homography_reprojection_error,
)

# Same epsilon convention as `localize.floor_spots_for_room`'s `dedup_eps`: two
# floor points within this many metres are "the same spot", so re-walking a spot
# REPLACES its old correspondence rather than piling up near-duplicates that add no
# real geometric information.
_DEDUP_EPS_M = 0.05
# Bound how many correspondences a progressive solve ever carries -- mirrors
# `CalibrationStore._MAX_POINTS` (the persistence-shape cap); a room re-walked every
# day forever must not grow this without bound.
_MAX_POINTS = 200


def merge_points(existing_image_pts, existing_floor_pts, existing_size,
                 new_image_pts, new_floor_pts, new_size,
                 *, max_points: int = _MAX_POINTS,
                 dedup_eps_m: float = _DEDUP_EPS_M) -> tuple[list, list]:
    """Union a camera's EXISTING stored correspondences with a NEW walk's points.

    A resolution change invalidates every prior correspondence (same invariant as
    `localize._frame_size_matches`: a homography/point is tied to the pixel size it
    was marked at) -- so when `existing_size != new_size` (or there is no existing
    set), this returns the NEW points UNCHANGED: a fresh start, not a merge. This is
    what keeps a camera's first-ever solve byte-identical to today's one-shot path.

    Otherwise, pairs are unioned by FLOOR-point proximity: a new pair within
    `dedup_eps_m` metres of an existing floor point REPLACES it (the person walked
    to roughly the same spot again -- newest measurement wins, since a repeat walk
    is presumably a deliberate re-calibration). Non-replaced existing pairs are kept;
    if the union still exceeds `max_points`, the OLDEST non-replaced pairs are
    dropped first (FIFO) so a long-lived camera's point set stays bounded without
    ever discarding data from THIS walk.
    """
    if (existing_image_pts is None or existing_floor_pts is None
            or existing_size is None or new_size is None
            or existing_size[0] != new_size[0] or existing_size[1] != new_size[1]):
        return (list(new_image_pts), list(new_floor_pts))

    # Which existing pairs are replaced by a close-enough new floor point.
    replaced = [False] * len(existing_floor_pts)
    for nfx, nfy in new_floor_pts:
        for i, (efx, efy) in enumerate(existing_floor_pts):
            if replaced[i]:
                continue
            if math.hypot(float(nfx) - float(efx), float(nfy) - float(efy)) <= dedup_eps_m:
                replaced[i] = True

    kept_image = [p for p, r in zip(existing_image_pts, replaced) if not r]
    kept_floor = [p for p, r in zip(existing_floor_pts, replaced) if not r]

    out_image = kept_image + list(new_image_pts)
    out_floor = kept_floor + list(new_floor_pts)

    overflow = len(out_image) - max_points
    if overflow > 0:
        # FIFO-drop the oldest pairs first -- `kept_*` (existing, oldest) sits at the
        # front of the list, `new_*` (THIS walk, freshest) is always at the tail and
        # is therefore never dropped by this slice.
        out_image = out_image[overflow:]
        out_floor = out_floor[overflow:]

    return out_image, out_floor


def solve_progressive(store: CalibrationStore, name: str, new_image_pts, new_floor_pts,
                      img_w: int, img_h: int) -> dict:
    """Merge `name`'s stored correspondences with this walk's new ones, solve the
    homography over the union, persist BOTH the solved matrix (`set_homography`,
    what `localize()` actually consumes) and the raw union (`set_points`, what the
    NEXT walk merges against), and return the solve result.

    First-ever solve for a camera (no stored points, or a resolution change) is
    BYTE-IDENTICAL to today's one-shot path: `merge_points` returns the new points
    unchanged, so this is a pure superset of the existing PUT .../calibration
    behaviour -- existing callers/tests that never re-walk see no difference.

    Raises ValueError (via `homography_from_points`) on a degenerate/malformed
    union, or `CalibrationError` (a ValueError subclass, from the store's own
    persistence-shape guards) on an out-of-range image size or an oversized point
    set -- same contract as the route's own solve path, so the caller's existing
    `except ValueError` -> 422 handling is unchanged. Nothing is persisted if the
    solve itself fails: the store is only ever written AFTER a successful solve.
    """
    prior = store.get(name)
    existing_points = prior.get("points") if prior else None
    existing_size = None
    if prior and prior.get("img_w") is not None and prior.get("img_h") is not None:
        existing_size = (prior["img_w"], prior["img_h"])
    existing_image = existing_points["image_pts"] if existing_points else None
    existing_floor = existing_points["floor_pts"] if existing_points else None

    n_new = len(new_image_pts)
    merged_image, merged_floor = merge_points(
        existing_image, existing_floor, existing_size,
        new_image_pts, new_floor_pts, (img_w, img_h))

    h = homography_from_points(merged_image, merged_floor)
    residual_m = homography_reprojection_error(h, merged_image, merged_floor)
    quality = homography_quality(residual_m)

    flat = [float(v) for v in h.flatten()]
    store.set_homography(name, flat, img_w, img_h, quality=quality)
    store.set_points(name, merged_image, merged_floor, img_w, img_h)

    return {"homography": flat, "quality": quality, "residual_m": residual_m,
            "n_points": len(merged_image), "n_new": n_new}
