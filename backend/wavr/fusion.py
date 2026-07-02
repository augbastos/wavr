from __future__ import annotations

from wavr.events import SensingEvent
from wavr.roomstate import RoomState

# Default trust weights per modality. Camera (video) is most precise; network
# (device presence) is house-level and coarse. Tunable via config later.
DEFAULT_WEIGHTS = {"camera": 1.0, "wifi_csi": 0.85, "network": 0.5, "sim": 0.6}


class FusionEngine:
    """Explainable fusion. Per room, confidence = agreement × strength, where
    `agreement` is the fraction of trusted mass saying "present" and `strength`
    is the best present evidence (weight × the source's own confidence). This stops
    a lone weak source (e.g. coarse network) from ever reporting 100%, and lets a
    trusted source dominate when modalities disagree."""

    def __init__(self, weights: dict | None = None, threshold: float = 0.5):
        self._weights = weights if weights is not None else DEFAULT_WEIGHTS
        self._threshold = threshold
        self._latest: dict[str, dict[str, SensingEvent]] = {}  # room -> modality -> event

    def update(self, event: SensingEvent) -> RoomState:
        self._latest.setdefault(event.room, {})[event.modality] = event
        return self._fuse(event.room, event.ts)

    def state(self, room: str) -> RoomState | None:
        if room not in self._latest:
            return None
        last_ts = max(e.ts for e in self._latest[room].values())
        return self._fuse(room, last_ts)

    def _fuse(self, room: str, ts: str) -> RoomState:
        events = self._latest[room]
        num = 0.0        # weighted mass saying "present"
        den = 0.0        # total weighted mass
        strength = 0.0   # best present evidence (weight × confidence)
        sources = []
        vitals: dict = {}
        for modality, e in events.items():
            mass = self._weights.get(modality, 0.5) * e.confidence
            den += mass
            if e.presence:
                num += mass
                strength = max(strength, mass)
            sources.append({"modality": modality, "presence": e.presence,
                            "confidence": round(e.confidence, 3)})
            if e.presence and e.breathing_bpm is not None:
                vitals = {"breathing_bpm": e.breathing_bpm, "heart_bpm": e.heart_bpm}
        agreement = num / den if den > 0 else 0.0
        confidence = round(agreement * strength, 3)
        occupied = confidence >= self._threshold
        parts = [f"{s['modality']}: {'presente' if s['presence'] else 'vazio'}" for s in sources]
        explanation = " · ".join(parts) + f" → {int(confidence * 100)}% ocupado"

        best_targets: list = []
        best_w = -1.0
        for modality, e in events.items():
            if e.presence and e.targets:
                w = self._weights.get(modality, 0.5)
                if w > best_w:
                    best_w = w
                    best_targets = [t.to_dict() for t in e.targets]

        return RoomState(room=room, occupied=occupied, confidence=confidence,
                         vitals=vitals, sources=sources, targets=best_targets,
                         explanation=explanation, ts=ts)
