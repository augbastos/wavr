from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from wavr.events import SensingEvent, Target


@dataclass(frozen=True)
class Detection:
    count: int
    confidence: float


class CameraPrivacySignal(Exception):
    """Raised by a frame source when the RTSP session opened cleanly (the TCP/RTSP
    handshake succeeded -- the camera is reachable and the stored credentials were
    accepted) but ZERO frames were ever produced before the stream ended. Per TP-Link's
    own docs, Tapo "Privacy Mode... disables all video and audio streaming and
    recording" while the camera itself stays powered and on the network -- so a
    deliberately-covered camera is expected to look exactly like this: reachable, but
    producing no media. A genuine fault (wrong IP/creds/dead device/DHCP drift) instead
    fails to open the capture at all, so it does NOT raise this.

    CameraSource.events() catches this SEPARATELY from a generic connection error so a
    covered camera is reported as a distinct 'privacy' state, never as 'offline'/
    unhealthy -- ADR-0002 holds (never a frame in the signal), and the point of this
    signal is exactly to stop a covered camera from crying wolf.

    HONESTY NOTE (NOT VERIFIED against real hardware): TP-Link does not document the
    RTSP wire-level behaviour of Privacy Mode, and no public source conclusively
    describes it either. This is the best available signal obtainable from the RTSP
    protocol alone without guessing at Tapo's undocumented local control-plane protocol
    (see camera_privacy.py). Validate against a real Tapo camera before fully trusting
    it: enter Privacy Mode in the Tapo app and confirm Wavr reports 'privacy', not
    'offline'. If it turns out privacy mode also fails to open the RTSP session at all
    (isOpened() False), this heuristic will misclassify it as a generic fault --
    functionally identical to today's (pre-this-feature) behaviour, so it never makes
    things worse, only sometimes fails to make them better."""


def classify_posture(keypoints) -> str | None:
    """COCO-17 pixel keypoints -> coarse posture. Pure heuristic, no ML."""
    def mid(a, b):
        (ax, ay), (bx, by) = keypoints[a], keypoints[b]
        if (ax, ay) == (0.0, 0.0) or (bx, by) == (0.0, 0.0):
            return None
        return ((ax + bx) / 2, (ay + by) / 2)

    sh, hip, knee = mid(5, 6), mid(11, 12), mid(13, 14)
    if sh is None or hip is None or knee is None:
        return None
    dx, dy = hip[0] - sh[0], hip[1] - sh[1]
    torso = (dx * dx + dy * dy) ** 0.5
    if torso == 0:
        return None
    if abs(dx) > abs(dy):
        return "lying"
    if (knee[1] - hip[1]) < 0.32 * torso:
        return "sitting"
    return "standing"


