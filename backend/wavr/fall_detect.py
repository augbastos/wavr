"""A9 -- fall / no-motion suspicion (RESEARCH-GRADE, ADR-0003).

NOT a certified medical or fall-detection device. This is a geometric RULE layered on
signals two other modules already produce -- nothing new is inferred from a frame here:

  * `wavr.sources.camera.classify_posture` -- a pure COCO-17 keypoint heuristic that already
    yields a per-target "lying"/"sitting"/"standing" posture (no ML beyond the pose model
    already running for A-series targets).
  * `wavr.housemap.in_rest_zone` -- the operator-drawn bed/rest polygon(s) per room (map
    editor, A9 requirement #1).

`lying_outside_zone` folds the two into ONE per-room boolean each fusion tick ("is someone
lying somewhere this room's bed/rest zones do NOT cover"); `FallDetector` turns a stream of
those booleans into an edge-triggered, dwell-debounced `FallAlert` -- mirroring
`wavr.watch.IntrusionAlertLog`'s shape exactly (`kind`/`severity`/`room`/`ts` +
`to_dict()`/`recent_alerts()`) so `wavr.api_inventory.merge_alerts` folds it into
GET /api/alerts with zero special-casing.

Egress discipline (ADR-0002/ADR-0003): a FallAlert carries ONLY the room name + how long
(`duration_s`) + the honest disclaimer below -- never a frame, a target position, a posture
value, or an identity. DEFAULT OFF at the caller (`Config.fall_detect_enabled`) -- this
module has no opinion on the toggle, it is only reachable when the operator explicitly opts
in, same posture as every other camera-adjacent feature in this codebase."""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.alert_severity import SEVERITY_ALERT
from wavr.housemap import in_rest_zone

DISCLAIMER = (
    "Wavr fall detection is a RESEARCH DEMONSTRATION, not a certified medical or "
    "fall-detection device (ADR-0003). Treat an alert as a prompt to check in on someone -- "
    "never as a diagnosis or a guarantee that a fall did or did not happen."
)

# Flicker tolerance (seconds): a brief GAP in the "lying outside a rest zone" reading -- a
# missed detection, an ambiguous single-frame pose read, a momentary occlusion -- does not
# reset the dwell clock as long as the gap itself stays under this window. Mirrors the
# asymmetric hold `wavr.fusion._debounce_occupancy` already uses for the occupied/vacant
# boolean, just tuned far shorter -- this is smoothing a noisy per-frame classifier, not
# debouncing a real occupancy state change. Not exposed as a config (A9 requirement #2 asks
# for exactly two knobs: the master toggle + the dwell duration); this is an internal
# implementation constant.
FLICKER_GRACE_S = 6.0


