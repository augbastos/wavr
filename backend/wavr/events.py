from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass(frozen=True)
class SensingEvent:
    room: str
    modality: str            # "wifi_csi" | "network" | "camera" | "sim"
    presence: bool
    motion: float
    breathing_bpm: float | None
    heart_bpm: float | None
    confidence: float        # the modality's own confidence 0..1
    ts: str                  # ISO-8601 UTC (+00:00)

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _f(v):
    return None if v is None else float(v)


def normalize_ruview(raw: dict, room: str) -> SensingEvent:
    classification = raw.get("classification", {})
    features = raw.get("features", {})
    vitals = raw.get("vital_signs", {})
    ts = raw.get("timestamp")
    return SensingEvent(
        room=room,
        modality="wifi_csi",
        presence=bool(classification.get("presence", False)),
        motion=float(features.get("motion_band_power", 0.0)),
        breathing_bpm=_f(vitals.get("breathing_rate_bpm")),
        heart_bpm=_f(vitals.get("heart_rate_bpm")),
        confidence=float(classification.get("confidence", 0.0)),
        ts=_iso_from_unix(ts) if ts is not None else datetime.now(timezone.utc).isoformat(),
    )