class CameraSource:
    """Highest-precision source: pulls RTSP frames and runs local person detection.
    Both the frame stream and the detector are injected (real defaults lazy-import
    cv2/YOLO in Task 2). The frame generator releases the capture in its finally,
    so SourceManager.aclose() on disable is a hard RTSP kill — no frame is read or
    kept while OFF. Detection runs in a thread so a slow inference never blocks the
    loop. Only derived presence/confidence is emitted; frames never persist."""

    def __init__(self, room: str, rtsp_url: str = "",
                 frames: Callable[[str], AsyncIterator[object]] | None = None,
                 detect: Callable[[object], Detection] | None = None,
                 interval: float = 0.5, confidence: float = 0.0,
                 reconnect_delay: float = 3.0,
                 pose: bool = False,
                 pose_detect: Callable[[object, float], list[Target]] | None = None,
                 name: str = "",
                 on_health: Callable[[str, bool], None] | None = None,
                 unhealthy_secs: float = 30.0,
                 on_privacy: Callable[[str, bool], None] | None = None):
        self.room = room
        self._url = rtsp_url
        self._frames = frames or rtsp_frames
        self._confidence = confidence
        # An injected `detect` is responsible for its own thresholding — the
        # confidence param is only applied on the real (yolo_detect) path below.
        self._detect = detect or (lambda f: yolo_detect(f, self._confidence))
        self._interval = interval
        self._reconnect = reconnect_delay
        # Opt-in posture pass: off by default -> zero behavior change (Camera
        # add-form/API flag not exposed yet; enabling comes with real-camera
        # bring-up). pose_detect always takes (frame, confidence) — unlike
        # `detect`, it never needs a closure since yolo_pose_detect already
        # has that signature.
        self._pose = pose
        self._pose_detect = pose_detect or yolo_pose_detect
        # F3 camera IP-drift health hook. `on_health(name, healthy)` is edge-triggered:
        # fired once (name, False) after `unhealthy_secs` of no frame, and once
        # (name, True) on the first frame after a down report. It ever only receives
        # (name, bool) -- NEVER a frame (ADR-0002 holds). `_last_ok` is a monotonic
        # timestamp of the last yielded frame; `_down_reported` is the edge latch.
        self._name = name
        self._on_health = on_health
        self._unhealthy_secs = unhealthy_secs
        self._last_ok = 0.0
        self._down_reported = False
        # Privacy-mode state (see CameraPrivacySignal): a separate edge latch from the
        # down-latch above -- a covered camera is intentional, never counted as unhealthy.
        self._on_privacy = on_privacy
        self._privacy_reported = False

    def _emit_health(self, healthy: bool) -> None:
        """Fire the health callback with ONLY (name, healthy) -- never a frame
        (ADR-0002). Tolerant: a broken monitor never breaks the source loop (same
        rule as the source's other injected callbacks)."""
        if self._on_health is None:
            return
        try:
            self._on_health(self._name, healthy)
        except Exception:
            logging.warning("CameraSource(%s) health callback failed",
                            self._name or self.room, exc_info=True)

    def _emit_privacy(self, active: bool) -> None:
        """Fire the privacy callback with ONLY (name, active) -- never a frame
        (ADR-0002). Tolerant: a broken monitor never breaks the source loop (same
        rule as the source's other injected callbacks)."""
        if self._on_privacy is None:
            return
        try:
            self._on_privacy(self._name, active)
        except Exception:
            logging.warning("CameraSource(%s) privacy callback failed",
                            self._name or self.room, exc_info=True)

    async def events(self) -> AsyncIterator[SensingEvent]:
        global _ACTIVE
        _ACTIVE += 1
        self._last_ok = time.monotonic()
        try:
            while True:
                try:
                    async with contextlib.aclosing(self._frames(self._url)) as stream:
                        async for frame in stream:
                            self._last_ok = time.monotonic()
                            if self._down_reported:
                                # Recovery edge: first frame after a down report.
                                self._emit_health(True)
                                self._down_reported = False
                            if self._privacy_reported:
                                # Recovery edge: a real frame means the camera left
                                # privacy mode (the lens was uncovered again).
                                self._emit_privacy(False)
                                self._privacy_reported = False
                            det = await asyncio.to_thread(self._detect, frame)
                            present = det.count > 0
                            targets: tuple[Target, ...] = ()
                            if self._pose:
                                targets = tuple(await asyncio.to_thread(
                                    self._pose_detect, frame, self._confidence))
                            yield SensingEvent(
                                room=self.room, modality="camera", presence=present,
                                motion=0.0, breathing_bpm=None, heart_bpm=None,
                                confidence=det.confidence if present else 0.0,
                                ts=datetime.now(timezone.utc).isoformat(),
                                targets=targets,
                                count=det.count,   # honest per-source count (0 when empty)
                            )
                            if self._interval:
                                await asyncio.sleep(self._interval)
                except asyncio.CancelledError:
                    raise
                except CameraPrivacySignal:
                    # Honest privacy state (see CameraPrivacySignal). This is NOT a
                    # fault: clear any stale down-latch and edge-report privacy=True.
                    # Deliberately `continue`s straight past the down-latch check below
                    # -- an indefinitely-covered camera must never cry-wolf as an error,
                    # no matter how long it stays covered.
                    if self._down_reported:
                        self._emit_health(True)
                        self._down_reported = False
                    if not self._privacy_reported:
                        self._emit_privacy(True)
                        self._privacy_reported = True
                    if self._reconnect:
                        await asyncio.sleep(self._reconnect)
                    continue
                except Exception:
                    logging.warning("CameraSource(%s) error; reconnecting", self.room, exc_info=True)
                # This cycle did NOT reproduce the privacy signature (a genuine error,
                # or a clean end that never opened at all) -- drop any stale privacy
                # claim so a real fault can surface via the ordinary down latch below
                # instead of hiding behind a privacy label that no longer applies.
                if self._privacy_reported:
                    self._emit_privacy(False)
                    self._privacy_reported = False
                # F3 down edge: fire a single (name, False) once we've gone
                # `unhealthy_secs` without a frame. Runs on both the error path AND a
                # clean stream-end (empty/closed capture, e.g. a dead/drifted host).
                if self._on_health and not self._down_reported and (
                        time.monotonic() - self._last_ok) >= self._unhealthy_secs:
                    self._emit_health(False)
                    self._down_reported = True
                if self._reconnect:
                    await asyncio.sleep(self._reconnect)
        finally:
            _ACTIVE -= 1
            if _ACTIVE == 0:
                release_model()


