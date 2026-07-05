from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from wavr.events import SensingEvent
from wavr.roomstate import RoomState

# Default trust weights per modality. Camera (video) is most precise; network
# (device presence) is house-level and coarse. Tunable via config later.
# `ble` (Bluetooth presence) sits between wifi_csi and network: room-ish, coarser
# than CSI but tighter than house-wide ARP.
DEFAULT_WEIGHTS = {"camera": 1.0, "mmwave": 0.9, "wifi_csi": 0.85, "ble": 0.7,
                   "network": 0.5, "sim": 0.6}

# Freshness decay window (seconds). A source votes at full trust up to
# FRESHNESS_S, then its trust decays linearly to zero at STALE_S — so a source
# that stopped reporting gradually loses its vote instead of freezing the fused
# confidence on a dead reading. Overridable via env.
_DEFAULT_FRESHNESS_S = float(os.getenv("WAVR_SOURCE_FRESHNESS_S", "30"))
_DEFAULT_STALE_S = float(os.getenv("WAVR_SOURCE_STALE_S", "90"))

# Occupancy dwell / hysteresis window (seconds). Asymmetric debounce on the
# per-room `occupied` boolean: a room flips to occupied the instant confidence
# clears the threshold (lights-on responsiveness), but once confidence falls
# back below it, `occupied` is HELD until confidence has stayed below for
# VACATE_S wall-clock seconds -- so a single dropped frame / momentary low
# reading cannot flick a room vacant and fire a "vacant" automation on someone
# still sitting there. Only the boolean is debounced; `confidence` stays
# continuous and the pending exit is surfaced in the explanation. Set
# WAVR_ROOM_VACATE_S=0 to disable (raw threshold crossing). Overridable via env.
_DEFAULT_VACATE_S = float(os.getenv("WAVR_ROOM_VACATE_S", "45"))


