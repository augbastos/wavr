"""Server-side guided-calibration session state machine (`wavr.calib_session`):
the 'stand here -> capture -> repeat -> solve' walk, moved server-side so it
survives a frontend reload and can be driven by a non-browser client (MCP/voice).

ADR-0002 is the load-bearing invariant under test here too: a `CalibSession`
holds ONLY known FLOOR spots (pure geometry) + captured FEET-PIXEL coordinates
-- NEVER a frame/crop/image. Nothing is written to `CalibrationStore` until an
explicit, successful solve; an aborted or timed-out walk leaves ZERO trace.

Pure unit tests against the module directly (no FastAPI/TestClient) -- the
route-level wiring (`/calib-session`, `/calib-capture`, `/calib-retry`,
`use_session=true` on PUT .../calibration) is already covered end-to-end in
test_calib_sample.py; this file locks the state machine's own contract.
"""
import dataclasses

import pytest

import wavr.calib_session as calib_session_mod
from wavr.calib_session import (
    CalibSession,
    CalibSessionError,
    CalibSessionStore,
    SessionState,
)
from wavr.localize import floor_spots_for_room, homography_from_points

# Same rectangular room + solvable image points already proven non-degenerate in
# test_calib_sample.py -- reused here so a homography solve in this file can never
# fail on a geometry issue unrelated to the session state machine.
_POLY = [[4.2, 0.0], [7.7, 0.0], [7.7, 3.0], [4.2, 3.0]]
_CORNERS = floor_spots_for_room(_POLY)[1:]          # 4 non-collinear floor corners
_IMG_PTS = [[100.0, 600.0], [1180.0, 600.0], [1180.0, 100.0], [100.0, 100.0]]


def _walked_session(spots=None):
    """A fresh session with `spots` (default: the 4 solvable corners above)."""
    return CalibSession(camera="cam_q", spots=list(spots if spots is not None else _CORNERS))


# --------------------------------------------------------------------------- #
# ADR-0002 shape: no frame ever held, error type is a controlled ValueError.
# --------------------------------------------------------------------------- #

def test_error_is_a_value_error_subclass():
    # Mirrors CalibrationError (calib_store.py): the route layer turns THIS
    # exception type into a clean 409, never an unhandled 500.
    assert issubclass(CalibSessionError, ValueError)


def test_session_dataclass_never_holds_a_frame_field():
    names = {f.name for f in dataclasses.fields(CalibSession)}
    assert names == {"camera", "spots", "state", "spot_idx", "pairs",
                      "img_size", "started", "touched"}
    for forbidden in ("frame", "image", "crop", "pixels", "jpeg", "bytes"):
        assert forbidden not in names


def test_pairs_hold_only_coordinate_tuples_never_a_frame():
    sess = _walked_session()
    sess.capture((200.0, 500.0), 1280, 720)
    feet, floor = sess.pairs[0]
    assert isinstance(feet, tuple) and all(isinstance(v, float) for v in feet)
    assert isinstance(floor, tuple) and all(isinstance(v, float) for v in floor)


# --------------------------------------------------------------------------- #
# Happy path: walk every spot, capture, advance, ready, solve.
# --------------------------------------------------------------------------- #

def test_new_session_starts_walking_at_first_spot():
    sess = _walked_session()
    assert sess.state == SessionState.WALKING
    assert sess.spot_idx == 0
    assert sess.pairs == []
    assert sess.img_size is None


def test_capture_records_feet_and_floor_pair_and_advances():
    sess = _walked_session()
    sess.capture((100.0, 600.0), 1280, 720)
    assert sess.pairs == [((100.0, 600.0), tuple(_CORNERS[0]))]
    assert sess.spot_idx == 1
    assert sess.state == SessionState.WALKING       # 3 spots still to go


def test_capture_all_spots_flips_to_ready():
    sess = _walked_session()
    for feet, img in zip(_IMG_PTS, _CORNERS):
        sess.capture(feet, 1280, 720)
    assert sess.state == SessionState.READY
    assert sess.spot_idx == len(_CORNERS)
    assert len(sess.pairs) == len(_CORNERS)


