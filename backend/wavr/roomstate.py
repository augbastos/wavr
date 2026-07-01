from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class RoomState:
    room: str
    occupied: bool
    confidence: float
    vitals: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    explanation: str = ""
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
