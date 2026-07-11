from __future__ import annotations

import json
import sqlite3

from wavr.localize import MountPose

_SCHEMA = """
CREATE TABLE IF NOT EXISTS camera_calib (
    name       TEXT PRIMARY KEY,
    mount_json TEXT,
    h_json     TEXT,
    img_w      INTEGER,
    img_h      INTEGER,
    updated    TEXT
);
"""

# Bound what a stored calibration can carry (defence-in-depth: this row is written
# from the /api/cameras/{name}/calibration route body, so the same "never trust an
# uploaded geometry blind" rule housemap.validate_house_map enforces applies here).
_MAX_JSON_BYTES = 4096
_MAX_DIM = 100_000  # a sane pixel-dimension ceiling


class CalibrationError(ValueError):
    """Raised on a structurally invalid calibration (bad mount / homography shape)."""


def validate_mount(d: dict) -> MountPose:
    """Coerce a mount-pose dict to a MountPose, rejecting non-finite / out-of-range
    values. Angles/fov are range-checked so a garbage prior can't silently mislocate.
    Returns a validated MountPose; raises CalibrationError otherwise."""
    if not isinstance(d, dict):
        raise CalibrationError("mount must be an object")

    def _num(key, default, lo, hi, *, required=False):
        if key not in d or d[key] is None:
            if required:
                raise CalibrationError(f"mount.{key} is required")
            return default
        try:
            v = float(d[key])
        except (TypeError, ValueError, OverflowError):
            # OverflowError: a raw huge-magnitude JSON int (e.g. a `10**400`-shaped
            # literal -- json.loads decodes it as an arbitrary-precision Python int,
            # not a float) can't be widened to a C double by float(). Same class
            # localize._finite_point / housemap._finite guard; treat it as "not a
            # number" -> clean CalibrationError (422 at the route), never a 500.
            raise CalibrationError(f"mount.{key} must be a number")
        if not (lo <= v <= hi):
            raise CalibrationError(f"mount.{key} out of range [{lo}, {hi}]")
        return v

    vfov = d.get("vfov_deg")
    if vfov is not None:
        try:
            vfov = float(vfov)
        except (TypeError, ValueError, OverflowError):   # see _num()'s comment above
            raise CalibrationError("mount.vfov_deg must be a number or null")
        if not (1.0 <= vfov <= 179.0):
            raise CalibrationError("mount.vfov_deg out of range [1, 179]")
    return MountPose(
        pos_x=_num("pos_x", 0.0, -10_000.0, 10_000.0, required=True),
        pos_y=_num("pos_y", 0.0, -10_000.0, 10_000.0, required=True),
        height=_num("height", 2.4, 0.05, 100.0),
        tilt_deg=_num("tilt_deg", 30.0, 0.0, 90.0),
        yaw_deg=_num("yaw_deg", 0.0, -360.0, 360.0),
        hfov_deg=_num("hfov_deg", 90.0, 1.0, 179.0),
        vfov_deg=vfov,
    )


def validate_homography(h) -> list[float]:
    """Validate a homography given as a flat list of 9 finite floats (row-major).
    Returns the normalized list; raises CalibrationError otherwise. Does NOT re-solve
    -- the route solves it via localize.homography_from_points, which already guards
    degeneracy; this is the persistence-shape guard."""
    if not isinstance(h, (list, tuple)) or len(h) != 9:
        raise CalibrationError("homography must be a list of 9 numbers (row-major 3x3)")
    out: list[float] = []
    for v in h:
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: same huge-int-literal class as validate_mount's _num()
            # above. Today's only writer (PUT /api/cameras/{name}/calibration) always
            # hands this a server-computed, already-finite matrix from
            # localize.homography_from_points -- but this is the shared persistence-
            # shape guard (also runs on CalibrationStore.get's read-back), so it stays
            # defensive against a directly-supplied homography too.
            raise CalibrationError("homography entries must be numbers")
        if f != f or f in (float("inf"), float("-inf")):
            raise CalibrationError("homography entries must be finite")
        out.append(f)
    if out[8] == 0.0:
        raise CalibrationError("homography h33 must be non-zero")
    return out


class CalibrationStore:
    """Persisted per-camera localization calibration (mount prior + optional 4-point
    homography + capture image size). Configuration, NOT runtime state and NEVER a
    frame (ADR-0002): only stored MATRICES / detection-space parameters live here.
    Shares the sqlite file with Storage/CameraStore but owns its own table.

    A camera with a row here localizes people to a floor (x, y); a camera without one
    stays room-centred (honest fallback). Keyed by camera name (FK-by-convention to
    camera_store.cameras.name)."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def set_mount(self, name: str, mount: MountPose) -> None:
        """Upsert a camera's mount prior (monocular estimate path), keeping any
        existing homography + image size."""
        blob = json.dumps(mount.to_dict())
        if len(blob) > _MAX_JSON_BYTES:
            raise CalibrationError("mount blob too large")
        self._conn.execute(
            "INSERT INTO camera_calib (name, mount_json, updated) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET mount_json=excluded.mount_json, "
            "updated=excluded.updated",
            (name, blob))
        self._conn.commit()

    def set_homography(self, name: str, h: list[float], img_w: int, img_h: int) -> None:
        """Upsert a camera's 4-point homography + the image size it was marked at."""
        clean = validate_homography(h)
        if not (0 < int(img_w) <= _MAX_DIM and 0 < int(img_h) <= _MAX_DIM):
            raise CalibrationError("image size out of range")
        blob = json.dumps(clean)
        if len(blob) > _MAX_JSON_BYTES:
            raise CalibrationError("homography blob too large")
        self._conn.execute(
            "INSERT INTO camera_calib (name, h_json, img_w, img_h, updated) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET h_json=excluded.h_json, "
            "img_w=excluded.img_w, img_h=excluded.img_h, updated=excluded.updated",
            (name, blob, int(img_w), int(img_h)))
        self._conn.commit()

    def get(self, name: str) -> dict | None:
        """Return {mount: MountPose|None, homography: list[9]|None, img_w, img_h,
        updated} for a camera, or None if it has no calibration row. A corrupt stored
        blob degrades that field to None (never raises) so one bad row can't brick
        the localizer for a healthy camera."""
        r = self._conn.execute(
            "SELECT name, mount_json, h_json, img_w, img_h, updated "
            "FROM camera_calib WHERE name = ?", (name,)).fetchone()
        if r is None:
            return None
        mount = None
        if r["mount_json"]:
            try:
                mount = validate_mount(json.loads(r["mount_json"]))
            except (ValueError, TypeError):
                mount = None
        homography = None
        if r["h_json"]:
            try:
                homography = validate_homography(json.loads(r["h_json"]))
            except (ValueError, TypeError):
                homography = None
        return {"mount": mount, "homography": homography,
                "img_w": r["img_w"], "img_h": r["img_h"], "updated": r["updated"]}

    def delete(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM camera_calib WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