def test_capture_locks_img_size_from_first_capture():
    sess = _walked_session()
    assert sess.img_size is None
    sess.capture(_IMG_PTS[0], 1280, 720)
    assert sess.img_size == (1280, 720)


def test_full_walk_pairs_solve_a_homography():
    # The stated invariant: a homography is producible from >= 4 captured pairs.
    sess = _walked_session()
    for feet, _spot in zip(_IMG_PTS, _CORNERS):
        sess.capture(feet, 1280, 720)
    assert sess.state == SessionState.READY
    image_points = [p[0] for p in sess.pairs]
    floor_points = [p[1] for p in sess.pairs]
    h = homography_from_points(image_points, floor_points)   # raises on degenerate
    assert h.shape == (3, 3)


# --------------------------------------------------------------------------- #
# img_size lock: a mid-walk resolution change is rejected, never silently mixed.
# --------------------------------------------------------------------------- #

def test_capture_rejects_img_size_change_mid_walk():
    sess = _walked_session()
    sess.capture(_IMG_PTS[0], 1280, 720)
    with pytest.raises(CalibSessionError, match="image size changed"):
        sess.capture(_IMG_PTS[1], 640, 480)


def test_capture_after_size_mismatch_leaves_state_and_pairs_untouched():
    sess = _walked_session()
    sess.capture(_IMG_PTS[0], 1280, 720)
    with pytest.raises(CalibSessionError):
        sess.capture(_IMG_PTS[1], 640, 480)
    # the rejected capture must not have partially mutated anything
    assert len(sess.pairs) == 1
    assert sess.spot_idx == 1
    assert sess.img_size == (1280, 720)
    assert sess.state == SessionState.WALKING


# --------------------------------------------------------------------------- #
# State gate: capture()/retry_current() are only legal in specific states --
# the state machine's own permission gate (mirrors an auth-gate: an action
# outside its allowed state is refused, cleanly, every time).
# --------------------------------------------------------------------------- #

def test_capture_rejected_once_ready():
    sess = _walked_session()
    for feet in _IMG_PTS:
        sess.capture(feet, 1280, 720)
    assert sess.state == SessionState.READY
    with pytest.raises(CalibSessionError, match="not walking"):
        sess.capture((1.0, 1.0), 1280, 720)


def test_capture_rejected_when_aborted():
    sess = _walked_session()
    sess.abort()
    with pytest.raises(CalibSessionError):
        sess.capture((1.0, 1.0), 1280, 720)


def test_capture_rejected_when_solved():
    sess = _walked_session()
    for feet in _IMG_PTS:
        sess.capture(feet, 1280, 720)
    sess.mark_solved()
    with pytest.raises(CalibSessionError):
        sess.capture((1.0, 1.0), 1280, 720)


def test_capture_rejected_with_zero_known_spots():
    # Degenerate room (e.g. < 3 polygon vertices -> floor_spots_for_room == []),
    # mirrored from test_calib_sample.py's degenerate-polygon case: a session
    # with no spots to visit must refuse a capture cleanly, not IndexError.
    sess = _walked_session(spots=[])
    with pytest.raises(CalibSessionError, match="no spot left"):
        sess.capture((1.0, 1.0), 1280, 720)


# --------------------------------------------------------------------------- #
# retry_current: undo-last, step back, re-try -- never persisted either way.
# --------------------------------------------------------------------------- #

def test_retry_undoes_last_capture_and_returns_to_walking():
    sess = _walked_session()
    sess.capture(_IMG_PTS[0], 1280, 720)
    sess.retry_current()
    assert sess.state == SessionState.WALKING
    assert sess.spot_idx == 0
    assert sess.pairs == []


def test_retry_from_ready_steps_back_one_spot():
    sess = _walked_session()
    for feet in _IMG_PTS:
        sess.capture(feet, 1280, 720)
    assert sess.state == SessionState.READY
    sess.retry_current()
    assert sess.state == SessionState.WALKING
    assert sess.spot_idx == len(_CORNERS) - 1
    assert len(sess.pairs) == len(_CORNERS) - 1


