from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass(frozen=True)
class Target:
    """One tracked person.

    `frame` says which coordinate system x/y are in, and it is load-bearing:
    two sources were filling these fields in DIFFERENT frames while this
    docstring claimed one.

      * "room"   — metres, offset from the room polygon's min corner, x right,
                   y down. What this class always promised, and what the map
                   expects. The camera path produces it (localize.to_room_local).
      * "sensor" — the emitting sensor's own origin and axes. What an LD2450
                   radar actually reports. Meaningless anywhere else until the
                   sensor's mount is known, so `spatial_frames.transform_target`
                   either converts it or REMOVES the position.

    x/y None = the source knows posture but not position (a camera without a
    homography, or a sensor-frame target Wavr cannot place). None is the honest
    answer; a coordinate in an unknown frame is not.

    Defaults to "room" so every existing producer and stored event keeps its
    current meaning.
    """
    id: int
    x: float | None
    y: float | None
    z: float | None = None
    posture: str | None = None      # open vocab: standing/sitting/lying/walking/...
    velocity: float | None = None   # m/s, magnitude
    confidence: float = 0.0
    frame: str = "room"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Identity:
    """A device-to-person association surfaced as house-level "who is home".
    NON-BIOMETRIC: `person` is an operator-configured label (a phone's MAC/BLE
    address named after its owner), never derived from face/voice/gait/re-ID.
    `source` is the modality that saw the device ("ble" | "network"). `rssi` is
    coarse proximity to the ONE host adapter (BLE only; network scan has none ->
    None) and MUST NEVER be rendered as a room — a single antenna can localize to
    the house, not a room."""
    person: str
    source: str
    rssi: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SensingEvent:
    room: str
    modality: str            # "wifi_csi" | "network" | "camera" | "mmwave" | "sim"
    presence: bool
    motion: float
    breathing_bpm: float | None
    heart_bpm: float | None
    confidence: float        # the modality's own confidence 0..1
    ts: str                  # ISO-8601 UTC (+00:00)
    targets: tuple = ()      # tuple[Target, ...] — new optional last field
    identities: tuple = ()   # tuple[Identity, ...] — house-level "who is home"
    # Per-source person COUNT (additive). Only counting-capable modalities set it:
    # camera (Detection.count) and mmwave (len(targets)). Presence-only sources
    # (network/ble/wifi_csi/sim) leave it None = count-unknown, NEVER 0 -- a source
    # that cannot count must never assert a number it lacks (honesty).
    count: int | None = None
    # WHICH sensor produced this, when the sensor has a stable identity: a
    # camera's operator-given name, an enrolled node's id, a wired radar's port.
    # Empty for a source with no per-instance identity -- the host's single
    # network scan is the house's one network sense, not one of several.
    #
    # Load-bearing for reliability (a profile is per SENSOR, not per modality --
    # two cameras in one house behave differently) and for ground-truth
    # measurement (every hit and miss must attribute to something). Optional with
    # an empty default so every existing source and stored event stays valid, and
    # an anonymous event falls back to its modality's constant.
    #
    # NEVER self-declared by the device: a node's id comes from its enrolment
    # row, a camera's from the store. A sensor may not assert its own identity
    # any more than it may assert its own sensor type.
    sensor_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["targets"] = list(d["targets"])
        d["identities"] = list(d["identities"])
        return d


def _iso_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _f(v):
    return None if v is None else float(v)


def normalize_ruview(raw: dict, room: str) -> SensingEvent:
    classification = raw.get("classification", {})
    features = raw.get("features", {})
    vitals = raw.get("vital_signs", {})
    ts = raw.get("timestamp")

    targets = []
    for i, t in enumerate(raw.get("targets") or []):
        if not isinstance(t, dict):
            continue
        x, y = t.get("x"), t.get("y")
        posture = t.get("posture")
        def _num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        has_pos = _num(x) and _num(y)
        if not has_pos and not isinstance(posture, str):
            continue
        targets.append(Target(
            id=int(t.get("id", i + 1)),
            x=float(x) if has_pos else None,
            y=float(y) if has_pos else None,
            z=_f(t.get("z")),
            posture=posture if isinstance(posture, str) else None,
            velocity=_f(t.get("velocity")),
            confidence=float(t.get("confidence", 0.5)),
        ))

    return SensingEvent(
        room=room,
        modality="wifi_csi",
        presence=bool(classification.get("presence", False)),
        motion=float(features.get("motion_band_power", 0.0)),
        breathing_bpm=_f(vitals.get("breathing_rate_bpm")),
        heart_bpm=_f(vitals.get("heart_rate_bpm")),
        confidence=max(0.0, min(1.0, float(classification.get("confidence", 0.0)))),
        ts=_iso_from_unix(ts) if ts is not None else datetime.now(timezone.utc).isoformat(),
        targets=tuple(targets),
    )
