"""Ephemeral in-memory feet-pixel sink for the walk-to-calibrate wizard (Spec A).

The guided setup has the operator WALK to known floor spots (room centre, each corner);
at each spot the camera detects the person and the wizard pairs the KNOWN floor point
with the person's FEET PIXEL (bottom-centre of the YOLO box) to build a homography
correspondence -- the person's body IS the calibration marker, so no snapshot is ever
taken (that is the whole point vs the deferred 4-point-on-a-snapshot path, ADR-0002).

ADR-0002 (load-bearing): this store holds ONLY a DETECTION COORDINATE (the feet pixel)
+ the image DIMENSIONS + the detection CONFIDENCE -- NEVER a frame, crop, or any pixel
data. Each sample is computed from a frame that is then discarded; nothing here is
persisted (no DB, no disk). It is the read side of GET /api/cameras/{name}/calib-sample.

Samples EXPIRE (`max_age_s`): the wizard must capture the person's CURRENT position, so
a stale feet pixel from before they moved reads as "no sample" (None) rather than a
ghost that would silently corrupt the calibration.
"""
from __future__ import annotations

import math
import threading
import time

# Cap the cameras tracked at once. In practice there is one live calibration session at
# a time; this bounds memory if many cameras churn through sessions without cleanup.
_MAX_CAMERAS = 64
# How long a recorded feet pixel stays "current" before it reads as stale (None).
_DEFAULT_MAX_AGE_S = 2.0


class CalibSampleStore:
    """Latest feet PIXEL per camera during a walk-to-calibrate session. Coordinate-only,
    in-memory, TTL'd, thread-safe (the recorder runs in a to_thread detection worker;
    the reader is the request thread). NEVER a frame (ADR-0002)."""

    def __init__(self, max_age_s: float = _DEFAULT_MAX_AGE_S,
                 max_cameras: int = _MAX_CAMERAS):
        self._max_age = float(max_age_s)
        self._max = max_cameras
        self._lock = threading.Lock()
        self._samples: dict[str, dict] = {}

    def record(self, name: str, feet_px, img_w, img_h, confidence) -> None:
        """Store a camera's latest feet pixel + image dims + detection confidence.
        Silently DROPS a non-finite / malformed / non-positive-size sample, so a bad
        detection never corrupts the store or a later correspondence. Coordinate +
        scalars only -- no frame is accepted or kept (ADR-0002)."""
        try:
            u, v = float(feet_px[0]), float(feet_px[1])
            w, h = float(img_w), float(img_h)
            c = float(confidence)
        except (TypeError, ValueError, IndexError):
            return
        if not all(math.isfinite(x) for x in (u, v, w, h, c)):
            return
        if w <= 0 or h <= 0:
            return
        with self._lock:
            self._samples[name] = {"feet_px": (u, v), "img_w": w, "img_h": h,
                                   "confidence": c, "ts": time.monotonic()}
            # Bound memory: evict the oldest-recorded camera if over the cap.
            while len(self._samples) > self._max:
                oldest = min(self._samples, key=lambda k: self._samples[k]["ts"])
                self._samples.pop(oldest, None)

    def latest(self, name: str, max_age_s: float | None = None) -> dict | None:
        """The camera's latest feet pixel IF still fresh, else None. Returns a plain
        dict {feet_px:(u,v), img_w, img_h, confidence, age_s} -- coordinate + scalars,
        NEVER a frame. A sample older than `max_age_s` reads as None so the wizard can
        never capture a ghost position from before the walker moved."""
        age_cap = self._max_age if max_age_s is None else float(max_age_s)
        with self._lock:
            s = self._samples.get(name)
            if s is None:
                return None
            age = time.monotonic() - s["ts"]
            if age > age_cap:
                return None
            return {"feet_px": s["feet_px"], "img_w": s["img_w"], "img_h": s["img_h"],
                    "confidence": s["confidence"], "age_s": age}

    def clear(self, name: str) -> None:
        """Drop a camera's sample -- called when a calibration session ends so a stale
        feet pixel can't linger into the next session."""
        with self._lock:
            self._samples.pop(name, None)