def test_retry_with_no_capture_raises():
    sess = _walked_session()
    with pytest.raises(CalibSessionError, match="no capture to retry"):
        sess.retry_current()


def test_retry_rejected_when_solved_or_aborted():
    solved = _walked_session()
    for feet in _IMG_PTS:
        solved.capture(feet, 1280, 720)
    solved.mark_solved()
    with pytest.raises(CalibSessionError):
        solved.retry_current()

    aborted = _walked_session()
    aborted.capture(_IMG_PTS[0], 1280, 720)
    aborted.abort()
    with pytest.raises(CalibSessionError):
        aborted.retry_current()


# --------------------------------------------------------------------------- #
# abort / mark_solved: zero-trace-on-abandon (ADR-0002 / SD-wear idiom).
# --------------------------------------------------------------------------- #

def test_abort_clears_pairs_and_sets_state():
    sess = _walked_session()
    sess.capture(_IMG_PTS[0], 1280, 720)
    sess.capture(_IMG_PTS[1], 1280, 720)
    sess.abort()
    assert sess.state == SessionState.ABORTED
    assert sess.pairs == []                      # nothing lingers for a later read


def test_abort_then_capture_is_rejected():
    sess = _walked_session()
    sess.abort()
    with pytest.raises(CalibSessionError):
        sess.capture(_IMG_PTS[0], 1280, 720)


def test_mark_solved_sets_state():
    sess = _walked_session()
    for feet in _IMG_PTS:
        sess.capture(feet, 1280, 720)
    sess.mark_solved()
    assert sess.state == SessionState.SOLVED


# --------------------------------------------------------------------------- #
# TTL / expiry: an abandoned walk is evicted, never actionable again, and
# NOTHING was ever written to disk by simply timing out.
# --------------------------------------------------------------------------- #

