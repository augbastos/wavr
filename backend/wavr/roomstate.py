from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class RoomState:
    room: str
    occupied: bool
    confidence: float
    vitals: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    # House-level "who is home" (non-biometric, opt-in). Empty unless identity is
    # explicitly enabled AND a known device is present. Never per-room identity —
    # it only ever populates on the house-level 'casa' pseudo-room.
    identities: list = field(default_factory=list)
    # Per-room person COUNT (additive, honest). int when a counting-capable source
    # (camera/mmwave) present in this room vouches for a number; None = unknown (never
    # a fabricated 0). Absent/None behaves exactly as before this field existed.
    person_count: int | None = None
    explanation: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
