"""Camera person-localization geometry (roadmap spec A).

Turns a CV *feet pixel* (bottom-centre of a YOLO person box) into a *floor point*
in the house-map's metre frame, so a camera can place a person AT an (x, y) on the
maquette rather than only flagging the room. This module is PURE GEOMETRY + numpy
(already a dep): no cv2, no frame, no I/O. It never reads or persists a video frame
(ADR-0002) -- it only ever consumes a detection coordinate + a stored calibration.

COORDINATE FRAMES (load-bearing -- everything here is metres unless named `_px`):

  * IMAGE (pixels): origin top-left, `u` right in [0, img_w], `v` down in [0, img_h].
    The person's ground contact = the FEET pixel = bottom-centre of the bbox,
    ((x1 + x2) / 2, y2).

  * FLOOR / HOUSE (metres): the SAME frame housemap.py polygons live in -- x right,
    y down, top-left origin, one flat plane per level (housemap.py:21, DEFAULT_MAP).
    Height (up) is a separate axis `h`; the floor is the plane h = 0.

  * ROOM-LOCAL (metres): what events.Target.x/y carry (events.py:9) and what the
    radar/3D map renders (frontend placeDot, index.html:3206) -- an offset from the
    room polygon's MIN corner. `to_room_local()` converts FLOOR -> ROOM-LOCAL so a
    positioned Target drops straight into the existing render seam.

WORLD AXES for the monocular ray (right-handed): X = floor-x, Y = floor-y, Z = up.
A camera sits at C = (pos_x, pos_y, height); its optical axis has azimuth `yaw`
(radians in the floor plane, 0 = +X, turning toward +Y) and depression `tilt`
(radians below horizontal, 0 = level, pi/2 = straight down).

ACCURACY (be honest -- the whole point):
  * HOMOGRAPHY (accurate): a 4+-point image<->floor calibration gives a projective
    map good to a few cm for a fixed camera AT THE PIXEL SIZE IT WAS SOLVED AT --
    `homography_from_points` + `apply_h`. Its confidence is a MEASURED quality
    (`homography_reprojection_error` + `homography_quality`), not a flat constant --
    honest only past 4 correspondences (exactly 4 points make DLT an exact
    interpolant, so the residual is ~0 regardless of how well the points were marked;
    the walk-to-calibrate wizard's centroid+corners already gives 5+ for any
    quadrilateral room). `localize`/`make_localizer` also REJECT the homography path
    (falling back to a mount prior if any) when the live frame's pixel size doesn't
    match the size the calibration was solved at -- a resolution change silently
    mislocates every projected point otherwise.
  * MONOCULAR (approximate estimate): feet pixel + a mount PRIOR (height/tilt/pos/yaw
    /fov) intersected with the floor plane. Zero extra marking, but only as good as
    the prior -- a wrong tilt or mount position mislocates by tens of cm. Labelled
    `method="monocular"` and carries a LOWER positional confidence, never presented
    as exact.
  * AUTO-CALIBRATE from movement (`MovementAccumulator`): SCAFFOLD. Collects feet
    samples; honestly cannot fit a full homography from feet-only without floor
    correspondences, so it exposes samples for a future walked-path fit and does NOT
    fabricate a matrix. Convergence is NOT VERIFIED.
  * PTZ bearing (`ptz_bearing_floor_point`): the pan/tilt when a Tapo auto-track has
    the person centred == the bearing to them; bearing + monocular depth -> a floor
    point for a MOVING PTZ camera. The math is here; mapping a camera's normalized
    ONVIF pan/tilt to real radians is per-model and NOT VERIFIED on hardware.

Positional confidence returned here is a POSITION-QUALITY hint (0..1), NOT the room
trust weight -- fusion owns that (fusion.py DEFAULT_WEIGHTS). A camera puts it on
Target.confidence (per-person display), leaving SensingEvent.confidence = the
detection/presence confidence untouched, so the fused room math is unchanged.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

# Positional-quality hints per method. These scale a positioned Target's OWN
# confidence (display / per-person), never the room's fused confidence. Deliberately
# conservative for the estimate path so an uncalibrated dot never reads as certain.
# Q_HOMOGRAPHY is the FALLBACK default for a homography whose calibration-time
# reprojection residual isn't known (a pre-migration calibration row, or a homography
# handed to `localize`/`make_localizer` directly without going through
# `homography_quality`) -- the normal path (PUT /api/cameras/{name}/calibration)
# computes a REAL per-camera quality from `homography_reprojection_error` and passes
# it as `homography_quality=`, which overrides this constant.
Q_HOMOGRAPHY = 0.85
Q_MONOCULAR = 0.45

# A homography is refused if its correspondences are near-degenerate (collinear /
# coincident), detected via the smallest singular value of the DLT matrix relative
# to the largest. Below this ratio the solve is numerically meaningless.
_DEGENERATE_SVAL_RATIO = 1e-8


@dataclass(frozen=True)
class MountPose:
    """A camera's physical mounting, in the FLOOR metre frame. Editable by the
    operator (drop the camera on the map + drag its facing) -- far lighter than a
    4-point calibration, and enough for the monocular immediate estimate.

    `pos_x/pos_y` = where the camera is on the floor plan (metres).
    `height`     = lens height above the floor (metres).
    `tilt_deg`   = downward tilt below horizontal (deg; 0 level .. 90 straight down).
    `yaw_deg`    = optical-axis heading in the floor plane (deg; 0 = +x, +toward +y).
    `hfov_deg`   = horizontal field of view (deg). `vfov_deg` None -> derived from
                   the image aspect ratio at localize time.
    """
    pos_x: float
    pos_y: float
    height: float = 2.4
    tilt_deg: float = 30.0
    yaw_deg: float = 0.0
    hfov_deg: float = 90.0
    vfov_deg: float | None = None

    def to_dict(self) -> dict:
        return {"pos_x": self.pos_x, "pos_y": self.pos_y, "height": self.height,
                "tilt_deg": self.tilt_deg, "yaw_deg": self.yaw_deg,
                "hfov_deg": self.hfov_deg, "vfov_deg": self.vfov_deg}


@dataclass(frozen=True)
class LocalizeResult:
    """A floor point (metres) + how it was derived. `confidence` is a POSITION-quality
    hint (0..1), not a presence/room weight. `method` in {'homography','monocular'}."""
    x: float
    y: float
    confidence: float
    method: str

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "confidence": self.confidence,
                "method": self.method}


def _finite_point(p) -> bool:
    try:
        return len(p) == 2 and all(math.isfinite(float(c)) for c in p)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() on a raw huge-magnitude JSON int (e.g. a `10**400`-
        # shaped literal -- json.loads decodes it as an arbitrary-precision Python int,
        # not a float) can't be widened to a C double. Same class housemap._finite
        # guards; a non-finite-by-construction coordinate is just not finite, not a
        # crash -- callers (homography_from_points via PUT /api/cameras/{name}/
        # calibration) already turn a False here into a clean ValueError -> 422.
        return False


# --------------------------------------------------------------------------- #
# Path 2 -- accurate: 4+-point homography (pure DLT, numpy SVD).
# --------------------------------------------------------------------------- #

def homography_from_points(image_pts, floor_pts) -> np.ndarray:
    """Least-squares homography H (3x3) mapping IMAGE pixels -> FLOOR metres from
    >=4 correspondences, via the Direct Linear Transform (SVD, no cv2).

    Raises ValueError on: mismatched lengths, <4 points, any non-finite coord, or a
    near-degenerate (collinear / coincident) configuration whose DLT matrix is
    rank-deficient -- so a bad calibration is refused, never returned as a silent
    near-singular transform that mislocates every later projection.
    """
    if len(image_pts) != len(floor_pts):
        raise ValueError("image_pts and floor_pts must be the same length")
    if len(image_pts) < 4:
        raise ValueError("need >= 4 point correspondences for a homography")
    if not all(_finite_point(p) for p in image_pts) or \
       not all(_finite_point(p) for p in floor_pts):
        raise ValueError("all correspondence coords must be finite")

    rows = []
    for (u, v), (x, y) in zip(image_pts, floor_pts):
        u, v, x, y = float(u), float(v), float(x), float(y)
        rows.append([-u, -v, -1, 0, 0, 0, u * x, v * x, x])
        rows.append([0, 0, 0, -u, -v, -1, u * y, v * y, y])
    a = np.asarray(rows, dtype=float)
    # SVD: the homography is the right-singular vector of the smallest singular value.
    _, s, vh = np.linalg.svd(a)
    if s[0] == 0 or (s[-1] / s[0]) < _DEGENERATE_SVAL_RATIO:
        raise ValueError("degenerate correspondences (collinear/coincident)")
    h = vh[-1].reshape(3, 3)
    if h[2, 2] == 0 or not math.isfinite(h[2, 2]):
        raise ValueError("degenerate homography (h33 == 0)")
    return h / h[2, 2]


def apply_h(h: np.ndarray, u: float, v: float) -> tuple[float, float] | None:
    """Project one image pixel (u, v) to a floor point via homography `h`. Returns
    None if the projective denominator is ~0 (point maps to infinity -- a ray parallel
    to the floor), so a bad pixel yields no point rather than a garbage coordinate."""
    vec = h @ np.array([float(u), float(v), 1.0])
    w = vec[2]
    if abs(w) < 1e-12 or not math.isfinite(w):
        return None
    x, y = vec[0] / w, vec[1] / w
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return float(x), float(y)


def homography_reprojection_error(h: np.ndarray, image_pts, floor_pts) -> float:
    """RMS reprojection error, in FLOOR METRES, of homography `h` against the SAME
    correspondences it was solved from: for each (image, floor) pair, project the
    image point through `h` and measure the distance to the actual floor point. This
    is the calibration's REAL accuracy measurement (feeds `homography_quality` below)
    -- HONEST CAVEAT: with exactly 4 points DLT is an exact interpolant, so the
    residual is ~0 (float noise only) no matter how well the 4 points were actually
    marked; >4 points (the walk-to-calibrate wizard gives centroid+corners, 5+ for any
    quadrilateral room) is what makes this a meaningful over-determined fit. Returns
    math.inf for an empty input or if any correspondence projects to infinity
    (`apply_h` -> None) -- a degenerate projection is the worst possible fit, never a
    fabricated low number."""
    sq_errs = []
    for (u, v), (x, y) in zip(image_pts, floor_pts):
        p = apply_h(h, u, v)
        if p is None:
            return math.inf
        sq_errs.append((p[0] - float(x)) ** 2 + (p[1] - float(y)) ** 2)
    if not sq_errs:
        return math.inf
    return math.sqrt(sum(sq_errs) / len(sq_errs))


# A residual of 0m -> quality 1.0, halving every _QUALITY_HALF_LIFE_M of additional
# RMS error. 10cm is still a solid calibration for room-scale placement (quality
# ~0.5); 30-40cm (e.g. a badly-marked corner) drops below 0.15 -- the same order as
# the monocular path's honest inaccuracy, not a magic number.
_QUALITY_HALF_LIFE_M = 0.10


def homography_quality(residual_m: float) -> float:
    """Map a homography's RMS reprojection residual (metres, from
    `homography_reprojection_error`) to a 0..1 quality score -- a REAL measurement of
    THIS calibration, not the flat `Q_HOMOGRAPHY` constant. Exponential decay so 0
    residual -> 1.0, halving every `_QUALITY_HALF_LIFE_M`. A non-finite or negative
    residual (a degenerate/malformed fit) -> 0.0, the worst honest score, never a
    fabricated number."""
    if not math.isfinite(residual_m) or residual_m < 0:
        return 0.0
    return float(2.0 ** (-residual_m / _QUALITY_HALF_LIFE_M))


# --------------------------------------------------------------------------- #
# Path 1 -- approximate: monocular ground-plane ray from the mount prior.
# --------------------------------------------------------------------------- #

def monocular_floor_point(u: float, v: float, img_w: float, img_h: float,
                          mount: MountPose) -> tuple[float, float] | None:
    """Intersect the ray through image pixel (u, v) with the floor plane (h = 0),
    given the camera `mount` pose. APPROXIMATE -- accuracy is bounded by the prior.

    Returns None when the pixel's ray does not strike the floor in front of the
    camera (at/above the horizon, e.g. a detection whose feet are above the horizon
    line -> not physically on this floor), rather than inventing a point.
    """
    if img_w <= 0 or img_h <= 0:
        return None
    hfov = math.radians(mount.hfov_deg)
    if not (0 < hfov < math.pi):
        return None
    # Focal lengths in pixels from the FOV. vfov derived from aspect if not given.
    fx = (img_w / 2.0) / math.tan(hfov / 2.0)
    if mount.vfov_deg is not None:
        vfov = math.radians(mount.vfov_deg)
        fy = (img_h / 2.0) / math.tan(vfov / 2.0)
    else:
        fy = fx  # square pixels: same focal length in px on both axes
    cx, cy = img_w / 2.0, img_h / 2.0

    tilt = math.radians(mount.tilt_deg)
    yaw = math.radians(mount.yaw_deg)
    # Optical axis (unit): azimuth yaw in floor plane, depressed by tilt below level.
    fwd = np.array([math.cos(tilt) * math.cos(yaw),
                    math.cos(tilt) * math.sin(yaw),
                    -math.sin(tilt)])
    world_up = np.array([0.0, 0.0, 1.0])
    right = np.cross(fwd, world_up)
    n = np.linalg.norm(right)
    if n < 1e-9:
        # Looking straight up/down: pick an arbitrary horizontal right axis by yaw.
        right = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    else:
        right = right / n
    cam_down = np.cross(fwd, right)  # image +v direction (right-handed x=right,y=down,z=fwd)

    a = (float(u) - cx) / fx        # rightward pixel offset (focal units)
    b = (float(v) - cy) / fy        # downward pixel offset
    ray = fwd + a * right + b * cam_down
    rn = np.linalg.norm(ray)
    if rn < 1e-9:
        return None
    ray = ray / rn
    if ray[2] >= -1e-9:             # not pointing downward -> no floor hit ahead
        return None
    t = mount.height / (-ray[2])    # distance along ray to h = 0
    fx_m = mount.pos_x + t * ray[0]
    fy_m = mount.pos_y + t * ray[1]
    if not (math.isfinite(fx_m) and math.isfinite(fy_m)):
        return None
    return float(fx_m), float(fy_m)


# --------------------------------------------------------------------------- #
# Room-local conversion + top-level localize.
# --------------------------------------------------------------------------- #

def polygon_min_corner(poly) -> tuple[float, float]:
    """(min_x, min_y) of a room polygon -- the origin the render uses for room-local
    Target coords (frontend placeDot does room.x + t.x, index.html:3206)."""
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return (min(xs), min(ys)) if xs and ys else (0.0, 0.0)


def to_room_local(floor_xy: tuple[float, float], poly) -> tuple[float, float]:
    """FLOOR metres -> ROOM-LOCAL metres (offset from the polygon's min corner), the
    frame events.Target.x/y carries and the map renders."""
    mx, my = polygon_min_corner(poly)
    return (floor_xy[0] - mx, floor_xy[1] - my)


def _frame_size_matches(img_size, calib_size) -> bool:
    """True when the CURRENT frame's pixel dimensions equal the size the homography
    was solved at, or when no calibrated size is known (back-compat: a homography
    handed to `localize`/`make_localizer` directly, e.g. by a test or a pre-migration
    calibration row, carries no stored img_w/img_h). A homography is a projective map
    tied to specific pixel coordinates -- applying it to a frame at a DIFFERENT
    resolution (the stream's resolution changed, or the calibration was solved at a
    different capture size) silently mislocates every projected point, so a KNOWN
    mismatch means REJECT the homography path (the caller falls back to the mount
    prior if any) rather than presenting a wrong point as precise."""
    if calib_size is None or calib_size[0] is None or calib_size[1] is None:
        return True
    try:
        iw, ih = float(img_size[0]), float(img_size[1])
        cw, ch = float(calib_size[0]), float(calib_size[1])
    except (TypeError, ValueError, IndexError):
        return False
    return iw == cw and ih == ch


def localize(feet_px, img_size, *, homography: np.ndarray | None = None,
             mount: MountPose | None = None,
             homography_img_size: tuple[float, float] | None = None,
             homography_quality: float | None = None) -> LocalizeResult | None:
    """Feet pixel -> FLOOR point, preferring the accurate homography over the
    approximate monocular prior. Returns None if neither path yields a floor point
    (no calibration/mount, or the ray misses the floor). The caller converts to
    room-local via `to_room_local` before building a Target.

    `homography_img_size` is the (img_w, img_h) the homography was CALIBRATED at
    (CalibrationStore's stored size); when given and it doesn't match this call's
    `img_size` (the LIVE frame's actual pixel size), the homography path is rejected
    (falls back to `mount` if present) -- see `_frame_size_matches`. `homography_quality`
    is a per-camera MEASURED quality (0..1, from `homography_reprojection_error` +
    `homography_quality()`) that overrides the flat `Q_HOMOGRAPHY` default when given.
    """
    if not _finite_point(feet_px):
        return None
    u, v = float(feet_px[0]), float(feet_px[1])
    if homography is not None and _frame_size_matches(img_size, homography_img_size):
        p = apply_h(homography, u, v)
        if p is not None:
            q = Q_HOMOGRAPHY if homography_quality is None else float(homography_quality)
            return LocalizeResult(p[0], p[1], q, "homography")
    if mount is not None:
        try:
            img_w, img_h = float(img_size[0]), float(img_size[1])
        except (TypeError, ValueError, IndexError):
            return None
        p = monocular_floor_point(u, v, img_w, img_h, mount)
        if p is not None:
            return LocalizeResult(p[0], p[1], Q_MONOCULAR, "monocular")
    return None


def make_localizer(room_poly, *, homography=None, mount: MountPose | None = None,
                    calib_img_size: tuple | None = None,
                    homography_quality: float | None = None):
    """Build a per-camera localizer closure the camera source can call without knowing
    any geometry: ``(feet_px, img_size) -> (x, y, confidence) | None`` in ROOM-LOCAL
    metres (the frame events.Target.x/y carries + the map renders).

    Wraps `localize()` (homography preferred, monocular fallback) + `to_room_local()`.
    `homography` may be a flat 9-list (row-major, as the CalibrationStore persists), a
    3x3 array, or None; `mount` a MountPose or None. Returns None (not a localizer) when
    NEITHER a usable homography nor a mount is given -- so the camera stays room-centred
    (honest fallback), never a fabricated (0,0). The returned closure returns None for a
    feet pixel whose ray misses the floor, so a bad detection yields no point.

    `calib_img_size` is the (img_w, img_h) the homography was solved at (pass the
    CalibrationStore row's stored size) -- every call's LIVE `img_size` is checked
    against it, and the homography path is rejected (never silently mislocating) on a
    mismatch. `homography_quality` is the per-camera MEASURED quality (0..1) computed
    at calibration time; omit it to fall back to the flat `Q_HOMOGRAPHY` default."""
    h = None
    if homography is not None:
        arr = np.asarray(homography, dtype=float)
        if arr.size == 9 and np.all(np.isfinite(arr)):
            h = arr.reshape(3, 3)
    if h is None and mount is None:
        return None

    def _loc(feet_px, img_size):
        res = localize(feet_px, img_size, homography=h, mount=mount,
                       homography_img_size=calib_img_size,
                       homography_quality=homography_quality)
        if res is None:
            return None
        rx, ry = to_room_local((res.x, res.y), room_poly)
        return (rx, ry, res.confidence)

    return _loc


def floor_spots_for_room(poly, *, dedup_eps: float = 0.05):
    """KNOWN floor target spots (FLOOR metres) the walk-to-calibrate wizard guides the
    person to stand on, one at a time: the room CENTROID first, then each polygon VERTEX
    (corner) in order. The wizard pairs each spot with the FEET PIXEL captured while the
    person stands on it (GET calib-sample) to build the image<->floor correspondences
    `homography_from_points` solves.

    De-duplicates coincident / near-coincident points (within `dedup_eps` metres) so a
    later solve isn't fed a duplicate (degenerate) correspondence. A degenerate polygon
    (< 3 finite vertices) yields an empty list -- the wizard has nothing valid to guide
    to. The centroid is the vertex average: exact for the convex rooms the map editor
    produces, and only ever an approximate 'stand roughly here' guide (the CORNERS are
    the load-bearing correspondences), so a non-convex room's centroid falling slightly
    outside is harmless. Pure geometry: no frame, no I/O (ADR-0002)."""
    pts = [(float(p[0]), float(p[1])) for p in poly if _finite_point(p)]
    if len(pts) < 3:
        return []
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    out: list[tuple[float, float]] = []
    for s in [(cx, cy), *pts]:
        if not any(math.hypot(s[0] - o[0], s[1] - o[1]) <= dedup_eps for o in out):
            out.append(s)
    return out


def default_mount_for_room(poly, *, height: float = 2.4, tilt_deg: float = 30.0,
                           hfov_deg: float = 90.0) -> MountPose:
    """A sane, EDITABLE monocular prior for a freshly-added camera with zero setup:
    mount the camera at the room polygon's min corner, yaw toward the room centroid.
    Coarse by construction -- the operator refines pos/yaw by dragging on the map, or
    replaces it entirely with a 4-point homography. Honest starting point, not truth.
    """
    xs = [float(p[0]) for p in poly] or [0.0]
    ys = [float(p[1]) for p in poly] or [0.0]
    mx, my = min(xs), min(ys)
    cxr, cyr = sum(xs) / len(xs), sum(ys) / len(ys)
    yaw = math.degrees(math.atan2(cyr - my, cxr - mx))
    return MountPose(pos_x=mx, pos_y=my, height=height, tilt_deg=tilt_deg,
                     yaw_deg=yaw, hfov_deg=hfov_deg)


# --------------------------------------------------------------------------- #
# Path 3 -- auto-calibrate from movement: SCAFFOLD (honest; convergence NOT VERIFIED).
# --------------------------------------------------------------------------- #

class MovementAccumulator:
    """Collects feet-pixel samples from a walking person to (eventually) refine a
    camera's calibration from normal movement -- the 'auto at setup' path.

    HONESTY: feet-only samples do NOT by themselves constrain a full homography (no
    known floor correspondences). This scaffold therefore BOUNDS + stores samples and
    reports spatial coverage, but `refine()` returns None until a walked-path fit
    (feet assumed on the floor + room-polygon bounds as soft constraints) is built.
    It never fabricates a matrix. Convergence is NOT VERIFIED.
    """

    def __init__(self, maxlen: int = 512, min_samples: int = 40,
                 min_spread_px: float = 40.0):
        self._samples: deque = deque(maxlen=maxlen)
        self._min_samples = min_samples
        self._min_spread_px = min_spread_px

    def add(self, feet_px) -> None:
        if _finite_point(feet_px):
            self._samples.append((float(feet_px[0]), float(feet_px[1])))

    def __len__(self) -> int:
        return len(self._samples)

    def coverage(self) -> float:
        """Pixel bounding-box diagonal of the samples -- a proxy for how much of the
        frame the walker has covered (more coverage -> a better-constrained fit)."""
        if len(self._samples) < 2:
            return 0.0
        us = [s[0] for s in self._samples]
        vs = [s[1] for s in self._samples]
        return math.hypot(max(us) - min(us), max(vs) - min(vs))

    def ready(self) -> bool:
        """Enough, well-spread samples to ATTEMPT a fit. (The fit itself is a scaffold.)"""
        return len(self._samples) >= self._min_samples and \
            self.coverage() >= self._min_spread_px

    def refine(self):  # -> np.ndarray | None
        """SCAFFOLD: a walked-path homography fit would go here. Returns None (no
        fabricated matrix) until that estimator is built + verified. NOT VERIFIED."""
        return None


# --------------------------------------------------------------------------- #
# Path 4 -- PTZ bearing fusion: math present; hardware mapping NOT VERIFIED.
# --------------------------------------------------------------------------- #

def normalized_pan_tilt_to_radians(pan, tilt, *, pan_half_range_deg: float = 170.0,
                                   tilt_half_range_deg: float = 45.0
                                   ) -> tuple[float, float]:
    """SCAFFOLD (NOT VERIFIED): map a camera's NORMALIZED ONVIF pan/tilt in [-1,1]
    (ptz.parse_ptz_status) to (pan_rad, tilt_rad) for `ptz_bearing_floor_point`. The
    angular RANGE is a per-model PRIOR (defaults halve the Tapo C210's advertised
    ~340deg pan / ~90deg tilt); the true range must be read from the PTZ node
    (GetConfigurationOptions) or measured. Inputs clamped to [-1,1] first (NaN/inf->0),
    so a garbage or degree-valued reading can never yield a wild angle."""
    def _c(v) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(-1.0, min(1.0, f)) if math.isfinite(f) else 0.0
    return (math.radians(_c(pan) * pan_half_range_deg),
            math.radians(_c(tilt) * tilt_half_range_deg))


def ptz_bearing_floor_point(mount: MountPose, pan_rad: float, tilt_rad: float,
                            distance_m: float) -> tuple[float, float] | None:
    """For a PTZ camera that has centred a target (auto-track), the pan/tilt IS the
    bearing to them: project a floor point at `distance_m` along that bearing from the
    camera mount. `pan_rad` adds to the mount yaw; `tilt_rad` is depression below level.

    The GEOMETRY is exact; what is NOT VERIFIED is mapping a specific camera's
    normalized ONVIF pan/tilt status ([-1,1]) to real radians (per-model range) and
    estimating `distance_m` (monocular depth). Reading ONVIF PTZ *status* (GetStatus)
    is the missing seam in ptz.py -- see the module docstring.
    """
    if distance_m <= 0 or not math.isfinite(distance_m):
        return None
    yaw = math.radians(mount.yaw_deg) + float(pan_rad)
    tilt = float(tilt_rad)
    # Horizontal ground distance from the (tilted) slant range.
    ground = distance_m * math.cos(tilt)
    x = mount.pos_x + ground * math.cos(yaw)
    y = mount.pos_y + ground * math.sin(yaw)
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return float(x), float(y)