# ---- Real adapters (lazy imports; only exercised on the real hardware path) ----

_YOLO_MODEL = None
_POSE_MODEL = None
_ACTIVE = 0                       # count of running CameraSource.events() loops
_MODEL_LOCK = threading.Lock()    # guards the lazy YOLO load (called from to_thread workers)


def _open_capture(url: str):
    import cv2  # lazy: only needed on the real path
    return cv2.VideoCapture(url)


def _read(cap):
    return cap.read()  # (ok: bool, frame)


def _release(cap) -> None:
    with contextlib.suppress(Exception):
        cap.release()


def _is_opened(cap) -> bool:
    """True only if the capture reports its session as open (the RTSP handshake
    succeeded). Never raises -- an odd/fake capture object (e.g. a test double without
    isOpened()) is treated as 'not opened', which is the safe/conservative side (no
    privacy signal fabricated from an object we can't confidently ask)."""
    try:
        return bool(cap.isOpened())
    except Exception:
        return False


def _model():
    """Load the YOLO nano model once (GPU if available). Lazy — importing
    ultralytics pulls torch/CUDA, which we never want at import/test time.
    Thread-safe (double-checked locking): _detect runs via asyncio.to_thread,
    so concurrent cameras could race on the first load without this lock."""
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        with _MODEL_LOCK:
            if _YOLO_MODEL is None:
                from ultralytics import YOLO
                _YOLO_MODEL = YOLO("yolov8n.pt")
    return _YOLO_MODEL


def _pose_model():
    """Load the YOLO-pose nano model once (GPU if available). Lazy/thread-safe
    for the same reasons as `_model()` — its own cached global so the plain
    detect and pose paths can be loaded/released independently until both
    cameras stop."""
    global _POSE_MODEL
    if _POSE_MODEL is None:
        with _MODEL_LOCK:
            if _POSE_MODEL is None:
                from ultralytics import YOLO
                _POSE_MODEL = YOLO("yolo11n-pose.pt")
    return _POSE_MODEL


def release_model() -> None:
    """Drop the cached YOLO models (detect + pose) and hand cached VRAM back to
    the driver. Safe with torch absent (suppressed). Called when the last
    camera stops so the GPU isn't held while no camera is running (e.g. so
    games get the VRAM back)."""
    global _YOLO_MODEL, _POSE_MODEL
    with _MODEL_LOCK:
        _YOLO_MODEL = None
        _POSE_MODEL = None
    with contextlib.suppress(Exception):
        import torch
        torch.cuda.empty_cache()


async def rtsp_frames(url: str) -> "AsyncIterator[object]":
    """Pull frames from an RTSP capture. Blocking reads run in a thread so they
    never block the loop; the capture is released in the finally, so aclose()
    on disable is a hard RTSP kill. Opening the capture is offloaded too — a
    hung/unreachable camera would otherwise freeze the whole backend for the
    OS TCP timeout on every (re)connect.

    Privacy-mode signature (see CameraPrivacySignal): if the capture opened
    (isOpened() True -- the RTSP session negotiated) but not a single frame was ever
    read before the stream ended, this raises CameraPrivacySignal instead of ending
    silently, so CameraSource can report a distinct 'privacy' state. A capture that
    never opens at all (isOpened() False -- wrong IP/creds/camera off the network) is
    unaffected: it still just ends the generator normally, same as before this feature
    (F3's existing down-latch path)."""
    cap = await asyncio.to_thread(_open_capture, url)
    try:
        opened = await asyncio.to_thread(_is_opened, cap)
        got_frame = False
        while True:
            ok, frame = await asyncio.to_thread(_read, cap)
            if not ok:
                break
            got_frame = True
            yield frame
        if opened and not got_frame:
            raise CameraPrivacySignal(
                "RTSP session opened but produced no frames (privacy-mode candidate)")
    finally:
        _release(cap)