def test_expired_false_within_ttl_true_after(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(calib_session_mod.time, "monotonic", lambda: clock[0])
    sess = _walked_session()
    # `touched`'s dataclass default_factory=time.monotonic is bound to the REAL
    # function at module-import time, so it is unaffected by patching the module
    # attribute afterward -- sync it explicitly to the fake clock's baseline.
    # `expired()` itself does a dynamic `time.monotonic()` lookup at call time, so
    # it DOES see the patch, which is what this test actually locks down.
    sess.touched = clock[0]
    assert sess.expired(ttl_s=60.0) is False
    clock[0] += 61.0
    assert sess.expired(ttl_s=60.0) is True


def test_capture_touches_session_extending_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(calib_session_mod.time, "monotonic", lambda: clock[0])
    sess = _walked_session()
    sess.touched = clock[0]
    clock[0] += 500.0                              # long idle, but under TTL
    sess.capture(_IMG_PTS[0], 1280, 720)            # touches the session
    assert sess.expired(ttl_s=600.0) is False
    clock[0] += 599.0                               # < 600s since the touch
    assert sess.expired(ttl_s=600.0) is False


def test_store_get_evicts_expired_session_and_returns_none(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(calib_session_mod.time, "monotonic", lambda: clock[0])
    store = CalibSessionStore(ttl_s=60.0)
    sess = store.start("cam_q", _CORNERS)
    sess.touched = clock[0]                        # sync (see note above)
    clock[0] += 61.0
    assert store.get("cam_q") is None
    # a session already evicted-on-read never comes back on a later read either
    assert store.get("cam_q") is None


# --------------------------------------------------------------------------- #
# Camera-disabled / no-frames degrade: a walk with zero captures never crashes,
# just sits WALKING until it is explicitly ended or ages out via TTL.
# --------------------------------------------------------------------------- #

def test_session_with_no_captures_stays_walking_until_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(calib_session_mod.time, "monotonic", lambda: clock[0])
    store = CalibSessionStore(ttl_s=60.0)
    sess = store.start("cam_q", _CORNERS)
    sess.touched = clock[0]                        # sync (see TTL note above)
    # camera disabled / no person ever detected -> nothing ever calls capture()
    got = store.get("cam_q")
    assert got is not None and got.state == SessionState.WALKING and got.pairs == []
    clock[0] += 61.0
    assert store.get("cam_q") is None               # degrades to a clean eviction, not an error


def test_repeated_reads_of_a_live_session_do_not_mutate_it():
    store = CalibSessionStore()
    store.start("cam_q", _CORNERS)
    for _ in range(5):
        got = store.get("cam_q")
        assert got.state == SessionState.WALKING and got.pairs == []


# --------------------------------------------------------------------------- #
# Store cap ("rate limit" on concurrently tracked walks) + replace-on-restart.
# --------------------------------------------------------------------------- #

def test_store_bounds_session_count_evicts_oldest():
    store = CalibSessionStore(max_sessions=2)
    store.start("a", _CORNERS)
    store.start("b", _CORNERS)
    store.start("c", _CORNERS)                       # evicts the oldest ("a")
    assert store.get("a") is None
    assert store.get("b") is not None
    assert store.get("c") is not None


def test_store_start_replaces_existing_session_for_same_camera():
    store = CalibSessionStore()
    first = store.start("cam_q", _CORNERS)
    first.capture(_IMG_PTS[0], 1280, 720)
    second = store.start("cam_q", _CORNERS)           # a new walk always wins
    assert second is not first
    assert second.pairs == []
    assert store.get("cam_q").pairs == []


def test_store_end_on_unknown_camera_is_a_noop():
    store = CalibSessionStore()
    store.end("nope")                                 # must not raise


def test_store_get_unknown_camera_returns_none():
    assert CalibSessionStore().get("nope") is None


# --------------------------------------------------------------------------- #
# Adversarial / garbage observations: rejected via a controlled exception
# type, never a silently corrupted session -- and one real gap this proves
# rather than hides (capture() does not itself range-check img dimensions
# the way the PUT .../calibration route does for its own image_points path).
# --------------------------------------------------------------------------- #

def test_capture_non_iterable_feet_px_raises_and_does_not_advance():
    sess = _walked_session()
    with pytest.raises(TypeError):
        sess.capture(42, 1280, 720)                   # feet_px must be a pair, not a scalar
    assert sess.pairs == []
    assert sess.spot_idx == 0
    assert sess.state == SessionState.WALKING


def test_capture_non_numeric_img_dims_raises_before_mutating_state():
    sess = _walked_session()
    with pytest.raises(ValueError):
        sess.capture((1.0, 2.0), "wide", 720)          # int("wide") raises ValueError
    assert sess.pairs == []
    assert sess.spot_idx == 0
    assert sess.img_size is None                       # rejected before the lock happened


def test_capture_nan_img_dims_raises_and_leaves_session_clean():
    sess = _walked_session()
    with pytest.raises(ValueError):
        sess.capture((1.0, 2.0), float("nan"), 720)    # int(nan) -> ValueError, not a silent 0
    assert sess.img_size is None
    assert sess.pairs == []


def test_capture_infinite_img_dims_raises_overflowerror_not_wrapped():
    # Documents actual behaviour: int(float('inf')) raises OverflowError, which is
    # NOT a CalibSessionError/ValueError -- the route's `except CalibSessionError`
    # would NOT catch this, so it would surface as an unhandled 500 rather than a
    # clean 409/422. Still "rejected without crashing the session state" (nothing
    # is mutated), but callers of THIS module directly should not assume every
    # garbage input maps to CalibSessionError.
    sess = _walked_session()
    with pytest.raises(OverflowError):
        sess.capture((1.0, 2.0), float("inf"), 720)
    assert sess.img_size is None
    assert sess.pairs == []


def test_capture_first_capture_garbage_feet_px_still_locks_img_size():
    # A genuine side effect worth locking down: img_size is set BEFORE feet_px is
    # validated, so a garbage feet_px on the very FIRST capture of a walk still
    # leaves img_size populated even though the capture itself failed. Confirms
    # this doesn't corrupt `pairs`/`spot_idx` (append() never runs), only that
    # img_size is not perfectly atomic with the rest of the capture.
    sess = _walked_session()
    with pytest.raises(TypeError):
        sess.capture(None, 1280, 720)
    assert sess.img_size == (1280, 720)                # locked despite the failed capture
    assert sess.pairs == []
    assert sess.spot_idx == 0
