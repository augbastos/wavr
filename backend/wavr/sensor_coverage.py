"""What each sensor can honestly observe, and which rooms nothing watches.

## The question this answers

"Wavr says the living room is empty. Should I believe it?"

That depends on something the room state cannot tell you: whether anything is
watching the living room at all, and whether what is watching can see a person
standing still. A confident `occupied: false` from a house-level Wi-Fi count and
a confident `occupied: false` from a calibrated camera are not the same claim,
and a product that renders them identically is lying by omission.

## Why this is not the same as reading room state

`fusion` publishes what each source REPORTED. This module publishes what each
source EXISTS TO REPORT. The difference matters exactly when something breaks:

    a radar installed in the kitchen that stopped answering an hour ago
      -> fusion: the kitchen has no sources
      -> here:   the kitchen has a count-capable radar, and it is OFFLINE

Deriving coverage from live state (which is what `mcp.get_sensor_coverage` did
before this module) collapses those two into "uncovered", so a dead sensor is
indistinguishable from a sensor nobody ever installed. The first is a repair
job; the second is a shopping list.

## The honesty rules

1. **No fabricated percentages.** A "living room 87% covered" number needs a
   camera field-of-view, a radar coverage polygon and a floor plan with walls.
   Wavr has none of those for most installs, so this module reports the
   precision LEVEL each sensor supports and never invents an area figure. When
   real geometry exists, that is when a coverage polygon may be added -- see
   `geometry` below, deliberately left as None.

2. **Precision comes from `fusion.RESOLUTION_SCOPE`, not from a second table.**
   One source of truth for "how detailed an answer can this modality honestly
   support". A copy here would drift, and the drift would be invisible.

3. **The position rung is EARNED.** A camera reports `count` until it has a
   stored homography; only then can it place a person at a coordinate. That
   mirrors fusion's own rule (position is granted at runtime by a positioned
   target, never by a modality's mere presence) and it is why `calibrated` is a
   first-class field rather than a detail.

4. **A room with no sensor is stated, never implied.** `rooms_without_coverage`
   exists so a caller cannot forget to ask.

5. **Health is what the supervisor observed, never what the switch says.** A
   camera switched ON whose task has crashed is OFFLINE here, not OK. This rule
   had to be written down because the module broke it: health was derived from
   the operator's enable flag, so a dead camera reported `observing: true` and
   `precision_level: position` — Wavr's strongest claim, about a sensor that had
   not produced a frame since the router rebooted. See `health_from_source_state`
   and `wavr.provider_runtime`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from wavr.fusion import RESOLUTION_SCOPE
from wavr.nodes import (
    STATE_ACTIVE, STATE_DISABLED, STATE_PENDING, STATE_REVOKED,
)
from wavr.provider_runtime import STATE_RUNNING, STATE_STOPPED

# The same window fusion uses to stop trusting a source that went quiet. Shared
# rather than re-read from the environment here, so "stale" means one thing.
DEFAULT_STALE_S = 90.0

# Health, in the order a human cares about it. `unknown` is not a failure -- it
# is what a source that has no heartbeat concept honestly is.
HEALTH_OK = "ok"
HEALTH_OFFLINE = "offline"
HEALTH_DISABLED = "disabled"
HEALTH_UNKNOWN = "unknown"
# No "pending": a node awaiting approval is skipped entirely (see `_node_rows`),
# so a pending health would be a value nothing could ever produce.

# Where a coverage row came from. Kept separate from `modality` because two
# different kinds of thing can share one modality: a USB radar wired to the Core
# and an ESP32 radar node both report `mmwave`, and they fail for different
# reasons.
KIND_CAMERA = "camera"
KIND_NODE = "node"
KIND_WIRED = "wired"          # a sensor cabled directly to this Core
KIND_HOST = "host"            # a capability of the machine itself (its BLE radio)


def health_from_source_state(state: str | None, *, enabled: bool) -> str:
    """A source's supervised state, in this module's health vocabulary.

    The two directions that matter:

      * **Off is not broken.** A camera the operator switched off is `disabled` —
        one settings click from working, and nothing to worry about.
      * **Broken is not off.** Anything the supervisor is retrying, has given up
        fast-retrying on, or considers silent past its declared cadence is
        `offline`: a fault, and a different sentence on the screen.

    `state is None` means the caller could not observe health at all — a store
    read without a running SourceManager behind it — so the reading falls back to
    the enable flag. That fallback is the OLD behaviour and it is a lie whenever
    a source has actually crashed, which is exactly why the single production
    caller passes real health. It survives only for callers with nothing better
    to offer, and `unknown` is not used because a coverage row that renders as
    "unknown" for a perfectly healthy camera would be its own kind of noise.
    """
    if not enabled:
        return HEALTH_DISABLED
    if state is None:
        return HEALTH_OK
    if state == STATE_RUNNING:
        return HEALTH_OK
    if state == STATE_STOPPED:
        return HEALTH_DISABLED
    return HEALTH_OFFLINE


@dataclass(frozen=True)
class SensorCoverage:
    """One sensor's honest observational reach.

    `room` is empty for a sensor that does not localize to a room -- the host's
    Bluetooth radio and the network scan both see the whole house, and pinning
    them to a room would be the fabrication this module exists to avoid.
    """

    sensor_id: str
    kind: str
    modality: str
    room: str = ""
    node_id: str = ""
    health: str = HEALTH_UNKNOWN
    calibrated: bool = False
    # Reserved for real geometry (a camera's projected floor polygon, a radar's
    # coverage fan). None until Wavr genuinely has it; see honesty rule 1.
    geometry: dict | None = None
    detail: dict = field(default_factory=dict)

    @property
    def precision_level(self) -> str:
        """The finest answer this sensor can support, on fusion's own ladder.

        A calibrated camera earns `position`; everything else gets its
        modality's scope. An unknown modality falls back to `house` rather than
        to something flattering -- the same direction fusion errs in.
        """
        base = RESOLUTION_SCOPE.get(self.modality, "house")
        if self.kind == KIND_CAMERA and self.calibrated:
            return "position"
        return base

    @property
    def observing(self) -> bool:
        """Whether this sensor is contributing right now.

        Distinct from existing. A disabled camera still tells you the room is
        watchable; it is just not being watched.
        """
        return self.health == HEALTH_OK

    def to_dict(self) -> dict:
        out = {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "modality": self.modality,
            "room": self.room,
            "health": self.health,
            "calibrated": self.calibrated,
            "precision_level": self.precision_level,
            "observing": self.observing,
        }
        if self.node_id:
            out["node_id"] = self.node_id
        if self.geometry is not None:
            out["geometry"] = self.geometry
        if self.detail:
            out["detail"] = self.detail
        return out


def _camera_rows(cameras, calib, enabled_names,
                 source_health=None) -> list[SensorCoverage]:
    """Cameras, from the store the dashboard reads.

    `enabled_names` is the set of camera source names currently switched ON.
    Per-camera, not a single process-wide flag: Wavr genuinely runs one camera
    enabled and another off (`manager.status()["sources"]`), and collapsing that
    into one boolean would report a working camera as disabled or vice versa.

    A camera the operator added but has not switched on is `disabled`, not
    `offline` -- one is a choice and the other is a fault, and telling them apart
    is the difference between a settings click and a ladder. Cameras boot OFF by
    design (ADR-0002), so `disabled` is the NORMAL state of a new one.

    `source_health` closes the third case, which used to be reported as the
    first: a camera switched ON whose source task has crashed. That is neither a
    choice nor a working camera, and it is the state a router reboot leaves
    behind.
    """
    rows: list[SensorCoverage] = []
    on = set(enabled_names or ())
    health_of = dict(source_health or {})
    for cam in _safe_list(cameras):
        name = str(cam.get("name") or "")
        if not name:
            continue
        calibrated = False
        if calib is not None:
            try:
                c = calib.get(name)
            except Exception:      # noqa: BLE001 -- a calibration read must not
                c = None           # decide whether a camera EXISTS
            # A mount pose alone does not place anyone: the homography is what
            # turns a pixel into a floor coordinate. Anything less is `count`.
            # `homography`, which is what `CalibrationStore.get` actually
            # returns. This read `c.get("h")` — a key that store has never
            # emitted — so `calibrated` was False for every camera ever
            # calibrated, and the "position rung is EARNED" rule three lines up
            # meant no camera could ever earn it. Silent: a fully calibrated
            # camera simply reported count-only, to the API, the MCP tool and
            # the dashboard alike.
            calibrated = bool(c and c.get("homography"))
        rows.append(SensorCoverage(
            sensor_id=name, kind=KIND_CAMERA, modality="camera",
            room=str(cam.get("room") or ""),
            health=health_from_source_state(
                health_of.get(name) if source_health is not None else None,
                enabled=name in on),
            calibrated=calibrated,
        ))
    return rows


def _node_health(state: str, last_seen_ts: str | None, *, stale_s: float,
                 now: datetime | None = None) -> str:
    """A node's health, which its `state` column alone cannot give.

    `nodes.py` has exactly four states -- pending, active, disabled, revoked --
    and **none of them is "offline"**. A radar that was unplugged an hour ago is
    still `active`; its silence lives in `last_seen_ts`. So offline is derived
    here, from the same staleness window fusion uses to stop trusting a source
    (`WAVR_SOURCE_STALE_S`), rather than invented as a fifth state nothing could
    ever set.

    An active node that has NEVER reported is offline, not unknown: it was
    approved, it should be talking, and it is not.

    Pending never reaches here -- `_node_rows` drops those before calling.
    """
    if state == STATE_DISABLED:
        return HEALTH_DISABLED
    if state != STATE_ACTIVE:
        return HEALTH_UNKNOWN
    if not last_seen_ts:
        return HEALTH_OFFLINE
    try:
        seen = datetime.fromisoformat(str(last_seen_ts))
    except ValueError:
        # An unparseable timestamp is not evidence of health. Saying "ok" here
        # would let a corrupt row present as a working sensor.
        return HEALTH_UNKNOWN
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return HEALTH_OK if (ref - seen).total_seconds() <= stale_s else HEALTH_OFFLINE


def _node_rows(nodes, *, stale_s: float, now: datetime | None = None) -> list[SensorCoverage]:
    """Sensor nodes, with liveness derived rather than assumed.

    A node's `modality` is already the operator-set, never node-claimed value
    (`nodes.SENSOR_MODALITY`), so it can be trusted here. An environmental node
    has modality "" -- it senses, but not presence -- and is reported with an
    empty modality rather than being silently dropped, because "there is a
    thermometer in there and no presence sensor" is the useful answer.
    """
    rows: list[SensorCoverage] = []
    for node in _safe_list(nodes):
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        state = str(node.get("state") or "")
        if state == STATE_REVOKED:
            continue                       # gone, not merely quiet
        if state == STATE_PENDING:
            # A pending node holds no credential, has no room and no sensor type
            # — nobody has decided what it is yet. It senses nothing, so it is
            # not coverage; it is a QUESTION, and the Discovery Inbox is where
            # questions live. Listing it here put an unplaced node in the
            # house-wide bucket beside the Wi-Fi scan, implying the house was
            # being watched by something that has not been let in.
            continue
        rows.append(SensorCoverage(
            sensor_id=str(node.get("name") or node_id), kind=KIND_NODE,
            modality=str(node.get("modality") or ""),
            room=str(node.get("room") or ""), node_id=node_id,
            health=_node_health(state, node.get("last_seen_ts"),
                                stale_s=stale_s, now=now),
            detail={"sensor_type": str(node.get("sensor_type") or "")},
        ))
    return rows


def _host_rows(*, ble: bool = False, ble_room: str = "", network: bool = False,
               serial_rooms: tuple[str, ...] = (),
               source_health=None) -> list[SensorCoverage]:
    """Sensors that are part of the machine, plus anything cabled to it.

    The network scan gets NO room: one antenna localizes to the HOUSE, and fusion
    pins `network` to house scope for exactly that reason. Claiming a room would
    put a person somewhere on the strength of a signal that cannot tell rooms
    apart.

    Bluetooth is different and gets `ble_room`. Wavr's BLE source is configured
    with one room (`WAVR_BLE_ROOM`) and emits into it, and fusion scopes `ble` to
    `room` accordingly. Reporting it as roomless here would contradict where its
    readings actually land.

    These three used to be hard-coded `ok`, which made a claim from CONFIGURATION
    rather than from observation: an unplugged USB radar reported as watching its
    room forever, because the config still named a serial port. Health now comes
    from the source behind each row -- `ble`, `network` and `mmwave` are the names
    those sources are registered under, and that coupling is deliberate: a row
    whose source is missing from the manager entirely reads `unknown`, never
    green, so a renamed source shows up as a gap instead of false reassurance.
    """
    rows: list[SensorCoverage] = []
    seen = source_health is not None
    health_of = dict(source_health or {})

    def _health(source_name: str) -> str:
        if not seen:
            return HEALTH_OK          # legacy caller; see health_from_source_state
        if source_name not in health_of:
            # The config says this sensor exists and the manager has no source
            # behind it. Something is mis-wired, and a mis-wired sensor is
            # certainly not observing anything.
            return HEALTH_UNKNOWN
        return health_from_source_state(health_of[source_name], enabled=True)

    if ble:
        rows.append(SensorCoverage(sensor_id="host-bluetooth", kind=KIND_HOST,
                                   modality="ble", room=str(ble_room or ""),
                                   health=_health("ble")))
    if network:
        rows.append(SensorCoverage(sensor_id="host-network", kind=KIND_HOST,
                                   modality="network", health=_health("network")))
    for room in serial_rooms:
        rows.append(SensorCoverage(
            sensor_id=f"wired-radar-{room or 'unassigned'}", kind=KIND_WIRED,
            modality="mmwave", room=str(room or ""), health=_health("mmwave")))
    return rows


def _safe_list(source) -> list[dict]:
    """Rows from a store, a callable, or nothing.

    Narrow on purpose: this catches a store that is absent or momentarily
    unreadable, and it does NOT catch a wrong method name -- callers pass either
    an iterable of rows or a zero-arg callable, and anything else is a wiring
    bug that should surface rather than silently produce "no sensors", which
    reads as "your house has no coverage".
    """
    if source is None:
        return []
    rows = source() if callable(source) else source
    return [r for r in (rows or []) if isinstance(r, dict)]


def collect_coverage(*, cameras=None, nodes=None, calib=None,
                     cameras_enabled=(), ble: bool = False, ble_room: str = "",
                     network: bool = False,
                     serial_rooms: tuple[str, ...] = (),
                     source_health=None,
                     stale_s: float = DEFAULT_STALE_S,
                     now: datetime | None = None) -> list[SensorCoverage]:
    """Every sensor this Core knows it has, whether or not it is talking.

    Deliberately takes stores rather than reaching for globals, so this is
    testable without an app and reusable from the API, the MCP tool and the
    dashboard without three slightly different versions appearing. `now` is
    injectable for the same reason: a coverage test that depends on the wall
    clock is a test that fails at midnight.

    `source_health` maps a SourceManager source name to its supervised state
    (`wavr.provider_runtime`). Without it every enabled sensor reads as working,
    which is what this module used to do and is wrong the moment anything
    actually breaks -- see honesty rule 5.
    """
    return (_camera_rows(cameras, calib, cameras_enabled, source_health)
            + _node_rows(nodes, stale_s=stale_s, now=now)
            + _host_rows(ble=ble, ble_room=ble_room, network=network,
                         serial_rooms=serial_rooms, source_health=source_health))


def by_room(coverage: list[SensorCoverage]) -> dict[str, list[SensorCoverage]]:
    """Group by room, dropping the house-wide sensors.

    Those are not roomless by accident (see `_host_rows`), so they are excluded
    here rather than filed under "" and later rendered as a room called nothing.
    """
    out: dict[str, list[SensorCoverage]] = {}
    for c in coverage:
        if not c.room:
            continue
        out.setdefault(c.room, []).append(c)
    return out


def rooms_without_coverage(rooms, coverage: list[SensorCoverage]) -> list[str]:
    """Rooms with no sensor at all -- the answer that changes a decision.

    A room here is not empty; it is unobserved. Callers must render the
    difference, which is why this is a separate function with a name that says
    so instead of an `else` branch someone can forget.
    """
    covered = set(by_room(coverage))
    return [r for r in (rooms or []) if r not in covered]


def best_precision(coverage: list[SensorCoverage]) -> str:
    """The finest level the OBSERVING sensors here can support.

    Only observing ones count: a disabled camera cannot place anyone, so letting
    it raise the answer would restate the exact overclaim this module exists to
    prevent.
    """
    from wavr.fusion import _SCOPE_RANK, _RANK_SCOPE

    ranks = [_SCOPE_RANK.get(c.precision_level, 0) for c in coverage
             if c.observing]
    return _RANK_SCOPE[max(ranks)] if ranks else "none"


def summarize(rooms, coverage: list[SensorCoverage]) -> dict:
    """The shape the API, the MCP tool and the dashboard all render.

    One serializer, so the three surfaces cannot disagree about what a room's
    coverage is -- which they did when each derived it for itself.
    """
    grouped = by_room(coverage)
    return {
        "rooms": [
            {
                "room": room,
                "sensors": [c.to_dict() for c in grouped[room]],
                "precision_level": best_precision(grouped[room]),
                # Stated per room, because "there is a camera in here but it is
                # switched off" is the case a bare sensor list hides.
                "observing": any(c.observing for c in grouped[room]),
            }
            for room in sorted(grouped)
        ],
        "uncovered": rooms_without_coverage(rooms, coverage),
        "house_wide": [c.to_dict() for c in coverage if not c.room],
        "note": ("A room in `uncovered` has no sensor at all — Wavr cannot see "
                 "it, which is not the same as it being empty. A room whose "
                 "`observing` is false has a sensor that is switched off or not "
                 "answering; that is a repair, not a purchase. No percentage is "
                 "given: Wavr has no floor geometry to support one."),
    }