def yolo_detect(frame, conf_threshold: float = 0.0) -> Detection:
    results = _model()(frame)
    persons = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        for cls, conf in zip(list(boxes.cls), list(boxes.conf)):
            if int(cls) == 0 and float(conf) >= conf_threshold:  # COCO class 0 = person
                persons.append(float(conf))
    return Detection(count=len(persons), confidence=max(persons) if persons else 0.0)


def feet_point(xyxy) -> tuple[float, float]:
    """Ground-contact pixel = bottom-centre of a person bbox (x1, y1, x2, y2): the point
    a localizer projects to the floor. Pure pixel arithmetic on a DETECTION coordinate --
    it never reads or keeps frame contents (ADR-0002)."""
    x1, _y1, x2, y2 = (float(v) for v in xyxy)
    return ((x1 + x2) / 2.0, y2)


def yolo_pose_detect(frame, confidence: float = 0.0, localize=None,
                     on_feet=None) -> list[Target]:
    """Per-person targets from YOLO-pose. `posture` derives from COCO-17 keypoints via
    `classify_posture`.

    When a `localize` callable is supplied -- ``(feet_px, (img_w, img_h)) ->
    (x, y, conf) | None`` in ROOM-LOCAL metres -- the person's FEET pixel (bottom-centre
    of the box) is projected to a floor (x, y) and that POSITION-quality confidence rides
    the Target. Without it (no calibration), x/y stay None so the camera stays
    room-centred (honest fallback).

    When an `on_feet` callable is supplied -- ``(feet_px, (img_w, img_h), conf) ->
    None`` -- the HIGHEST-confidence person's raw FEET PIXEL is handed to it once per
    frame. This is the walk-to-calibrate sampling seam: the wizard pairs that pixel with
    the known floor spot the person is standing on. ADR-0002: `on_feet` receives ONLY a
    pixel COORDINATE + the image DIMENSIONS + a confidence scalar -- never a frame/crop.

    The frame is read ONLY for its pixel size (`frame.shape`); it is never stored
    (ADR-0002)."""
    results = _pose_model()(frame)
    img_size = None
    if localize is not None or on_feet is not None:
        try:
            h, w = frame.shape[:2]
            img_size = (float(w), float(h))
        except Exception:
            img_size = None       # unknown frame size -> skip positioning, keep posture
    want_feet = img_size is not None and (localize is not None or on_feet is not None)
    targets: list[Target] = []
    best_feet = None              # (feet_px, conf) of the highest-confidence person
    for r in results:
        boxes = getattr(r, "boxes", None)
        kpts = getattr(r, "keypoints", None)
        if boxes is None or kpts is None:
            continue
        for i, (cls, conf) in enumerate(zip(list(boxes.cls), list(boxes.conf))):
            if int(cls) != 0 or float(conf) < confidence:  # COCO class 0 = person
                continue
            kps = [(float(x), float(y)) for x, y in kpts.xy[i]]
            tx = ty = None
            tconf = float(conf)                            # detection conf when unpositioned
            feet = None
            if want_feet:
                try:
                    feet = feet_point(boxes.xyxy[i])
                except Exception:
                    feet = None
            if localize is not None and feet is not None:
                try:
                    loc = localize(feet, img_size)
                except Exception:
                    loc = None
                if loc is not None:
                    tx, ty, tconf = float(loc[0]), float(loc[1]), float(loc[2])
            if on_feet is not None and feet is not None and (
                    best_feet is None or float(conf) > best_feet[1]):
                best_feet = (feet, float(conf))
            targets.append(Target(
                id=i + 1, x=tx, y=ty,
                posture=classify_posture(kps),
                confidence=tconf,
            ))
    if on_feet is not None and best_feet is not None:
        # Coordinate + dims + confidence only -- no frame (ADR-0002). Tolerant: a broken
        # sink never breaks the detection loop (same rule as the other injected hooks).
        try:
            on_feet(best_feet[0], img_size, best_feet[1])
        except Exception:
            logging.warning("yolo_pose_detect on_feet sink failed", exc_info=True)
    return targets
