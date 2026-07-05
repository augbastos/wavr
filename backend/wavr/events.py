from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass(frozen=True)
class Target:
    """One tracked person. Room-local frame: meters, origin = room's top-left
    on the house map, x right / y down. x/y None = source knows posture but
    not position (e.g. camera without homography)."""
    id: int
    x: float | None
    y: float | None
    z: float | None = None
    posture: str | None = None      # open vocab: standing/sitting/lying/walking/...
    velocity: float | None = None   # m/s, magnitude
    confidence: float = 0.0

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