def _as_utc(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def lying_outside_zone(house: dict, room: str, targets) -> bool:
    """True iff at least one target in `targets` (RoomState.targets -- dicts or Target
    objects) is posture=='lying' with a KNOWN position (x/y not None) that
    `wavr.housemap.in_rest_zone` says is OUTSIDE every bed/rest zone for `room`.

    HONESTY GATE: a lying target with an UNKNOWN position (x/y None -- an uncalibrated
    camera, see wavr.sources.camera.yolo_pose_detect) is SKIPPED, never counted as "at
    risk". An uncalibrated camera cannot tell in/out of a zone, so it must never manufacture
    a verdict either way -- the alternative (treating unpositioned = outside) would make
    this feature fire on ordinary sleep in every bedroom whose camera isn't calibrated,
    exactly the false-positive the zone requirement (#1) exists to prevent. A room with NO
    rest zone drawn at all still protects nothing -- lying anywhere positioned in it counts
    as "at risk" (the honest complement: no zone means no exemption)."""
    for t in targets or ():
        posture = t.get("posture") if isinstance(t, dict) else getattr(t, "posture", None)
        if posture != "lying":
            continue
        x = t.get("x") if isinstance(t, dict) else getattr(t, "x", None)
        y = t.get("y") if isinstance(t, dict) else getattr(t, "y", None)
        if x is None or y is None:
            continue
        if not in_rest_zone(house, room, float(x), float(y)):
            return True
    return False


class FallAlert:
    """One edge-triggered "lying outside a rest zone" episode that has persisted for at
    least the configured dwell. Count/geometry-free by construction: `room` + `duration_s`
    only -- NEVER a target position, posture value, or identity (A9 requirement: "carrying
    the ROOM + duration only"). Severity fixed at `alert` (high, but not `critical` -- the
    shared ladder reserves `critical` for a sustained/confirmed gateway-identity change,
    see wavr.alert_severity)."""

    __slots__ = ("room", "duration_s", "severity", "ts")

    def __init__(self, room: str, duration_s: float, ts: str):
        self.room = room
        self.duration_s = duration_s
        self.severity = SEVERITY_ALERT
        self.ts = ts

    def to_dict(self) -> dict:
        return {
            "kind": "fall_suspected",
            "severity": self.severity,
            "room": self.room,
            "duration_s": self.duration_s,
            "disclaimer": DISCLAIMER,
            "ts": self.ts,
        }


class FallDetector:
    """Edge-triggered, dwell-debounced per-room timer over `lying_outside_zone`. Mirrors
    `wavr.watch.IntrusionAlertLog`'s ring/edge-latch pattern (same `recent_alerts()`/
    `to_dict()` shape) so GET /api/alerts merges it with zero special-casing.

    `record(room, at_risk, ts)`:
      * `at_risk=True`  -- (re)start or continue the room's dwell window. Once it has run
        for >= `dwell_s` (tolerating gaps under FLICKER_GRACE_S -- see module docstring),
        returns ONE FallAlert and latches: no repeat until the episode clears.
      * `at_risk=False` -- starts a grace countdown. Once the GAP since the last `True`
        exceeds FLICKER_GRACE_S the episode is over: the dwell clock AND the latch both
        reset, so a real "they got up" (or moved back into the bed zone) fully re-arms the
        detector rather than it silently staying latched forever.

    DEFAULT-OFF is enforced by the CALLER (only constructed when
    `Config.fall_detect_enabled` is on) -- this class has no toggle of its own."""

    def __init__(self, dwell_s: float = 60.0, max_alerts: int = 50, now_fn=None):
        self._dwell_s = max(0.0, float(dwell_s))
        self._max = max_alerts
        self._now_fn = now_fn
        self._since: dict[str, datetime] = {}      # room -> ts the CURRENT episode started
        self._last_seen: dict[str, datetime] = {}   # room -> ts of the most recent at_risk=True
        self._fired: set[str] = set()                # rooms already alerted THIS episode
        self._alerts: list[FallAlert] = []

    def _now_iso(self) -> str:
        if self._now_fn is not None:
            return self._now_fn()
        return datetime.now(timezone.utc).isoformat()

    def record(self, room: str, at_risk: bool, ts: str | None = None) -> FallAlert | None:
        ts = ts or self._now_iso()
        try:
            now = _as_utc(ts)
        except (TypeError, ValueError):
            return None   # a malformed ts must never crash the fusion tick for this room
        if not at_risk:
            last = self._last_seen.get(room)
            if last is not None and (now - last).total_seconds() > FLICKER_GRACE_S:
                self._since.pop(room, None)
                self._last_seen.pop(room, None)
                self._fired.discard(room)
            return None
        self._last_seen[room] = now
        since = self._since.get(room)
        if since is None:
            self._since[room] = now
            return None
        elapsed = (now - since).total_seconds()
        if elapsed < self._dwell_s or room in self._fired:
            return None
        self._fired.add(room)
        alert = FallAlert(room, round(elapsed, 1), ts)
        self._alerts.append(alert)
        if len(self._alerts) > self._max:
            self._alerts = self._alerts[-self._max:]
        return alert

    def reset(self) -> None:
        """Clear ALL per-room dwell/edge state (not the alert ring) -- mirrors
        IntrusionAlertLog.reset(), used when the master toggle is turned off then back on so
        a still-at-risk room re-arms instead of staying silently latched."""
        self._since.clear()
        self._last_seen.clear()
        self._fired.clear()

    def recent_alerts(self, limit: int = 50) -> list[FallAlert]:
        return self._alerts[-limit:]

    def active_alerts(self) -> list[FallAlert]:
        """The latest FallAlert for each room CURRENTLY latched (`_fired`) -- mirrors
        `wavr.watch.IntrusionAlertLog.active_alerts()` exactly, feeding
        `wavr.house_status`'s physical-layer input. A room whose episode already cleared
        is never included, so a resolved fall-suspected episode cannot pin the house-status
        composite at 'alert' forever."""
        fired = self._fired
        if not fired:
            return []
        found: dict[str, FallAlert] = {}
        for alert in reversed(self._alerts):
            if alert.room in fired and alert.room not in found:
                found[alert.room] = alert
                if len(found) == len(fired):
                    break
        return list(found.values())
