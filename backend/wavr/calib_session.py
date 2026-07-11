"""Server-side guided-calibration session state machine: the 'stand here -> capture
-> repeat -> solve' walk-to-calibrate quest, moved server-side so it (a) survives a
frontend reload and (b) can be driven by a non-browser client (MCP/voice) without
reimplementing the bookkeeping the existing client-side `walk` object in
frontend/index.html already has (that wizard keeps working unchanged; this is an
additive alternative driver, not a replacement).

In-memory, ephemeral, coordinate-only (ADR-0002): a session holds ONLY the KNOWN
floor spots (pure geometry, from `localize.floor_spots_for_room`) + the captured
FEET PIXELS (from `CalibSampleStore.latest`, itself already coordinate-only) --
NEVER a frame. Nothing here is written to `CalibrationStore` until an explicit,
successful solve (`PUT .../calibration` with `use_session=true`) -- a walk that is
aborted or simply times out leaves ZERO trace on disk (change-gated write: only a
completed solve ever touches SQLite / the SD card, same idiom as `_refuse_once`'s
`persist=changed` gate in `wavr.app`)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class SessionState(str, Enum):
    WALKING = "walking"
    READY = "ready"
    SOLVED = "solved"
    ABORTED = "aborted"


class CalibSessionError(ValueError):
    """Raised on an out-of-order or malformed session action (e.g. a capture after
    the walk is already READY, or a capture whose image size doesn't match the size
    this session locked at its first capture). The route layer turns this into a
    409 (session-state conflict), never a 500."""


# How long an abandoned session lingers before it is treated as gone (auto-evicted
# on the next read/start) -- mirrors CalibSampleStore's TTL idiom, just much longer
# since a guided walk is a multi-minute human-paced flow, not a per-frame sample.
_DEFAULT_TTL_S = 600.0
# Cap the cameras tracked at once -- mirrors CalibSampleStore._MAX_CAMERAS; in
# practice there is one live guided-calibration session at a time.
_MAX_SESSIONS = 64


@dataclass
class CalibSession:
    """One camera's in-progress (or just-finished) guided walk.

    `spots`    : the KNOWN floor spots to visit in order (FLOOR metres, from
                 `localize.floor_spots_for_room` -- centroid then corners).
    `pairs`    : captured `(feet_px, floor_spot)` correspondences so far, in spot
                 order -- exactly the shape `calib_refine.solve_progressive`
                 consumes once unzipped.
    `img_size` : the `(img_w, img_h)` LOCKED by this session's first capture; every
                 later capture in the SAME walk must match it (a mid-walk resolution
                 change would silently mix incompatible pixel spaces into one solve
                 -- reject instead of doing that).
    """
    camera: str
    spots: list
    state: SessionState = SessionState.WALKING
    spot_idx: int = 0
    pairs: list = field(default_factory=list)
    img_size: tuple | None = None
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)

    def expired(self, ttl_s: float = _DEFAULT_TTL_S) -> bool:
        return (time.monotonic() - self.touched) > ttl_s

    def capture(self, feet_px, img_w: int, img_h: int) -> None:
        """Pair the CURRENT spot with `feet_px`, advance to the next spot, and flip
        to READY once every spot has been captured. Raises `CalibSessionError` if
        the walk isn't WALKING (already READY/SOLVED/ABORTED, or every spot already
        captured) or if `(img_w, img_h)` doesn't match the size this session's
        first capture locked."""
        if self.state != SessionState.WALKING:
            raise CalibSessionError(f"session is {self.state.value}, not walking")
        if self.spot_idx >= len(self.spots):
            raise CalibSessionError("no spot left to capture")
        size = (int(img_w), int(img_h))
        if self.img_size is None:
            self.img_size = size
        elif self.img_size != size:
            raise CalibSessionError(
                f"image size changed mid-walk (was {self.img_size}, now {size})")
        self.pairs.append((tuple(feet_px), tuple(self.spots[self.spot_idx])))
        self.spot_idx += 1
        self.touched = time.monotonic()
        if self.spot_idx >= len(self.spots):
            self.state = SessionState.READY

    def retry_current(self) -> None:
        """Undo the LAST capture and step back to re-try that spot -- a capability
        the browser-only wizard doesn't have today (it only ever cancels the whole
        walk). Raises `CalibSessionError` if there is nothing to undo (no capture
        yet) or the walk is already SOLVED/ABORTED."""
        if self.state not in (SessionState.WALKING, SessionState.READY):
            raise CalibSessionError(f"session is {self.state.value}, cannot retry")
        if not self.pairs:
            raise CalibSessionError("no capture to retry")
        self.pairs.pop()
        self.spot_idx = max(0, self.spot_idx - 1)
        self.state = SessionState.WALKING
        self.touched = time.monotonic()

    def abort(self) -> None:
        """Cancel the walk -- clears the captured pairs so nothing lingers for a
        later read. Nothing here was ever written to disk (ADR-0002 / SD-wear)."""
        self.pairs = []
        self.state = SessionState.ABORTED
        self.touched = time.monotonic()

    def mark_solved(self) -> None:
        self.state = SessionState.SOLVED
        self.touched = time.monotonic()


class CalibSessionStore:
    """In-memory guided-calibration session per camera. Same lifecycle/eviction
    pattern as `CalibSampleStore`: TTL'd, capped, coordinate-only (ADR-0002). NOT
    thread-safe against concurrent starts of the SAME camera from two callers --
    the route layer already serializes writes via `require_local` (one operator
    walk at a time is the whole point of a guided session)."""

    def __init__(self, ttl_s: float = _DEFAULT_TTL_S, max_sessions: int = _MAX_SESSIONS):
        self._ttl = float(ttl_s)
        self._max = max_sessions
        self._sessions: dict[str, CalibSession] = {}

    def start(self, name: str, spots: list) -> CalibSession:
        """Begin a fresh walk for `name`, REPLACING any existing session for that
        camera (a new walk always wins -- an abandoned prior session is discarded,
        never merged)."""
        sess = CalibSession(camera=name, spots=list(spots))
        self._sessions[name] = sess
        while len(self._sessions) > self._max:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].started)
            self._sessions.pop(oldest, None)
        return sess

    def get(self, name: str) -> CalibSession | None:
        """The camera's live session, or None if there isn't one / it expired (an
        expired session is evicted right here on read, so it can never be acted on
        again)."""
        sess = self._sessions.get(name)
        if sess is None:
            return None
        if sess.expired(self._ttl):
            self._sessions.pop(name, None)
            return None
        return sess

    def end(self, name: str) -> None:
        """Drop a camera's session outright (walk finished/solved, or the operator
        cancelled) -- called when a calibration session ends server-side."""
        self._sessions.pop(name, None)