def _as_utc(value) -> datetime:
    """Coerce an ISO-8601 string (or datetime) to an aware UTC datetime."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class FusionEngine:
    """Explainable fusion. Per room, confidence = agreement × strength, where
    `agreement` is the fraction of trusted mass saying "present" and `strength`
    is the best present evidence (weight × the source's own confidence). This stops
    a lone weak source (e.g. coarse network) from ever reporting 100%, and lets a
    trusted source dominate when modalities disagree.

    Each source's trust is additionally scaled by a freshness decay: full weight
    while the reading is fresh, fading to zero once it is stale, so a source that
    stopped reporting honestly loses its vote (and the fused confidence drops)
    rather than freezing on its last reading.

    The per-room `occupied` boolean is additionally run through an asymmetric
    wall-clock dwell (fast to occupied, slow to vacant) so a single-frame
    confidence dip cannot flap a room -- see `_debounce_occupancy`. This is the
    one place occupancy is decided, so the dashboard, rules.py and away.py all
    consume the SAME debounced boolean."""

    def __init__(self, weights: dict | None = None, threshold: float = 0.5,
                 now_fn=None, freshness_s: float | None = None,
                 stale_s: float | None = None, vacate_s: float | None = None):
        self._weights = weights if weights is not None else DEFAULT_WEIGHTS
        self._threshold = threshold
        # Injectable clock returning an aware UTC "now". When None (default) each
        # source is aged against the room's newest event, which keeps a live
        # stream fully fresh and stays deterministic for fixed-timestamp tests.
        # Pass now_fn=lambda: datetime.now(timezone.utc) for wall-clock aging.
        self._now_fn = now_fn
        self._freshness_s = _DEFAULT_FRESHNESS_S if freshness_s is None else freshness_s
        self._stale_s = _DEFAULT_STALE_S if stale_s is None else stale_s
        # Asymmetric occupancy dwell: how long `occupied` is held after
        # confidence falls below threshold before it may flip to vacant
        # (0 disables the dwell).
        self._vacate_s = _DEFAULT_VACATE_S if vacate_s is None else vacate_s
        self._latest: dict[str, dict[str, SensingEvent]] = {}  # room -> modality -> event
        self._occupied_state: dict[str, bool] = {}     # room -> last debounced occupied
        self._vacate_since: dict[str, datetime] = {}   # room -> when a pending vacate began

    def update(self, event: SensingEvent) -> RoomState:
        room_events = self._latest.setdefault(event.room, {})
        try:
            _as_utc(event.ts)
            valid_ts = True
        except (TypeError, ValueError):
            valid_ts = False

        if valid_ts:
            room_events[event.modality] = event
            ts = event.ts
        else:
            # A malformed/unparseable ts must never be stored: once in
            # `_latest` it would poison this modality's slot and make every
            # later fuse touching the room raise (killing healthy sources
            # one-by-one). Reject the event instead and fuse whatever's
            # already known for the room.
            ts = max((e.ts for e in room_events.values()), default=None)
            if ts is None:
                ts = datetime.now(timezone.utc).isoformat()
        return self._fuse(event.room, ts)

    def state(self, room: str) -> RoomState | None:
        if room not in self._latest:
            return None
        last_ts = max(e.ts for e in self._latest[room].values())
        return self._fuse(room, last_ts)

    def rooms(self) -> list[str]:
        """Every room the engine has ever fused. Authoritative room set (the
        engine's own `_latest` keys) — used by the periodic re-fuse tick to age
        rooms that have stopped receiving events, rather than trusting an
        app-side mirror dict that could drift."""
        return list(self._latest.keys())

    def _freshness(self, age_s: float) -> tuple[float, str]:
        """Map a source's age to (trust multiplier 0..1, health label).
        fresh → full weight; stale → linearly decayed; dead → zero weight."""
        if age_s <= self._freshness_s:
            return 1.0, "fresh"
        if age_s >= self._stale_s or self._stale_s <= self._freshness_s:
            return 0.0, "dead"
        return (self._stale_s - age_s) / (self._stale_s - self._freshness_s), "stale"

    def _debounce_occupancy(self, room: str, raw_occupied: bool,
                            ref: datetime) -> tuple[bool, float | None]:
        """Asymmetric wall-clock dwell on the per-room `occupied` boolean.

        Fast to occupied: flip the instant confidence clears the threshold
        (lights-on responsiveness is non-negotiable). Slow to vacant: once
        confidence drops below the threshold, HOLD `occupied` until it has
        stayed below for `self._vacate_s` wall-clock seconds; any re-cross above
        the threshold in that window cancels the pending vacate. Only the
        boolean is debounced -- `confidence` stays continuous and honest.

        Returns `(occupied, pending_s)` where `pending_s` is the seconds still
        remaining on a pending vacate (None when not counting down), surfaced in
        the explanation so the uncertainty is shown, never hidden. Measured
        against the SAME `ref` clock the freshness decay uses, so the dwell and
        ageing stay consistent and deterministic under a fixed/injected clock."""
        prev = self._occupied_state.get(room)
        if raw_occupied:
            # Fast path to occupied; abandon any in-flight vacate.
            self._vacate_since.pop(room, None)
            self._occupied_state[room] = True
            return True, None
        if prev is not True:
            # Already vacant, or first-ever reading for the room: nothing to hold.
            self._vacate_since.pop(room, None)
            self._occupied_state[room] = False
            return False, None
        # Was occupied and has now dropped below threshold -> run the vacate dwell.
        started = self._vacate_since.setdefault(room, ref)
        elapsed = max(0.0, (ref - started).total_seconds())
        if self._vacate_s <= 0 or elapsed >= self._vacate_s:
            # Dwell disabled, or the grace has fully elapsed -> confirm vacant.
            self._vacate_since.pop(room, None)
            self._occupied_state[room] = False
            return False, None
        # Still within the grace window -> hold occupied, report the countdown.
        self._occupied_state[room] = True
        return True, self._vacate_s - elapsed

    def _fuse(self, room: str, ts: str) -> RoomState:
        events = self._latest[room]
        # Reference "now" for ageing: injected clock, else the room's newest event
        # (identity for fresh events → existing fusion math is unchanged).
        try:
            ref = _as_utc(self._now_fn()) if self._now_fn is not None else _as_utc(ts)
        except (TypeError, ValueError):
            # ts itself is unusable (e.g. the room has no stored events yet and
            # the triggering event's ts was rejected upstream) — fall back to
            # wall-clock rather than raising.
            ref = datetime.now(timezone.utc)
        num = 0.0        # weighted mass saying "present"
        den = 0.0        # total weighted mass
        strength = 0.0   # best present evidence (weight × confidence)
        sources = []
        vitals: dict = {}
        decays: dict[str, float] = {}  # modality -> trust multiplier, reused for target gating
        for modality, e in events.items():
            try:
                e_ts = _as_utc(e.ts)
            except (TypeError, ValueError):
                # Defensive: a stored event with an unparseable ts must never
                # crash fusion for the room's other, healthy sources. Treat it
                # as contributing no evidence (same as a dead source).
                decays[modality] = 0.0
                sources.append({"modality": modality, "presence": e.presence,
                                "confidence": round(e.confidence, 3),
                                "age_s": None, "health": "invalid_ts"})
                continue
            age_s = max(0.0, (ref - e_ts).total_seconds())
            decay, health = self._freshness(age_s)
            decays[modality] = decay
            mass = self._weights.get(modality, 0.5) * e.confidence * decay
            den += mass
            if e.presence:
                num += mass
                strength = max(strength, mass)
            sources.append({"modality": modality, "presence": e.presence,
                            "confidence": round(e.confidence, 3),
                            "age_s": round(age_s), "health": health})
            if e.presence and e.breathing_bpm is not None:
                vitals = {"breathing_bpm": e.breathing_bpm, "heart_bpm": e.heart_bpm}
        agreement = num / den if den > 0 else 0.0
        # Defensive clamp: a single out-of-range source confidence (negative or
        # >1) must never drive the fused confidence outside [0, 1].
        confidence = round(min(1.0, max(0.0, agreement * strength)), 3)
        raw_occupied = confidence >= self._threshold
        occupied, pending_s = self._debounce_occupancy(room, raw_occupied, ref)
        parts = [f"{s['modality']}: {'presente' if s['presence'] else 'vazio'}" for s in sources]
        explanation = " · ".join(parts) + f" → {int(confidence * 100)}% ocupado"
        if pending_s is not None:
            # Confidence has dropped below threshold but the room is still HELD
            # occupied by the dwell -- show the countdown, do not hide the doubt.
            rem = math.ceil(pending_s)
            explanation += f", confirmando saída {rem // 60}:{rem % 60:02d}"

        best_targets: list = []
        best_w = -1.0
        for modality, e in events.items():
            # Same freshness/decay gate as the confidence loop: a stale/dead
            # (or invalid-ts) source must not pass its targets through — a
            # decayed-to-zero source is indistinguishable from an absent one.
            if e.presence and e.targets and decays.get(modality, 0.0) > 0.0:
                w = self._weights.get(modality, 0.5)
                if w > best_w:
                    best_w = w
                    best_targets = [t.to_dict() for t in e.targets]

        # Identity pass-through (non-biometric "who is home"). METADATA ONLY: this
        # rides the SAME present + fresh (decay>0) gate as targets and NEVER feeds
        # num/den/strength/agreement above — so `confidence` is provably unchanged
        # whether or not identities are present. Deduped by person, keeping the
        # entry with the stronger (higher/closer) rssi; a labelled entry with an
        # rssi always beats one without.
        merged: dict[str, dict] = {}
        for modality, e in events.items():
            if not (e.presence and e.identities and decays.get(modality, 0.0) > 0.0):
                continue
            for ident in e.identities:
                d = ident.to_dict()
                person = d.get("person")
                if not person:
                    continue
                prev = merged.get(person)
                if prev is None:
                    merged[person] = d
                    continue
                pr, cr = prev.get("rssi"), d.get("rssi")
                if cr is not None and (pr is None or cr > pr):
                    merged[person] = d
        identities = list(merged.values())

        return RoomState(room=room, occupied=occupied, confidence=confidence,
                         vitals=vitals, sources=sources, targets=best_targets,
                         identities=identities, explanation=explanation, ts=ts)
